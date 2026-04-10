"""Step definitions for multi-turn count-then-list scenarios.

Implements the two-turn flow: send a count query, capture tool_messages,
then replay them in a follow-up that requests a CSV download.
"""

import requests

from pytest_bdd import scenarios, when, parsers

from tests.conftest import BASE_URL, DEFAULT_HEADERS, chat_multi_turn

scenarios("../features/multi_turn.feature")


# ── When — multi-turn steps ──────────────────────────────────────────────


@when(
    parsers.parse('the user asks "{prompt}"'),
    target_fixture="context",
)
def user_asks_count(prompt):
    """Turn 1: send the count query and store the full response."""
    from tests.conftest import chat
    result = chat(prompt)
    tools = result.get("tool_calls_executed", [])
    assert "vendor_count" in tools, (
        f"Turn 1 should call vendor_count, got: {tools}. "
        f"Reply: {result['reply'][:300]}"
    )
    return {"turn1": result, "turn1_prompt": prompt}


@when(parsers.parse('the user follows up with "{follow_up}"'))
def user_follows_up(context, follow_up):
    """Turn 2: replay tool_messages from turn 1 and send the follow-up."""
    turn1 = context["turn1"]
    prompt1 = context["turn1_prompt"]

    tool_msgs = turn1.get("tool_messages", [])
    assert len(tool_msgs) >= 2, (
        f"Expected tool_messages from turn 1, got {len(tool_msgs)}"
    )

    messages = [{"role": "user", "content": prompt1}]
    for tm in tool_msgs:
        msg = {"role": tm["role"]}
        if tm.get("content") is not None:
            msg["content"] = tm["content"]
        if tm.get("tool_calls"):
            msg["tool_calls"] = tm["tool_calls"]
        if tm.get("tool_call_id"):
            msg["tool_call_id"] = tm["tool_call_id"]
        messages.append(msg)
    messages.append({"role": "assistant", "content": turn1["reply"]})
    messages.append({"role": "user", "content": follow_up})

    result = chat_multi_turn(messages)
    context["result"] = result
