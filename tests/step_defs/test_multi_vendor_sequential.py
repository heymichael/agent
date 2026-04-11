"""Step definitions for multi-vendor sequential modification scenarios (51–62).

Tests sequential modify_vendor calls for 2–5 named vendors, the CSV
threshold at 6 vendors, and explicit CSV generation requests.

Most When/Then steps are inherited from the shared conftest.
"""

from pytest_bdd import scenarios, then, parsers

scenarios("../features/multi_vendor_sequential.feature")


# ── Then — multi-vendor-specific assertions ──────────────────────────────


@then("the reply suggests CSV workflow")
def assert_csv_redirect(context):
    reply = context["result"]["reply"].lower()
    assert any(w in reply for w in [
        "csv", "spreadsheet", "bulk", "upload",
    ]), f"Expected CSV redirect suggestion. Reply: {context['result']['reply'][:300]}"


@then(parsers.parse('the agent does not call "{tool}"'))
def assert_tool_not_called(context, tool):
    tools = context["result"].get("tool_calls_executed", [])
    assert tool not in tools, (
        f"Expected {tool} NOT in tool_calls_executed, but found it. Tools: {tools}"
    )


@then("the agent returns a CSV download or calls modify_vendor")
def assert_csv_or_modify(context):
    downloads = context["result"].get("downloads", [])
    tools = context["result"].get("tool_calls_executed", [])
    has_csv = len(downloads) >= 1
    has_modify = "modify_vendor" in tools
    assert has_csv or has_modify, (
        f"Expected CSV download or modify_vendor call. "
        f"Downloads: {len(downloads)}, Tools: {tools}. "
        f"Reply: {context['result']['reply'][:300]}"
    )


@then("the reply reports not-found vendors")
def assert_not_found_reported(context):
    reply = context["result"]["reply"].lower()
    assert any(w in reply for w in [
        "not found", "couldn't find", "could not find",
        "unable to find", "no vendor", "doesn't exist",
        "does not exist", "don't recognize", "unrecognized",
        "no matching", "no match", "isn't a match", "is no match",
        "can't find", "cannot find",
        "wasn't found", "was not found", "weren't found",
        "invalid vendor", "fix the name",
    ]), f"Expected not-found report. Reply: {context['result']['reply'][:300]}"
