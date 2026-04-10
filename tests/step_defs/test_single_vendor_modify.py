"""Step definitions for single-vendor modify scenarios (1–12).

Tests modify_vendor with real production vendors — no test fixture needed.
Most When/Then steps are inherited from the shared conftest. This file
contains only the modify-specific multi-turn and assertion steps.
"""

from pytest_bdd import scenarios, when, then

from tests.conftest import chat

scenarios("../features/single_vendor_modify.feature")


# ── When — multi-step scenarios ──────────────────────────────────────────


@when("the reply contains disambiguation candidates")
def check_disambiguation(context):
    """Verify turn 1 returned disambiguation info and extract candidates."""
    result = context["result"]
    reply = result["reply"].lower()
    actions = result.get("pending_actions", [])

    has_disambig = (
        "which" in reply
        or "multiple" in reply
        or "disambigu" in reply
        or any(a.get("type") == "disambiguate" for a in actions)
    )
    if not has_disambig:
        import pytest
        pytest.skip("Agent resolved without disambiguation — vendor may no longer be ambiguous")


@when("the user re-sends with the first candidate UUID")
def resend_with_uuid(context):
    """Extract the first UUID from the disambiguation response and re-send."""
    import re
    result = context["result"]
    reply = result["reply"]

    uuids = re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        reply,
        re.IGNORECASE,
    )
    assert uuids, f"No UUID found in disambiguation reply: {reply[:300]}"
    context["result"] = chat(
        f"Use vendor {uuids[0]}, set department to Administration"
    )


@when("the edit pending action contains a vendor UUID")
def extract_edit_uuid(context):
    """Extract the vendor UUID from the confirm_edit pending action."""
    result = context["result"]
    actions = result.get("pending_actions", [])
    edit = next((a for a in actions if a["type"] == "confirm_edit"), None)
    assert edit, f"Expected confirm_edit action, got: {actions}"
    vendor_id = edit.get("vendor_id") or edit.get("vendorId")
    assert vendor_id, f"No vendor ID in confirm_edit action: {edit}"
    context["extracted_uuid"] = vendor_id


@when("the user re-sends with that UUID")
def resend_with_extracted_uuid(context):
    """Re-send modify request using the extracted UUID."""
    uuid = context["extracted_uuid"]
    context["result"] = chat(f"Set vendor {uuid} department to Marketing")


# ── Then — modify-specific assertions ────────────────────────────────────


@then("the reply does not list fabricated departments")
def assert_no_hallucinated_departments(context):
    reply = context["result"]["reply"].lower()
    fabricated = ["consulting", "operations", "logistics", "research"]
    found = [d for d in fabricated if d in reply]
    assert not found, (
        f"Reply may contain fabricated departments: {found}. "
        f"Reply: {context['result']['reply'][:300]}"
    )


@then("the reply acknowledges both vendors")
def assert_both_vendors_acknowledged(context):
    """Agent should either process both or ask which to start with."""
    result = context["result"]
    reply = result["reply"].lower()
    actions = result.get("pending_actions", [])

    processed_both = len(actions) >= 2
    asks_which = any(w in reply for w in ["which", "one at a time", "start with"])
    mentions_both = "b. on the go" in reply and "cheese plus" in reply

    assert processed_both or asks_which or mentions_both, (
        f"Expected agent to acknowledge both vendors. "
        f"Actions: {len(actions)}, Reply: {reply[:300]}"
    )


@then("the reply denies deletion")
def assert_deletion_denied(context):
    reply = context["result"]["reply"].lower()
    assert any(w in reply for w in [
        "not available", "cannot delete", "can't delete",
        "not supported", "admin", "not able",
    ]), f"Expected deletion denial. Reply: {context['result']['reply'][:300]}"
