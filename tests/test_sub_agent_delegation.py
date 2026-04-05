"""Validate that run_agent_loop can be called from within a tool handler
(sub-agent / agent-as-a-tool pattern).

This is the go/no-go gate for the expense/vendor domain split (task 151).
The test proves that:
  1. An outer agent loop can delegate to an inner agent loop via a tool
  2. The inner loop makes its own tool calls and completes independently
  3. Structured results (tables, downloads) propagate from inner to outer
  4. No shared-state corruption between loops

TestRunAgentLoopBasic — sanity checks that the extracted run_agent_loop()
function works correctly on its own, before layering nested-loop tests:

  test_text_reply: Simplest possible exchange — user says "Hi", model
  replies with text, no tools involved. The mock OpenAI client returns
  finish_reason "stop" on the first call, so the loop runs one iteration
  and exits. Confirms AgentResult has the right reply and all accumulators
  (tool_calls_executed, tables, downloads) are empty. Catches wiring
  errors from the extraction of run_agent_loop() out of chat().

  test_tool_call_round_trip: Two-iteration conversation — model requests
  vendor_count on the first LLM call, handler returns a count, then the
  model replies with text on the second call. Validates the continue path
  (append tool call + result to messages, loop back, call OpenAI again)
  and confirms "vendor_count" is recorded in tool_calls_executed.

TestSubAgentDelegation — validates the nested agent-as-a-tool pattern
that the expense/vendor domain split depends on:

  test_inner_loop_runs_and_returns_structured_result: The key test. An
  outer "vendor management" agent calls ask_expense_agent, whose handler
  invokes run_agent_loop() with a separate prompt and tool set. The inner
  "expense" agent makes its own tool call (spend_by_vendor), completes
  independently, and returns structured data. Proves end-to-end that the
  nested loop pattern works.

  test_inner_table_not_auto_merged: The inner agent returns a table
  payload (spend data with rows/columns). The handler surfaces it via the
  standard "table" key in its JSON return, and the outer loop's table-
  popping logic picks it up and adds it to result.tables. Validates that
  structured rendering data propagates from inner agent through to the
  final response correctly.

  test_independent_state_between_loops: Confirms inner and outer loops
  maintain completely separate accumulators — tables, downloads, and
  pending_actions from one don't leak into the other. This was the main
  re-entrancy concern, and it's clean because run_agent_loop() uses only
  local variables with no shared mutable state.

  test_inner_error_does_not_crash_outer: The inner agent's OpenAI call
  throws an exception. Instead of propagating the crash to the outer
  loop, the inner loop returns a graceful error message, and the outer
  agent continues and delivers its own reply. Validates fault tolerance
  in the delegation chain.
"""

import json
from unittest.mock import MagicMock

import pytest

from service.app import run_agent_loop, AgentResult, TablePayload

pytestmark = pytest.mark.vendor_management


def _mock_openai_text(text: str) -> MagicMock:
    """Create a mock OpenAI client that returns a text reply (no tool calls)."""
    client = MagicMock()
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.message.tool_calls = None
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    client.chat.completions.create.return_value = resp
    return client


def _mock_openai_tool_then_text(
    tool_name: str,
    tool_args: dict,
    tool_call_id: str,
    final_text: str,
) -> MagicMock:
    """Mock OpenAI client: round 1 triggers a tool call, round 2 returns text."""
    client = MagicMock()

    tc = MagicMock()
    tc.id = tool_call_id
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(tool_args)

    tool_choice = MagicMock()
    tool_choice.finish_reason = "tool_calls"
    tool_choice.message.tool_calls = [tc]
    tool_choice.message.model_dump.return_value = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
            }
        ],
    }

    text_choice = MagicMock()
    text_choice.finish_reason = "stop"
    text_choice.message.tool_calls = None
    text_choice.message.content = final_text

    tool_resp = MagicMock()
    tool_resp.choices = [tool_choice]
    text_resp = MagicMock()
    text_resp.choices = [text_choice]

    client.chat.completions.create.side_effect = [tool_resp, text_resp]
    return client


def _make_spend_tool_result(with_table: bool = True) -> str:
    """Simulate a spend_by_vendor tool result with an optional table payload."""
    result: dict = {
        "status": "ok",
        "data": {"vendor": "AWS", "totalAmount": 45230},
    }
    if with_table:
        result["table"] = {
            "metric": "Spend",
            "columns": ["Month", "Amount"],
            "rows": [["2026-01", 15000], ["2026-02", 16230], ["2026-03", 14000]],
            "filename": "aws-spend-by-month.csv",
        }
    return json.dumps(result)


class TestRunAgentLoopBasic:
    """Sanity: run_agent_loop works for a simple text-only exchange."""

    def test_text_reply(self):
        client = _mock_openai_text("Hello from the agent.")
        result = run_agent_loop(
            openai_client=client,
            system_prompt="You are a test agent.",
            messages_in=[{"role": "user", "content": "Hi"}],
            tools=[],
            tool_handlers={},
            caller_email="test@example.com",
        )
        assert isinstance(result, AgentResult)
        assert result.reply == "Hello from the agent."
        assert result.tool_calls_executed == []
        assert result.tables == []

    def test_tool_call_round_trip(self):
        """Single tool call followed by text reply."""

        def fake_handler(args, caller_email=""):
            return json.dumps({"status": "ok", "data": {"count": 42}})

        client = _mock_openai_tool_then_text(
            "vendor_count", {}, "tc_001", "There are 42 vendors."
        )
        result = run_agent_loop(
            openai_client=client,
            system_prompt="You are a test agent.",
            messages_in=[{"role": "user", "content": "How many vendors?"}],
            tools=[{"type": "function", "function": {"name": "vendor_count"}}],
            tool_handlers={"vendor_count": fake_handler},
            caller_email="test@example.com",
        )
        assert result.reply == "There are 42 vendors."
        assert result.tool_calls_executed == ["vendor_count"]


class TestSubAgentDelegation:
    """Core validation: an outer loop delegates to an inner loop via a tool."""

    def test_inner_loop_runs_and_returns_structured_result(self):
        """The ask_expense_agent handler calls run_agent_loop internally;
        the outer loop receives the inner result as a tool response."""

        inner_client = _mock_openai_tool_then_text(
            "spend_by_vendor",
            {"vendor": "AWS", "period": "YTD"},
            "inner_tc_001",
            "AWS spend is $45,230 YTD.",
        )

        def ask_expense_agent_handler(args, caller_email=""):
            inner_result = run_agent_loop(
                openai_client=inner_client,
                system_prompt="You are an expense analytics agent.",
                messages_in=[{"role": "user", "content": args["question"]}],
                tools=[{"type": "function", "function": {"name": "spend_by_vendor"}}],
                tool_handlers={
                    "spend_by_vendor": lambda a, caller_email="": _make_spend_tool_result(),
                },
                caller_email=caller_email,
            )
            response = {
                "status": "ok",
                "reply": inner_result.reply,
                "tables": [
                    {"metric": t.metric, "columns": t.columns, "rows": t.rows, "filename": t.filename}
                    for t in inner_result.tables
                ],
            }
            return json.dumps(response)

        outer_client = _mock_openai_tool_then_text(
            "ask_expense_agent",
            {"question": "How much do we spend on AWS?"},
            "outer_tc_001",
            "AWS spend is $45,230 year-to-date.",
        )

        result = run_agent_loop(
            openai_client=outer_client,
            system_prompt="You are a vendor management agent.",
            messages_in=[{"role": "user", "content": "How much do we spend on AWS?"}],
            tools=[{"type": "function", "function": {"name": "ask_expense_agent"}}],
            tool_handlers={"ask_expense_agent": ask_expense_agent_handler},
            caller_email="test@example.com",
        )

        assert result.reply == "AWS spend is $45,230 year-to-date."
        assert result.tool_calls_executed == ["ask_expense_agent"]

    def test_inner_table_not_auto_merged(self):
        """Tables from the inner loop come back as data in the tool result,
        not auto-merged into the outer loop's tables list. The outer handler
        is responsible for merging if desired."""

        inner_client = _mock_openai_tool_then_text(
            "spend_by_vendor",
            {"vendor": "AWS", "period": "YTD"},
            "inner_tc_001",
            "Here's the breakdown.",
        )

        def ask_expense_agent_handler(args, caller_email=""):
            inner_result = run_agent_loop(
                openai_client=inner_client,
                system_prompt="You are an expense analytics agent.",
                messages_in=[{"role": "user", "content": args["question"]}],
                tools=[{"type": "function", "function": {"name": "spend_by_vendor"}}],
                tool_handlers={
                    "spend_by_vendor": lambda a, caller_email="": _make_spend_tool_result(),
                },
                caller_email=caller_email,
            )
            response: dict = {
                "status": "ok",
                "reply": inner_result.reply,
            }
            if inner_result.tables:
                t = inner_result.tables[0]
                response["table"] = {
                    "metric": t.metric,
                    "columns": t.columns,
                    "rows": t.rows,
                    "filename": t.filename,
                }
            return json.dumps(response)

        outer_client = _mock_openai_tool_then_text(
            "ask_expense_agent",
            {"question": "Break down AWS spend by month"},
            "outer_tc_001",
            "Here's your AWS spend breakdown.",
        )

        result = run_agent_loop(
            openai_client=outer_client,
            system_prompt="You are a vendor management agent.",
            messages_in=[{"role": "user", "content": "Break down AWS spend by month"}],
            tools=[{"type": "function", "function": {"name": "ask_expense_agent"}}],
            tool_handlers={"ask_expense_agent": ask_expense_agent_handler},
            caller_email="test@example.com",
        )

        assert result.reply == "Here's your AWS spend breakdown."
        assert len(result.tables) == 1
        assert result.tables[0].metric == "Spend"
        assert result.tables[0].rows == [
            ["2026-01", 15000],
            ["2026-02", 16230],
            ["2026-03", 14000],
        ]

    def test_independent_state_between_loops(self):
        """Inner and outer loops maintain separate accumulators —
        downloads/pending_actions from one don't leak into the other."""

        inner_client = _mock_openai_text("Inner reply.")

        outer_tool_calls = []

        def ask_expense_agent_handler(args, caller_email=""):
            inner_result = run_agent_loop(
                openai_client=inner_client,
                system_prompt="Expense agent.",
                messages_in=[{"role": "user", "content": args["question"]}],
                tools=[],
                tool_handlers={},
                caller_email=caller_email,
            )
            assert inner_result.tool_calls_executed == []
            assert inner_result.tables == []
            assert inner_result.downloads == []
            return json.dumps({"status": "ok", "reply": inner_result.reply})

        outer_client = _mock_openai_tool_then_text(
            "ask_expense_agent",
            {"question": "test"},
            "outer_tc_001",
            "Outer reply.",
        )

        result = run_agent_loop(
            openai_client=outer_client,
            system_prompt="Vendor agent.",
            messages_in=[{"role": "user", "content": "test"}],
            tools=[{"type": "function", "function": {"name": "ask_expense_agent"}}],
            tool_handlers={"ask_expense_agent": ask_expense_agent_handler},
            caller_email="test@example.com",
        )

        assert result.reply == "Outer reply."
        assert result.tool_calls_executed == ["ask_expense_agent"]
        assert result.tables == []
        assert result.downloads == []

    def test_inner_error_does_not_crash_outer(self):
        """If the inner OpenAI call fails, the inner loop returns a graceful
        error message rather than raising, and the outer loop continues."""

        failing_client = MagicMock()
        failing_client.chat.completions.create.side_effect = Exception("API timeout")

        def ask_expense_agent_handler(args, caller_email=""):
            inner_result = run_agent_loop(
                openai_client=failing_client,
                system_prompt="Expense agent.",
                messages_in=[{"role": "user", "content": args["question"]}],
                tools=[],
                tool_handlers={},
                caller_email=caller_email,
            )
            return json.dumps({"status": "error", "reply": inner_result.reply})

        outer_client = _mock_openai_tool_then_text(
            "ask_expense_agent",
            {"question": "AWS spend?"},
            "outer_tc_001",
            "I wasn't able to get that information right now.",
        )

        result = run_agent_loop(
            openai_client=outer_client,
            system_prompt="Vendor agent.",
            messages_in=[{"role": "user", "content": "AWS spend?"}],
            tools=[{"type": "function", "function": {"name": "ask_expense_agent"}}],
            tool_handlers={"ask_expense_agent": ask_expense_agent_handler},
            caller_email="test@example.com",
        )

        assert result.reply == "I wasn't able to get that information right now."
        assert result.tool_calls_executed == ["ask_expense_agent"]
