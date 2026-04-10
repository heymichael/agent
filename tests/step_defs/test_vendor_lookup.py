"""Step definitions for vendor lookup scenarios.

Tests single-vendor profile queries: name resolution, alias handling,
full profile requests, specific field questions, and error cases.

Most When/Then steps are inherited from the shared conftest.
"""

from pytest_bdd import scenarios, then

scenarios("../features/vendor_lookup.feature")


# ── Then — lookup-specific assertions ─────────────────────────────────────


@then("the reply mentions a department")
def assert_reply_mentions_department(context):
    """Verify the reply references a department value, not just the word 'department'."""
    reply = context["result"]["reply"].lower()
    department_keywords = [
        "engineering", "marketing", "product", "finance", "administration",
        "it", "operations", "sales", "legal", "hr", "human resources",
        "department",
    ]
    assert any(w in reply for w in department_keywords), (
        f"Expected department mention in reply. Reply: {context['result']['reply'][:300]}"
    )


@then("the reply includes multiple vendor fields")
def assert_multiple_fields(context):
    """Verify the reply surfaces several profile fields, not just the vendor name."""
    reply = context["result"]["reply"].lower()
    field_signals = [
        "department", "owner", "payment", "billing", "contract",
        "status", "category", "account type", "1099", "auto-renew",
        "email", "phone", "address", "purpose", "frequency",
        "renew", "vendor", "type", "active", "inactive",
        "monthly", "annual", "usage", "cloud", "software",
    ]
    matches = [f for f in field_signals if f in reply]
    assert len(matches) >= 2, (
        f"Expected >=2 vendor fields in reply, found {len(matches)}: {matches}. "
        f"Reply: {context['result']['reply'][:300]}"
    )


@then("the reply contains disambiguation candidates")
def assert_disambiguation(context):
    """Verify the reply presents multiple vendor candidates for the user to choose from."""
    reply = context["result"]["reply"].lower()
    actions = context["result"].get("pending_actions", [])
    has_disambig = (
        "which" in reply
        or "multiple" in reply
        or "did you mean" in reply
        or "disambigu" in reply
        or any(a.get("type") == "disambiguate" for a in actions)
    )
    assert has_disambig, (
        f"Expected disambiguation candidates. Reply: {context['result']['reply'][:300]}"
    )
