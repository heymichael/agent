"""BDD step definitions for CMS schema design scenarios.

These tests validate the CMS admin agent's ability to create and refine
content type definitions through natural language conversation.
"""

import json
import uuid
from unittest.mock import patch, MagicMock

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from tests.conftest import cms_chat, BASE_URL

import requests

scenarios("../features/cms_schema_design.feature")


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def cms_session_id():
    """Generate a unique session ID for CMS multi-turn tests."""
    return f"cms-test-{uuid.uuid4()}"


@pytest.fixture
def draft_content_type_id():
    """Create a draft content type for testing and clean up after."""
    # This fixture creates a draft content type via the API and yields its ID
    # For now, we'll mock the setup in the step definition
    return None


# ── Given ───────────────────────────────────────────────────────────────────


@given(parsers.parse('a draft content type "{name}" exists'))
def given_draft_content_type(context, name):
    """Ensure a draft content type exists by creating one via the agent."""
    # Create a simple content type to work with
    result = cms_chat(f"Create a {name} collection with title and description")
    context["setup_result"] = result
    context["content_type_name"] = name
    # Extract the content type ID from the tool response if available
    for msg in result.get("tool_messages", []):
        if msg.get("role") == "tool":
            try:
                parsed = json.loads(msg.get("content", "{}"))
                if parsed.get("status") == "ok" and "contentType" in parsed:
                    context["content_type_id"] = parsed["contentType"]["id"]
                    break
            except json.JSONDecodeError:
                pass


# ── When ────────────────────────────────────────────────────────────────────


@when(
    parsers.parse('the CMS admin says "{prompt}"'),
    target_fixture="context",
)
def cms_admin_says(prompt, context=None):
    """Send a message to the CMS admin agent."""
    result = cms_chat(prompt, mode="admin")
    ctx = context if context is not None else {}
    ctx["result"] = result
    return ctx


# ── Then ────────────────────────────────────────────────────────────────────


@then(parsers.parse('the agent calls "{tool}"'))
def assert_tool_called(context, tool):
    """Assert the agent called the specified tool."""
    tools = context["result"].get("tool_calls_executed", [])
    assert tool in tools, (
        f"Expected {tool} in tool_calls_executed, got: {tools}. "
        f"Reply: {context['result']['reply'][:500]}"
    )


@then(parsers.parse('the agent calls "{tool}" or asks for clarification'))
def assert_tool_called_or_clarification(context, tool):
    """Assert the agent either called the tool or asked for clarification (valid consultative behavior)."""
    tools = context["result"].get("tool_calls_executed", [])
    reply = context["result"]["reply"].lower()

    # Check if tool was called
    if tool in tools:
        return

    # Check if agent asked for clarification (valid behavior when multiple content types exist)
    clarification_phrases = [
        "which one",
        "which content type",
        "clarify",
        "multiple",
        "referring to",
        "do you mean",
        "please confirm",
        "switch to",
        "set it active",
        "provide its id",
    ]
    asked_clarification = any(phrase in reply for phrase in clarification_phrases)

    assert asked_clarification, (
        f"Expected {tool} in tool_calls_executed or clarification request. "
        f"Got tools: {tools}. Reply: {reply[:500]}"
    )


@then(parsers.parse('the agent calls "{tool}" or proposes the change'))
def assert_tool_called_or_proposal(context, tool):
    """Assert the agent either called the tool, proposed the change, or asked for clarification."""
    tools = context["result"].get("tool_calls_executed", [])
    reply = context["result"]["reply"].lower()

    # Check if tool was called
    if tool in tools:
        return

    # Check if agent proposed the change (valid consultative behavior)
    proposal_phrases = [
        "i'll go ahead",
        "i will add",
        "does that work",
        "shall i",
        "would you like me to",
        "i can add",
        "let me add",
        "i'll add",
    ]
    if any(phrase in reply for phrase in proposal_phrases):
        return

    # Check if agent asked for clarification (valid when multiple content types exist)
    clarification_phrases = [
        "which one",
        "which content type",
        "which should i",
        "please clarify",
        "multiple",
    ]
    asked_clarification = any(phrase in reply for phrase in clarification_phrases)

    assert asked_clarification, (
        f"Expected {tool} in tool_calls_executed, proposal, or clarification. "
        f"Got tools: {tools}. Reply: {reply[:500]}"
    )


@then(parsers.parse('the agent does not call "{tool}"'))
def assert_tool_not_called(context, tool):
    """Assert the agent did NOT call the specified tool."""
    tools = context["result"].get("tool_calls_executed", [])
    assert tool not in tools, (
        f"Expected {tool} NOT in tool_calls_executed, but it was. "
        f"Tools called: {tools}"
    )


@then("the tool response indicates success")
def assert_tool_response_success(context):
    """Assert the tool response has status: ok and no validation errors.
    
    This verifies the LLM generated a valid schema that passed validation.
    """
    tool_messages = context["result"].get("tool_messages", [])
    
    # Find tool response messages
    found_success = False
    validation_errors = []
    
    for msg in tool_messages:
        if msg.get("role") == "tool":
            content = msg.get("content", "{}")
            try:
                parsed = json.loads(content)
                if parsed.get("status") == "ok":
                    found_success = True
                elif parsed.get("status") == "error":
                    validation_errors.append(parsed.get("message", "Unknown error"))
                    if "errors" in parsed:
                        validation_errors.extend(parsed["errors"])
            except json.JSONDecodeError:
                pass
    
    assert found_success, (
        f"Expected tool response with status: ok. "
        f"Validation errors: {validation_errors}. "
        f"Tool messages: {[m.get('content', '')[:200] for m in tool_messages if m.get('role') == 'tool']}"
    )


@then(parsers.parse('the reply mentions "{keyword}"'))
def assert_reply_mentions(context, keyword):
    """Assert the reply contains the specified keyword (case-insensitive)."""
    reply = context["result"]["reply"].lower()
    assert keyword.lower() in reply, (
        f"Expected '{keyword}' in reply, got: {reply[:500]}"
    )


@then(parsers.re(r'the reply mentions "(?P<keyword1>[^"]+)" or "(?P<keyword2>[^"]+)" or asks for clarification'))
def assert_reply_mentions_either_or_clarification(context, keyword1, keyword2):
    """Assert the reply contains either keyword or asks for clarification."""
    reply = context["result"]["reply"].lower()
    
    # Check for keywords
    if keyword1.lower() in reply or keyword2.lower() in reply:
        return
    
    # Check for clarification phrases
    clarification_phrases = [
        "which one",
        "which content type",
        "which should i modify",
        "please clarify",
    ]
    asked_clarification = any(phrase in reply for phrase in clarification_phrases)
    
    assert asked_clarification, (
        f"Expected '{keyword1}' or '{keyword2}' or clarification in reply, got: {reply[:500]}"
    )


@then(parsers.re(r'the reply mentions "(?P<keyword1>[^"]+)" or "(?P<keyword2>[^"]+)"$'))
def assert_reply_mentions_either(context, keyword1, keyword2):
    """Assert the reply contains either keyword (case-insensitive)."""
    reply = context["result"]["reply"].lower()
    assert keyword1.lower() in reply or keyword2.lower() in reply, (
        f"Expected '{keyword1}' or '{keyword2}' in reply, got: {reply[:500]}"
    )


@then(parsers.re(r'the reply mentions "(?P<keyword>[^"]+)"$'))
def assert_reply_mentions(context, keyword):
    """Assert the reply contains the keyword (case-insensitive)."""
    reply = context["result"]["reply"].lower()
    assert keyword.lower() in reply, (
        f"Expected '{keyword}' in reply, got: {reply[:500]}"
    )


@then("the reply mentions relationship limitation")
def assert_reply_mentions_relationship_limitation(context):
    """Assert the reply explains relationship fields aren't available."""
    reply = context["result"]["reply"].lower()
    # Check for various ways the agent might explain the limitation
    limitation_phrases = [
        "relationship",
        "not supported",
        "not yet implemented",
        "not available",
        "coming soon",
        "future",
        "workaround",
    ]
    found = any(phrase in reply for phrase in limitation_phrases)
    assert found, (
        f"Expected explanation about relationship limitation in reply, got: {reply[:500]}"
    )


@then("the reply mentions image limitation")
def assert_reply_mentions_image_limitation(context):
    """Assert the reply explains image/media fields aren't available."""
    reply = context["result"]["reply"].lower()
    # Check for various ways the agent might explain the limitation
    limitation_phrases = [
        "image",
        "media",
        "can't",
        "cannot",
        "not support",
        "not yet",
        "not available",
        "workaround",
        "url field",
    ]
    found = any(phrase in reply for phrase in limitation_phrases)
    assert found, (
        f"Expected explanation about image limitation in reply, got: {reply[:500]}"
    )


@then("the reply does not call tool with image field")
def assert_no_image_field_in_tool_call(context):
    """Assert that if a tool was called, it didn't include an image field type."""
    tool_messages = context["result"].get("tool_messages", [])
    for msg in tool_messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                args = tc.get("function", {}).get("arguments", "{}")
                try:
                    parsed = json.loads(args)
                    schema = parsed.get("schema", [])
                    for field in schema:
                        assert field.get("type") != "image", (
                            f"Tool was called with image field type: {field}"
                        )
                except json.JSONDecodeError:
                    pass


@then("the schema uses snake_case names")
def assert_schema_uses_snake_case(context):
    """Assert that field names in the schema are snake_case (no spaces, lowercase)."""
    import re
    tool_messages = context["result"].get("tool_messages", [])
    for msg in tool_messages:
        if msg.get("role") == "tool":
            try:
                parsed = json.loads(msg.get("content", "{}"))
                if parsed.get("status") == "ok" and "contentType" in parsed:
                    schema = parsed["contentType"].get("schema", [])
                    if isinstance(schema, dict):
                        schema = schema.get("fields", [])
                    for field in schema:
                        name = field.get("name", "")
                        # snake_case: lowercase, underscores, no spaces
                        assert re.match(r"^[a-z][a-z0-9_]*$", name), (
                            f"Field name '{name}' is not snake_case"
                        )
            except json.JSONDecodeError:
                pass


@then("the reply does not mention education in current schema")
def assert_education_not_in_schema(context):
    """Assert that education field was removed from the schema."""
    tool_messages = context["result"].get("tool_messages", [])
    for msg in tool_messages:
        if msg.get("role") == "tool":
            try:
                parsed = json.loads(msg.get("content", "{}"))
                if parsed.get("status") == "ok" and "contentType" in parsed:
                    schema = parsed["contentType"].get("schema", [])
                    if isinstance(schema, dict):
                        schema = schema.get("fields", [])
                    field_names = [f.get("name", "").lower() for f in schema]
                    assert "education" not in field_names, (
                        f"Education field should have been deleted, but found in: {field_names}"
                    )
            except json.JSONDecodeError:
                pass


@then("the reply does not contain raw JSON schema")
def assert_no_raw_json_in_reply(context):
    """Assert the reply doesn't dump raw JSON schema - should be natural language."""
    reply = context["result"]["reply"]
    # Check for signs of raw JSON dump (multiple consecutive JSON-like lines)
    json_indicators = [
        '"name":',
        '"type":',
        '"required":',
        '{"name"',
        '[{"name"',
    ]
    json_count = sum(1 for indicator in json_indicators if indicator in reply)
    assert json_count < 3, (
        f"Reply appears to contain raw JSON schema (found {json_count} JSON indicators): {reply[:500]}"
    )


@then(parsers.parse('the agent does not call "{tool}" again'))
def assert_tool_not_called_again(context, tool):
    """Assert the agent only called the tool once (no looping)."""
    tools = context["result"].get("tool_calls_executed", [])
    tool_count = tools.count(tool)
    assert tool_count <= 1, (
        f"Expected {tool} to be called at most once, but was called {tool_count} times. "
        f"All tools: {tools}"
    )
