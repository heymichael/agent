"""Step definitions for spend query scenarios (13–18, 22–23, 25–26).

Tests spend detail discovery, drill-downs, summary tool selection,
empty dimension handling, and hidden vendor resolution.

Most When/Then steps are inherited from the shared conftest.
"""

from pytest_bdd import scenarios, then

scenarios("../features/spend_queries.feature")


# ── Then — spend-specific assertions ─────────────────────────────────────


@then("the reply gracefully explains no data is available")
def assert_graceful_empty(context):
    result = context["result"]
    reply = result["reply"].lower()

    tables = result.get("tables") or []
    empty_table = any(
        len(t.get("rows", [None])) == 0 for t in tables
    )

    text_signals = [
        "no ", "not available", "empty", "doesn't have",
        "don't have", "unavailable", "no data", "no project",
        "no categories", "no breakdown", "not populated",
        "couldn't find", "could not find", "no spend",
        "no record", "no expense", "no detail", "not found",
        "no information", "don't see", "doesn't appear",
    ]
    has_text_signal = any(w in reply for w in text_signals)

    assert has_text_signal or empty_table, (
        f"Expected graceful empty-data message or empty table. "
        f"Reply: {reply[:300]}"
    )


@then("the reply does not ask for disambiguation")
def assert_no_disambiguation(context):
    reply = context["result"]["reply"].lower()
    assert not any(w in reply for w in [
        "which one", "did you mean", "multiple vendors",
        "disambigu", "could you clarify",
    ]), f"Expected no disambiguation. Reply: {context['result']['reply'][:300]}"
