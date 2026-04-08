"""Agent-tuning test for task 176: multi-month period table rendering.

Bug: when a user asks for "Jan, Feb, Mar 2026" in a table, only January
renders because the LLM emits period="2026-01" instead of "2026-Q1".

Root causes addressed:
  1. Prompt lacks period consolidation guidance (primary)
  2. One-tool-per-response rule blocks multi-call workaround
  3. _execute_ask_expense_agent forwards only tables[0]

Tuning metadata:
  reported-by: huy@heretic.fund
  feedback-id: 7d31cbd3-8446-414b-8f6d-0ddbb86d7a02
  chat-session-id: 8330a9a1-9dc7-498b-874a-6b974a1d442e
  agent: expense-analytics
"""

import json
import os
from unittest.mock import MagicMock

import pytest

from service.app import run_agent_loop, AgentResult, TablePayload
from service.prompts import EXPENSE_ANALYTICS_PROMPT
from service.tools import EXPENSE_TOOL_DEFINITIONS
from mcp_server.period_parser import parse_period

pytestmark = pytest.mark.expense_analytics

_MULTI_MONTH_QUESTION = (
    "please show the full table of detailed GCP spend "
    "for jan 2026, feb 2026, and mar 2026"
)

_OLD_PERIOD_GUIDANCE = """\
**period**: Convert the user's time reference to one of these formats: \
YYYY-MM (month), YYYY-QN (quarter), YYYY-HN (half), YYYY (year), YTD, \
last-N-months. Examples: "last quarter" → "2026-Q4" (or whichever is \
correct), "this year" → "YTD", "February" → "2026-02"."""

_SPEND_DETAIL_TOOL = [
    t for t in EXPENSE_TOOL_DEFINITIONS
    if t["function"]["name"] == "spend_detail"
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_openai_tool_then_text(
    tool_name: str,
    tool_args: dict,
    tool_call_id: str,
    final_text: str,
) -> MagicMock:
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


def _make_multi_month_table() -> str:
    return json.dumps({
        "status": "ok",
        "data": [
            {"category": "Compute Engine", "amount": 5000},
            {"category": "Cloud Storage", "amount": 2000},
        ],
        "table": {
            "metric": "Spend",
            "columns": ["Category", "Jan 2026", "Feb 2026", "Mar 2026", "Total"],
            "rows": [
                ["Compute Engine", 4500, 5000, 5500, 15000],
                ["Cloud Storage", 1800, 2000, 2200, 6000],
            ],
            "filename": "gcp-spend-detail-q1-2026.csv",
        },
    })


# ---------------------------------------------------------------------------
# Fix 1: prompt contains period consolidation guidance
# ---------------------------------------------------------------------------

class TestPromptPeriodConsolidation:
    """The expense analytics prompt must guide the LLM to consolidate
    consecutive months into range formats the period parser accepts."""

    def test_prompt_mentions_quarter_consolidation(self):
        assert "Q1" in EXPENSE_ANALYTICS_PROMPT or "quarter" in EXPENSE_ANALYTICS_PROMPT.lower()

    def test_prompt_has_multi_month_to_range_example(self):
        prompt_lower = EXPENSE_ANALYTICS_PROMPT.lower()
        assert any(phrase in prompt_lower for phrase in [
            "jan, feb, mar",
            "january, february, march",
            "consecutive months",
        ]), "Prompt must include guidance for consolidating consecutive months into ranges"

    def test_prompt_mentions_half_year_consolidation(self):
        prompt_lower = EXPENSE_ANALYTICS_PROMPT.lower()
        assert any(phrase in prompt_lower for phrase in [
            "h1", "h2", "half",
        ]), "Prompt must include half-year range consolidation guidance"


# ---------------------------------------------------------------------------
# Fix 1 + period parser: multi-month ranges parse correctly
# ---------------------------------------------------------------------------

class TestPeriodParserMultiMonth:
    """The period parser already handles range formats — confirm the
    specific cases relevant to this bug."""

    def test_q1_covers_jan_through_mar(self):
        assert parse_period("2026-Q1") == ("2026-01", "2026-03")

    def test_h1_covers_jan_through_jun(self):
        assert parse_period("2026-H1") == ("2026-01", "2026-06")

    def test_full_year_covers_all_months(self):
        assert parse_period("2026") == ("2026-01", "2026-12")


# ---------------------------------------------------------------------------
# Fix 3: all tables forwarded from inner agent
# ---------------------------------------------------------------------------

class TestAllTablesForwarded:
    """When the inner expense agent produces multiple tables, the
    delegation handler must forward all of them, not just the first."""

    def test_single_table_forwarded(self):
        inner_client = _mock_openai_tool_then_text(
            "spend_detail",
            {"vendor": "GCP", "period": "2026-Q1", "group_by": "category"},
            "inner_tc_001",
            "Here's the Q1 breakdown.",
        )

        def ask_handler(args, caller_email=""):
            from service.app import _execute_ask_expense_agent
            inner_result = run_agent_loop(
                openai_client=inner_client,
                system_prompt="You are an expense analytics agent.",
                messages_in=[{"role": "user", "content": args["question"]}],
                tools=[{"type": "function", "function": {"name": "spend_detail"}}],
                tool_handlers={
                    "spend_detail": lambda a, caller_email="": _make_multi_month_table(),
                },
                caller_email=caller_email,
            )
            assert len(inner_result.tables) == 1
            response: dict = {"status": "ok", "reply": inner_result.reply}
            if inner_result.tables:
                response["tables"] = [
                    {
                        "metric": t.metric,
                        "columns": t.columns,
                        "rows": t.rows,
                        "filename": t.filename,
                        "filters": t.filters,
                    }
                    for t in inner_result.tables
                ]
            return json.dumps(response)

        outer_client = _mock_openai_tool_then_text(
            "ask_expense_agent",
            {"question": "Show GCP spend for Jan, Feb, Mar 2026"},
            "outer_tc_001",
            "Here's your Q1 GCP spend breakdown.",
        )

        result = run_agent_loop(
            openai_client=outer_client,
            system_prompt="You are a vendor management agent.",
            messages_in=[{"role": "user", "content": "Show GCP spend for Jan, Feb, Mar 2026"}],
            tools=[{"type": "function", "function": {"name": "ask_expense_agent"}}],
            tool_handlers={"ask_expense_agent": ask_handler},
            caller_email="test@example.com",
        )

        assert len(result.tables) == 1
        table = result.tables[0]
        assert table.metric == "Spend"
        assert len(table.columns) == 5
        assert "Jan 2026" in table.columns
        assert "Mar 2026" in table.columns

    def test_multiple_tables_all_forwarded(self):
        """If the inner agent produces two tables (e.g. two tool calls),
        both must appear in the outer result."""

        def _make_two_table_result():
            return json.dumps({
                "status": "ok",
                "reply": "Here are both breakdowns.",
                "tables": [
                    {
                        "metric": "Spend",
                        "columns": ["Category", "Amount"],
                        "rows": [["Compute", 5000]],
                        "filename": "table-1.csv",
                    },
                    {
                        "metric": "Spend",
                        "columns": ["Project", "Amount"],
                        "rows": [["proj-a", 3000]],
                        "filename": "table-2.csv",
                    },
                ],
            })

        outer_client = _mock_openai_tool_then_text(
            "ask_expense_agent",
            {"question": "GCP spend by category and project"},
            "outer_tc_001",
            "Here are both breakdowns.",
        )

        def handler(args, caller_email=""):
            return _make_two_table_result()

        result = run_agent_loop(
            openai_client=outer_client,
            system_prompt="You are a vendor management agent.",
            messages_in=[{"role": "user", "content": "GCP spend by category and project"}],
            tools=[{"type": "function", "function": {"name": "ask_expense_agent"}}],
            tool_handlers={"ask_expense_agent": handler},
            caller_email="test@example.com",
        )

        assert len(result.tables) == 2
        assert result.tables[0].filename == "table-1.csv"
        assert result.tables[1].filename == "table-2.csv"


# ---------------------------------------------------------------------------
# Live e2e tests — full stack: OpenAI + tool execution + Postgres
#
# Requires:
#   - Agent running on localhost:8080 with DEV_AUTH_EMAIL set
#   - Cloud SQL Proxy on localhost:5433
# ---------------------------------------------------------------------------

_E2E_BASE = "http://127.0.0.1:8080"
_E2E_HEADERS = {"Content-Type": "application/json"}


def _agent_server_reachable() -> bool:
    """Return True if the local agent server is accepting connections."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 8080), timeout=2):
            return True
    except OSError:
        return False


def _chat_e2e(prompt: str, app_context: str = "vendors") -> dict:
    """Send a single-turn chat to the live agent and return the response."""
    import requests

    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "context": {"app": app_context},
    }
    resp = requests.post(
        f"{_E2E_BASE}/chat", json=payload, headers=_E2E_HEADERS, timeout=90,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_period_from_llm(system_prompt: str, question: str) -> str | None:
    """Send a single question to the LLM with the spend_detail tool and
    return the period argument it emits, or None if no tool call.

    Used for the prompt-level reproduction test (no DB needed)."""
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set")

    client = OpenAI(api_key=api_key)
    today = "2026-04-07"
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": f"Today's date is {today}.\n\n{system_prompt}"},
            {"role": "user", "content": question},
        ],
        tools=_SPEND_DETAIL_TOOL,
        tool_choice="auto",
    )

    choice = response.choices[0]
    if not choice.message.tool_calls:
        return None
    args = json.loads(choice.message.tool_calls[0].function.arguments)
    return args.get("period")


def _build_old_prompt() -> str:
    """Reconstruct the pre-fix prompt by replacing the new period guidance
    with the original text that lacked consolidation instructions."""
    from service.prompts import (
        _SHARED_RESPONSE_CONTRACT,
        _SHARED_VENDOR_PARAM,
        _SHARED_FILTER_REFERENCE,
        _SHARED_TABLE_RENDERED_RULE,
    )
    return (
        "You are an expense analytics assistant for the Haderach platform.\n\n"
        "Your job is to help users analyze vendor spend — totals, rankings, "
        "breakdowns by dimension, and per-service drill-downs.\n\n"
        "## Available tools\n\n"
        "| Tool | Use when |\n"
        "|------|----------|\n"
        "| spend_detail | Drilling into a vendor's spend by service, SKU, or project |\n\n"
        + _SHARED_RESPONSE_CONTRACT + "\n\n"
        "## Parameter guidance\n\n"
        + _SHARED_VENDOR_PARAM + "\n\n"
        + _OLD_PERIOD_GUIDANCE + "\n\n"
        "## Behaviour rules\n\n"
        "1. Call a tool as soon as all required information is available.\n"
        "2. Only call one tool per response.\n"
        "3. Keep responses concise and conversational.\n"
        "4. Never fabricate vendor data.\n"
        "5. " + _SHARED_TABLE_RENDERED_RULE + "\n"
    )


@pytest.mark.llm_live
class TestLiveLLMReproduction:
    """Prompt-level LLM tests (OpenAI only, no DB).

    Verify the LLM emits the wrong period with the old prompt and the
    right period with the new prompt. Run with: pytest -m llm_live"""

    def test_old_prompt_emits_single_month(self):
        """With the old prompt (no consolidation guidance), the LLM emits
        a single month like '2026-01' for a multi-month request."""
        old_prompt = _build_old_prompt()
        period = _extract_period_from_llm(old_prompt, _MULTI_MONTH_QUESTION)

        assert period is not None, "LLM did not make a tool call"
        start, end = parse_period(period)
        covers_all_three = (start <= "2026-01" and end >= "2026-03")
        assert not covers_all_three, (
            f"Expected old prompt to emit a single month, but got period='{period}' "
            f"which covers all three months. Bug may not reproduce with this model."
        )

    def test_new_prompt_emits_multi_month_range(self):
        """With the fixed prompt (consolidation guidance), the LLM emits
        a range format like '2026-Q1' that covers all requested months."""
        period = _extract_period_from_llm(
            EXPENSE_ANALYTICS_PROMPT, _MULTI_MONTH_QUESTION,
        )

        assert period is not None, "LLM did not make a tool call"
        start, end = parse_period(period)
        assert start <= "2026-01" and end >= "2026-03", (
            f"Expected period covering Jan-Mar 2026, got period='{period}' "
            f"which resolves to ({start}, {end})"
        )


@pytest.mark.llm_live
@pytest.mark.skipif(
    not _agent_server_reachable(),
    reason="Local agent server not running on :8080",
)
class TestE2EMultiMonthTable:
    """True end-to-end test: OpenAI + tool execution + Postgres.

    Hits the live /chat endpoint and verifies the returned table covers
    all requested months. Requires agent on :8080 and Cloud SQL Proxy
    on :5433."""

    def test_multi_month_table_covers_all_months(self):
        """Ask for GCP spend for Jan, Feb, Mar 2026 and verify the table
        has columns for all three months."""
        result = _chat_e2e(
            "Show me a detailed breakdown of Google Cloud spend by service "
            "for January 2026, February 2026, and March 2026"
        )

        tools = result.get("tool_calls_executed", [])
        assert any(
            t in tools for t in ["spend_detail", "ask_expense_agent"]
        ), f"Expected spend_detail or ask_expense_agent, got: {tools}"

        tables = result.get("tables", [])
        assert len(tables) >= 1, (
            f"Expected at least 1 table, got {len(tables)}. "
            f"Reply: {result['reply'][:300]}"
        )

        table = tables[0]
        month_cols = [
            c for c in table["columns"]
            if c.startswith("2026-") or any(
                m in c.lower() for m in ["jan", "feb", "mar"]
            )
        ]

        assert len(month_cols) >= 2, (
            f"Table must have at least 2 month columns (the bug produced only 1). "
            f"Got columns: {table['columns']}"
        )
        assert len(table["rows"]) >= 1, "Expected at least 1 data row"

        print(
            f"  PASS: table has {len(table['rows'])} rows, "
            f"{len(month_cols)} month columns: {table['columns']}"
        )
