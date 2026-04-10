"""Step definitions for CSV bulk-edit scenarios.

Maps Gherkin steps from csv_bulk_edit.feature to the test helpers in
conftest.py. Each 'when' step builds a specific CSV, sends it through
chat_with_csv, and stores the response in the shared `context` fixture.

Shared Given/Then steps live in step_defs/conftest.py.
"""

import requests
from pytest_bdd import scenarios, when, then, parsers

from tests.conftest import (
    BASE_URL,
    DEFAULT_HEADERS,
    chat_with_csv,
    make_csv,
)

scenarios("../features/csv_bulk_edit.feature")

# ── When — success cases ─────────────────────────────────────────────────


@when(
    parsers.re(
        r'the user uploads a CSV changing department to "(?P<value>[^"]+)" '
        r'for "(?P<vendor_a>[^"]+)" and "(?P<vendor_b>[^"]+)"'
    ),
    target_fixture="context",
)
def upload_dept_csv_2(test_vendor_ids, value, vendor_a, vendor_b):
    csv = make_csv(
        ["id", "department"],
        [
            [test_vendor_ids[vendor_a], value],
            [test_vendor_ids[vendor_b], value],
        ],
    )
    return {"result": chat_with_csv(csv)}


@when(
    parsers.parse(
        'the user uploads a CSV changing department and billingFrequency for "{vendor}"'
    ),
    target_fixture="context",
)
def upload_multi_field_csv(test_vendor_ids, vendor):
    csv = make_csv(
        ["id", "department", "billingFrequency"],
        [[test_vendor_ids[vendor], "Finance", "annual"]],
    )
    return {"result": chat_with_csv(csv)}


@when(
    parsers.re(
        r'the user uploads a CSV changing purpose for "(?P<vendor_a>[^"]+)" and "(?P<vendor_b>[^"]+)"'
    ),
    target_fixture="context",
)
def upload_purpose_csv_2(test_vendor_ids, vendor_a, vendor_b):
    csv = make_csv(
        ["id", "purpose"],
        [
            [test_vendor_ids[vendor_a], "Updated accounting tools"],
            [test_vendor_ids[vendor_b], "New purpose for Foxtrot"],
        ],
    )
    return {"result": chat_with_csv(csv)}


@when(
    parsers.re(
        r'the user uploads a CSV changing department to "(?P<value>[^"]+)" for "(?P<vendor>[^"]+)"$'
    ),
    target_fixture="context",
)
def upload_dept_csv_1(test_vendor_ids, value, vendor):
    csv = make_csv(
        ["id", "department"],
        [[test_vendor_ids[vendor], value]],
    )
    return {"result": chat_with_csv(csv)}


@when(
    parsers.parse(
        'the user uploads a CSV with name and department columns for "{vendor}"'
    ),
    target_fixture="context",
)
def upload_name_dept_csv(test_vendor_ids, vendor):
    csv = make_csv(
        ["id", "name", "department"],
        [[test_vendor_ids[vendor], "RENAMED ALPHA", "Finance"]],
    )
    return {"result": chat_with_csv(csv)}


# ── When — column validation errors ──────────────────────────────────────


@when(
    parsers.parse(
        'the user uploads a CSV with columns "{columns}" for "{vendor}"'
    ),
    target_fixture="context",
)
def upload_csv_with_columns(test_vendor_ids, columns, vendor):
    cols = [c.strip() for c in columns.split(",")]
    row = [test_vendor_ids[vendor]] + ["Marketing"] * (len(cols) - 1)
    if "id" not in cols:
        row = ["Marketing"] * len(cols)
    csv = make_csv(cols, [row])
    return {"result": chat_with_csv(csv)}


# ── When — ID validation errors ──────────────────────────────────────────


@when(
    parsers.parse(
        'the user uploads a CSV with an extra-character UUID for "{vendor}"'
    ),
    target_fixture="context",
)
def upload_csv_extra_char_uuid(test_vendor_ids, vendor):
    bad_id = test_vendor_ids[vendor] + "f"
    csv = make_csv(["id", "department"], [[bad_id, "Marketing"]])
    return {"result": chat_with_csv(csv)}


@when(
    parsers.parse(
        'the user uploads a CSV with a truncated UUID for "{vendor}"'
    ),
    target_fixture="context",
)
def upload_csv_truncated_uuid(test_vendor_ids, vendor):
    bad_id = test_vendor_ids[vendor][:-4]
    csv = make_csv(["id", "department"], [[bad_id, "Marketing"]])
    return {"result": chat_with_csv(csv)}


@when(
    parsers.parse('the user uploads a CSV with UUID "{uuid}"'),
    target_fixture="context",
)
def upload_csv_specific_uuid(uuid):
    csv = make_csv(["id", "department"], [[uuid, "Marketing"]])
    return {"result": chat_with_csv(csv)}


@when(
    "the user uploads a CSV with an empty ID cell",
    target_fixture="context",
)
def upload_csv_empty_id():
    csv = make_csv(["id", "department"], [["", "Marketing"]])
    return {"result": chat_with_csv(csv)}


# ── When — value validation errors ───────────────────────────────────────


@when(
    parsers.parse(
        'the user uploads a CSV setting "{field}" to "{value}" for "{vendor}"'
    ),
    target_fixture="context",
)
def upload_csv_bad_value(test_vendor_ids, field, value, vendor):
    csv = make_csv(
        ["id", field],
        [[test_vendor_ids[vendor], value]],
    )
    return {"result": chat_with_csv(csv)}


# ── When — edge cases ────────────────────────────────────────────────────


@when(
    parsers.parse(
        'the user uploads a CSV with a UTF-8 BOM prefix for "{vendor}"'
    ),
    target_fixture="context",
)
def upload_csv_with_bom(test_vendor_ids, vendor):
    csv = "\ufeff" + make_csv(
        ["id", "department"],
        [[test_vendor_ids[vendor], "Marketing"]],
    )
    return {"result": chat_with_csv(csv)}


@when(
    parsers.parse('the user uploads a CSV with only headers "{headers}"'),
    target_fixture="context",
)
def upload_csv_headers_only(headers):
    return {"result": chat_with_csv(headers)}


@when(
    parsers.re(
        r'the user uploads a CSV with unchanged Engineering departments '
        r'for "(?P<vendor_a>[^"]+)" and "(?P<vendor_b>[^"]+)"'
    ),
    target_fixture="context",
)
def upload_csv_no_changes(test_vendor_ids, vendor_a, vendor_b):
    csv = make_csv(
        ["id", "department"],
        [
            [test_vendor_ids[vendor_a], "Engineering"],
            [test_vendor_ids[vendor_b], "Engineering"],
        ],
    )
    return {"result": chat_with_csv(csv)}


# ── When — mode switch ───────────────────────────────────────────────────


@when(
    parsers.parse('the user does a CSV batch edit for "{vendor}"'),
    target_fixture="context",
)
def csv_batch_step1(test_vendor_ids, vendor):
    csv = make_csv(
        ["id", "department"],
        [[test_vendor_ids[vendor], "IT"]],
    )
    payload = {
        "messages": [
            {"role": "user", "content": "I need to make bulk vendor changes via CSV."},
            {"role": "assistant", "content": "Sure, attach your CSV and I'll process it."},
            {"role": "user", "content": "Uploading test.csv"},
        ],
        "context": {"app": "vendors"},
        "attachments": [{"filename": "test.csv", "content": csv, "mime": "text/csv"}],
    }
    resp = requests.post(
        f"{BASE_URL}/chat", json=payload, headers=DEFAULT_HEADERS, timeout=30,
    )
    resp.raise_for_status()
    result1 = resp.json()
    actions = result1.get("pending_actions", [])
    assert any(a["type"] == "confirm_csv_batch" for a in actions), (
        f"Step 1 should produce confirm_csv_batch. Got: {result1['reply']}"
    )
    return {"step1_result": result1}


@when(
    parsers.parse(
        'then asks to change "{vendor}" department to Marketing '
        "in the same conversation"
    ),
)
def single_modify_step2(context, test_vendor_ids, vendor):
    result1 = context["step1_result"]
    payload = {
        "messages": [
            {"role": "user", "content": "I need to make bulk vendor changes via CSV."},
            {"role": "assistant", "content": "Sure, attach your CSV and I'll process it."},
            {"role": "user", "content": "Uploading test.csv"},
            {"role": "assistant", "content": result1["reply"]},
            {"role": "user", "content": f"Change {vendor} to department Marketing"},
        ],
        "context": {"app": "vendors"},
    }
    resp = requests.post(
        f"{BASE_URL}/chat", json=payload, headers=DEFAULT_HEADERS, timeout=30,
    )
    resp.raise_for_status()
    context["result"] = resp.json()


# ── Then — batch summary assertions ──────────────────────────────────────


@then(parsers.parse("the batch summary shows vendor_count {count:d}"))
def assert_vendor_count(context, count):
    batch = context["batch"]
    assert batch["summary"]["vendor_count"] == count


@then(parsers.parse('the batch summary field_counts includes "{field}"'))
def assert_field_in_counts(context, field):
    batch = context["batch"]
    assert field in batch["summary"]["field_counts"], (
        f"Expected {field} in field_counts, got: {batch['summary']['field_counts']}"
    )


@then(parsers.parse("the batch summary has {count:d} fields in field_counts"))
def assert_field_count(context, count):
    batch = context["batch"]
    assert len(batch["summary"]["field_counts"]) == count


# ── Then — batch change assertions ───────────────────────────────────────


@then(parsers.parse('the batch changes do not include "{field}"'))
def assert_field_not_in_changes(context, field):
    batch = context["batch"]
    changes = batch["updates"][0]["changes"]
    assert field not in changes, f"{field} should not be in changes, got: {changes}"


@then(parsers.parse('the batch changes include "{field}"'))
def assert_field_in_changes(context, field):
    batch = context["batch"]
    changes = batch["updates"][0]["changes"]
    assert field in changes, f"Expected {field} in changes, got: {changes}"


@then("the reply indicates the CSV is empty")
def assert_reply_empty_csv(context):
    reply = context["result"]["reply"].lower()
    assert any(w in reply for w in [
        "empty", "no ", "0 ", "no data", "doesn't contain",
    ]), f"Expected empty-CSV indication, got: {reply[:300]}"


@then("the reply indicates no changes were detected")
def assert_reply_no_changes(context):
    reply = context["result"]["reply"].lower()
    assert any(w in reply for w in [
        "no change", "no update", "same", "detect", "did not",
        "no modification", "nothing to update", "unchanged", "already",
    ]), f"Expected no-change message, got: {reply[:300]}"


# ── Then — edge case and mode switch ─────────────────────────────────────


@then("the CSV is processed without a column error")
def assert_bom_handled(context):
    result = context["result"]
    actions = result.get("pending_actions", [])
    has_confirm = any(a["type"] == "confirm_csv_batch" for a in actions)
    has_error = "column" in result["reply"].lower() or "error" in result["reply"].lower()
    assert has_confirm or not has_error, (
        f"BOM should not cause failure. Reply: {result['reply']}"
    )


@then("the follow-up uses modify_vendor not CSV redirect")
def assert_modify_vendor_used(context):
    result = context["result"]
    tools = result.get("tool_calls_executed", [])
    reply = result["reply"].lower()
    used_modify = "modify_vendor" in tools
    redirected_csv = "csv" in reply and "upload" in reply
    assert used_modify or not redirected_csv, (
        f"Expected modify_vendor or no CSV redirect. "
        f"Tools: {tools}, Reply: {reply[:200]}"
    )
