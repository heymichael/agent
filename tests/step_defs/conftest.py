"""Shared step definitions available to all BDD test files in step_defs/.

Steps defined here are automatically discovered by pytest-bdd for any
test module under tests/step_defs/.
"""

from pytest_bdd import given, when, then, parsers

from tests.conftest import chat, scoped_chat


# ── Given ────────────────────────────────────────────────────────────────


@given("test vendors exist")
def given_test_vendors(test_vendor_ids):
    assert len(test_vendor_ids) >= 6


@given("ACL test vendors exist")
def given_acl_vendors(acl_vendor_ids):
    assert len(acl_vendor_ids) >= 2


# ── When — generic chat ─────────────────────────────────────────────────


@when(
    parsers.parse('the user says "{prompt}"'),
    target_fixture="context",
)
def user_says(prompt):
    return {"result": chat(prompt)}


@when(
    parsers.parse('the scoped user says "{prompt}"'),
    target_fixture="context",
)
def scoped_user_says(prompt):
    return {"result": scoped_chat(prompt)}


# ── Then — tool assertions ──────────────────────────────────────────────


@then(parsers.parse('the agent calls "{tool}"'))
def assert_tool_called(context, tool):
    tools = context["result"].get("tool_calls_executed", [])
    assert tool in tools, (
        f"Expected {tool} in tool_calls_executed, got: {tools}. "
        f"Reply: {context['result']['reply'][:300]}"
    )


# ── Then — pending action assertions ────────────────────────────────────


@then(parsers.parse('the agent returns a "{action_type}" pending action'))
def assert_pending_action(context, action_type):
    result = context["result"]
    actions = result.get("pending_actions", [])
    assert any(a["type"] == action_type for a in actions), (
        f"Expected {action_type} action, got: {actions}. Reply: {result['reply']}"
    )
    context["batch"] = next(a for a in actions if a["type"] == action_type)


@then("no pending action is returned")
def assert_no_pending_action(context):
    result = context["result"]
    assert not result.get("pending_actions"), (
        f"Expected no pending actions, got: {result.get('pending_actions')}"
    )


# ── Then — reply text assertions ────────────────────────────────────────


@then(parsers.parse('the reply mentions "{keyword}"'))
def assert_reply_mentions(context, keyword):
    reply = context["result"]["reply"].lower()
    assert keyword.lower() in reply, (
        f"Expected '{keyword}' in reply, got: {reply[:300]}"
    )


@then("the reply mentions a UUID or format error")
def assert_reply_uuid_error(context):
    reply = context["result"]["reply"].lower()
    assert any(w in reply for w in ["uuid", "valid", "format"]), (
        f"Expected UUID/format error in reply, got: {reply[:300]}"
    )


@then("the reply mentions a not-found error")
def assert_reply_not_found(context):
    reply = context["result"]["reply"].lower()
    assert any(w in reply for w in [
        "not found", "couldn't find", "could not find", "couldn't",
        "can't find", "cannot find", "no vendor", "doesn't exist",
        "does not exist", "was not", "isn't", "not",
    ]), (
        f"Expected not-found error in reply, got: {reply[:300]}"
    )


@then("the reply mentions a missing ID error")
def assert_reply_missing_id(context):
    reply = context["result"]["reply"].lower()
    assert any(w in reply for w in ["id", "missing", "empty"]), (
        f"Expected missing-ID error in reply, got: {reply[:300]}"
    )


# ── Then — CSV download assertions ──────────────────────────────────────


@then("the response includes a CSV download")
def assert_csv_download(context):
    downloads = context["result"].get("downloads", [])
    assert len(downloads) >= 1, (
        f"Expected CSV download, got none. Reply: {context['result']['reply'][:300]}"
    )


@then("the response does not include a CSV download")
def assert_no_csv_download(context):
    downloads = context["result"].get("downloads", [])
    assert len(downloads) == 0, (
        f"Expected no downloads, got {len(downloads)}"
    )


# ── Then — denial / filter refusal assertions ───────────────────────────


@then("the reply does not refuse the filter")
def assert_no_filter_refusal(context):
    reply = context["result"]["reply"].lower()
    refusals = [
        "cannot filter", "don't support", "unable to filter", "no filter",
        "wildcard", "specify which",
    ]
    assert not any(p in reply for p in refusals), (
        f"Model refused filter. Reply: {context['result']['reply'][:300]}"
    )


@then("the reply mentions denial")
def assert_denial_mentioned(context):
    reply = context["result"]["reply"].lower()
    assert any(w in reply for w in [
        "not authorized", "permission", "don't have", "cannot edit", "not allowed",
    ]), f"Expected denial message. Reply: {context['result']['reply'][:300]}"


@then("the reply does not mention denial")
def assert_no_denial(context):
    reply = context["result"]["reply"].lower()
    assert "not authorized" not in reply and "permission" not in reply, (
        f"Should NOT be denied. Reply: {context['result']['reply'][:300]}"
    )
