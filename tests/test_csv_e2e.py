"""
End-to-end tests for the CSV bulk-edit pipeline.

Uses real test vendors (source_system='testing') and hits the live
/chat endpoint with CSV attachments. Requires the agent to be running
on localhost:8080 with DEV_AUTH_EMAIL set.

Test vendors are created automatically at the start of the run and
deleted at the end (even on failure) to keep the production DB clean.
"""

import json
import os
import requests

BASE = "http://127.0.0.1:8080"
HEADERS = {"Content-Type": "application/json"}

TEST_VENDOR_IDS = {}

_TEST_VENDOR_SPECS = [
    ("Test Vendor Alpha",   "test-alpha",   "dfe5fe4d-67df-4419-aec8-19ba0a9ed508", "89f8ec71-db35-4c86-be14-f435bd6a6ca7", "monthly",     "Widget supply"),
    ("Test Vendor Bravo",   "test-bravo",   "dfe5fe4d-67df-4419-aec8-19ba0a9ed508", "89f8ec71-db35-4c86-be14-f435bd6a6ca7", "annual",      "Cloud hosting"),
    ("Test Vendor Charlie", "test-charlie", "038ec2d2-ef5b-4cf9-a99c-15e717399e36", "e1ee8bee-5b98-4d0f-839e-6b2e570aa2f0", "usage-based", "Ad platform"),
    ("Test Vendor Delta",   "test-delta",   "038ec2d2-ef5b-4cf9-a99c-15e717399e36", "e1ee8bee-5b98-4d0f-839e-6b2e570aa2f0", "monthly",     "Email marketing"),
    ("Test Vendor Echo",    "test-echo",    "a37df929-61dd-437e-b2cc-c5acda327a4c", "4f809c5e-4263-478a-b103-9e264e59aba2", "annual",      "Accounting software"),
    ("Test Vendor Foxtrot", "test-foxtrot", "a37df929-61dd-437e-b2cc-c5acda327a4c", "4f809c5e-4263-478a-b103-9e264e59aba2", None,          None),
]


def _get_db_pool():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), interpolate=False)
    from service.pg_client import get_pool
    return get_pool()


def _insert_test_vendors():
    """Insert test vendors if they don't already exist."""
    pool = _get_db_pool()
    with pool.connection() as conn:
        for name, ssid, dept_id, owner_id, freq, purpose in _TEST_VENDOR_SPECS:
            conn.execute(
                """INSERT INTO vendors
                       (name, source_system, source_system_id, department_id, owner_id,
                        billing_frequency, purpose, created_at, modified_at)
                   VALUES (%s, 'testing', %s, %s, %s, %s, %s, NOW(), NOW())
                   ON CONFLICT (source_system, source_system_id) DO NOTHING""",
                (name, ssid, dept_id, owner_id, freq, purpose),
            )
        conn.commit()


def _delete_test_vendors():
    """Remove all vendors with source_system='testing'."""
    pool = _get_db_pool()
    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM vendors WHERE source_system = 'testing'")
        conn.commit()
        print(f"\n  Cleanup: deleted {cur.rowcount} test vendors")


def setup_module():
    """Create test vendors and fetch their IDs."""
    _insert_test_vendors()
    resp = requests.get(f"{BASE}/vendors", timeout=10)
    resp.raise_for_status()
    vendors = resp.json()
    for v in vendors:
        if v.get("sourceSystem") == "testing":
            TEST_VENDOR_IDS[v["name"]] = v["id"]
    assert len(TEST_VENDOR_IDS) >= 6, (
        f"Expected >=6 test vendors, found {len(TEST_VENDOR_IDS)}: {list(TEST_VENDOR_IDS)}"
    )


def teardown_module():
    """Delete test vendors after the run."""
    _delete_test_vendors()


def _chat_with_csv(csv_content: str, filename: str = "test.csv", prompt: str = ""):
    """Send a chat message with a CSV attachment and return the parsed response."""
    user_content = prompt or f"Uploading {filename}"
    payload = {
        "messages": [
            {"role": "user", "content": "I need to make bulk vendor changes via CSV."},
            {"role": "assistant", "content": "Sure, attach your CSV and I'll process it."},
            {"role": "user", "content": user_content},
        ],
        "context": {"app": "vendors"},
        "attachments": [{"filename": filename, "content": csv_content, "mime": "text/csv"}],
    }
    resp = requests.post(f"{BASE}/chat", json=payload, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _process_csv_directly(csv_content: str):
    """Call process_vendor_csv tool via the chat endpoint and return the tool result."""
    return _chat_with_csv(csv_content)


# ---- ids helper ----

def _id(name: str) -> str:
    return TEST_VENDOR_IDS[name]


def _make_csv(headers: list[str], rows: list[list[str]]) -> str:
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(row))
    return "\n".join(lines)


# =========================================================================
# SUCCESS CASES
# =========================================================================

class TestSuccessCases:

    def test_single_field_change_department(self):
        """Change department for two vendors."""
        csv = _make_csv(
            ["id", "department"],
            [
                [_id("Test Vendor Alpha"), "Marketing"],
                [_id("Test Vendor Bravo"), "Marketing"],
            ],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"]
        actions = result.get("pending_actions", [])
        assert any(a["type"] == "confirm_csv_batch" for a in actions), (
            f"Expected confirm_csv_batch action, got: {actions}. Reply: {reply}"
        )
        batch = next(a for a in actions if a["type"] == "confirm_csv_batch")
        assert batch["summary"]["vendor_count"] == 2
        assert "department_id" in batch["summary"]["field_counts"]
        print(f"  PASS: 2 vendors, department change -> confirm dialog")

    def test_multiple_fields_one_vendor(self):
        """Change department and billing frequency on one vendor."""
        csv = _make_csv(
            ["id", "department", "billingFrequency"],
            [[_id("Test Vendor Charlie"), "Finance", "annual"]],
        )
        result = _process_csv_directly(csv)
        actions = result.get("pending_actions", [])
        assert any(a["type"] == "confirm_csv_batch" for a in actions), (
            f"Expected confirm_csv_batch, got: {actions}"
        )
        batch = next(a for a in actions if a["type"] == "confirm_csv_batch")
        assert batch["summary"]["vendor_count"] == 1
        assert len(batch["summary"]["field_counts"]) == 2
        print(f"  PASS: 1 vendor, 2 fields -> confirm dialog")

    def test_partial_columns_only_id_and_purpose(self):
        """CSV with only id and purpose — all other columns deleted."""
        csv = _make_csv(
            ["id", "purpose"],
            [
                [_id("Test Vendor Echo"), "Updated accounting tools"],
                [_id("Test Vendor Foxtrot"), "New purpose for Foxtrot"],
            ],
        )
        result = _process_csv_directly(csv)
        actions = result.get("pending_actions", [])
        assert any(a["type"] == "confirm_csv_batch" for a in actions)
        batch = next(a for a in actions if a["type"] == "confirm_csv_batch")
        assert batch["summary"]["vendor_count"] == 2
        assert "purpose" in batch["summary"]["field_counts"]
        print(f"  PASS: partial columns (id + purpose only) -> confirm dialog")

    def test_subset_of_rows(self):
        """Only include 1 of the 6 test vendors — the rest are omitted."""
        csv = _make_csv(
            ["id", "department"],
            [[_id("Test Vendor Delta"), "IT"]],
        )
        result = _process_csv_directly(csv)
        actions = result.get("pending_actions", [])
        assert any(a["type"] == "confirm_csv_batch" for a in actions)
        batch = next(a for a in actions if a["type"] == "confirm_csv_batch")
        assert batch["summary"]["vendor_count"] == 1
        print(f"  PASS: single row subset -> confirm dialog for 1 vendor")

    def test_name_column_readonly_ignored(self):
        """Changing the name column should be silently ignored (readonly)."""
        csv = _make_csv(
            ["id", "name", "department"],
            [[_id("Test Vendor Alpha"), "RENAMED ALPHA", "Finance"]],
        )
        result = _process_csv_directly(csv)
        actions = result.get("pending_actions", [])
        assert any(a["type"] == "confirm_csv_batch" for a in actions)
        batch = next(a for a in actions if a["type"] == "confirm_csv_batch")
        changes = batch["updates"][0]["changes"]
        assert "name" not in changes, f"name should be readonly, but got: {changes}"
        assert "department_id" in changes
        print(f"  PASS: name column ignored (readonly), department applied")


# =========================================================================
# COLUMN VALIDATION ERRORS
# =========================================================================

class TestColumnErrors:

    def test_misspelled_column(self):
        """Column 'deparment' should fail validation."""
        csv = _make_csv(
            ["id", "deparment"],
            [[_id("Test Vendor Alpha"), "Marketing"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "deparment" in reply or "column" in reply, (
            f"Expected column error in reply, got: {result['reply']}"
        )
        assert not result.get("pending_actions"), "Should NOT get confirm dialog"
        print(f"  PASS: misspelled 'deparment' caught")

    def test_unknown_extra_column(self):
        """Column 'favoriteColor' doesn't exist in the profile."""
        csv = _make_csv(
            ["id", "department", "favoriteColor"],
            [[_id("Test Vendor Alpha"), "Marketing", "blue"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "favoritecolor" in reply or "column" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: unknown column 'favoriteColor' caught")

    def test_missing_id_column(self):
        """CSV without the id column should fail."""
        csv = _make_csv(
            ["department", "purpose"],
            [["Marketing", "Testing"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "id" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: missing id column caught")

    def test_upstream_column_rejected(self):
        """paymentMethod is upstream-sourced and not in the CSV profile."""
        csv = _make_csv(
            ["id", "paymentMethod"],
            [[_id("Test Vendor Alpha"), "ACH"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "paymentmethod" in reply or "column" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: upstream column 'paymentMethod' rejected")


# =========================================================================
# ID VALIDATION ERRORS
# =========================================================================

class TestIdErrors:

    def test_malformed_uuid_extra_char(self):
        """UUID with an extra character should fail format check."""
        bad_id = _id("Test Vendor Alpha") + "f"
        csv = _make_csv(
            ["id", "department"],
            [[bad_id, "Marketing"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "uuid" in reply or "valid" in reply or "format" in reply, (
            f"Expected UUID format error, got: {result['reply']}"
        )
        assert not result.get("pending_actions")
        print(f"  PASS: malformed UUID (extra char) caught")

    def test_truncated_uuid(self):
        """UUID missing last few characters."""
        bad_id = _id("Test Vendor Alpha")[:-4]
        csv = _make_csv(
            ["id", "department"],
            [[bad_id, "Marketing"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "uuid" in reply or "valid" in reply or "format" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: truncated UUID caught")

    def test_nonexistent_uuid(self):
        """Valid UUID format but doesn't exist in the DB."""
        fake_id = "00000000-0000-4000-a000-000000000000"
        csv = _make_csv(
            ["id", "department"],
            [[fake_id, "Marketing"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "not found" in reply or "exist" in reply or "not" in reply, (
            f"Expected not-found error, got: {result['reply']}"
        )
        assert not result.get("pending_actions")
        print(f"  PASS: nonexistent UUID caught")

    def test_empty_id_cell(self):
        """Row with empty id value."""
        csv = _make_csv(
            ["id", "department"],
            [["", "Marketing"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "id" in reply or "missing" in reply or "empty" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: empty ID cell caught")


# =========================================================================
# VALUE VALIDATION ERRORS
# =========================================================================

class TestValueErrors:

    def test_invalid_department_name(self):
        """Department 'Zorbology' doesn't exist."""
        csv = _make_csv(
            ["id", "department"],
            [[_id("Test Vendor Alpha"), "Zorbology"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "zorbology" in reply or "resolve" in reply or "department" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: invalid department 'Zorbology' caught")

    def test_invalid_owner(self):
        """Owner 'nobody@nowhere.com' doesn't exist."""
        csv = _make_csv(
            ["id", "owner"],
            [[_id("Test Vendor Alpha"), "nobody@nowhere.com"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "nobody" in reply or "resolve" in reply or "owner" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: invalid owner caught")

    def test_invalid_billing_frequency(self):
        """billingFrequency 'biweekly' is not a valid enum value."""
        csv = _make_csv(
            ["id", "billingFrequency"],
            [[_id("Test Vendor Alpha"), "biweekly"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "biweekly" in reply or "billing" in reply or "valid" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: invalid billingFrequency 'biweekly' caught")

    def test_invalid_date_format(self):
        """contractStartDate with garbage value."""
        csv = _make_csv(
            ["id", "contractStartDate"],
            [[_id("Test Vendor Alpha"), "not-a-date"]],
        )
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "date" in reply or "format" in reply or "not-a-date" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: invalid date 'not-a-date' caught")


# =========================================================================
# EDGE CASES
# =========================================================================

class TestEdgeCases:

    def test_empty_csv_headers_only(self):
        """CSV with headers but no data rows."""
        csv = "id,department"
        result = _process_csv_directly(csv)
        reply = result["reply"].lower()
        assert "empty" in reply or "no" in reply
        assert not result.get("pending_actions")
        print(f"  PASS: empty CSV (headers only) caught")

    def test_no_actual_changes(self):
        """All values match current data — nothing to update."""
        csv = _make_csv(
            ["id", "department"],
            [
                [_id("Test Vendor Alpha"), "Engineering"],
                [_id("Test Vendor Bravo"), "Engineering"],
            ],
        )
        result = _process_csv_directly(csv)
        assert not result.get("pending_actions"), (
            "Should not get confirm dialog when nothing changed"
        )
        reply = result["reply"].lower()
        assert "no change" in reply or "no update" in reply or "same" in reply or "detect" in reply, (
            f"Expected no-change message, got: {result['reply']}"
        )
        print(f"  PASS: no actual changes -> no confirm dialog")

    def test_bom_character(self):
        """CSV exported from Excel with UTF-8 BOM."""
        csv = "\ufeff" + _make_csv(
            ["id", "department"],
            [[_id("Test Vendor Alpha"), "Marketing"]],
        )
        result = _process_csv_directly(csv)
        actions = result.get("pending_actions", [])
        has_confirm = any(a["type"] == "confirm_csv_batch" for a in actions)
        has_error = "column" in result["reply"].lower() or "error" in result["reply"].lower()
        assert has_confirm or not has_error, (
            f"BOM should not cause failure. Reply: {result['reply']}"
        )
        print(f"  PASS: BOM character handled (confirm={has_confirm})")


# =========================================================================
# TWO-STEP: CSV BATCH THEN SINGLE-VENDOR MODIFY
# =========================================================================

class TestModeSwitch:

    def test_csv_then_single_modify(self):
        """
        Step 1: Do a successful CSV batch (get confirm dialog).
        Step 2: In the same conversation, ask to change one vendor — should
                trigger modify_vendor, NOT redirect to CSV.
        """
        csv = _make_csv(
            ["id", "department"],
            [[_id("Test Vendor Delta"), "IT"]],
        )
        step1_payload = {
            "messages": [
                {"role": "user", "content": "I need to make bulk vendor changes via CSV."},
                {"role": "assistant", "content": "Sure, attach your CSV and I'll process it."},
                {"role": "user", "content": "Uploading test.csv"},
            ],
            "context": {"app": "vendors"},
            "attachments": [{"filename": "test.csv", "content": csv, "mime": "text/csv"}],
        }
        resp1 = requests.post(f"{BASE}/chat", json=step1_payload, headers=HEADERS, timeout=30)
        resp1.raise_for_status()
        result1 = resp1.json()
        actions1 = result1.get("pending_actions", [])
        assert any(a["type"] == "confirm_csv_batch" for a in actions1), (
            f"Step 1 should produce confirm_csv_batch. Got: {result1['reply']}"
        )
        print(f"  Step 1 PASS: CSV batch confirmed")

        step2_payload = {
            "messages": [
                {"role": "user", "content": "I need to make bulk vendor changes via CSV."},
                {"role": "assistant", "content": "Sure, attach your CSV and I'll process it."},
                {"role": "user", "content": "Uploading test.csv"},
                {"role": "assistant", "content": result1["reply"]},
                {"role": "user", "content": "Change Test Vendor Echo to department Marketing"},
            ],
            "context": {"app": "vendors"},
        }
        resp2 = requests.post(f"{BASE}/chat", json=step2_payload, headers=HEADERS, timeout=30)
        resp2.raise_for_status()
        result2 = resp2.json()

        tools_used = result2.get("tool_calls_executed", [])
        reply2 = result2["reply"].lower()
        used_modify = "modify_vendor" in tools_used
        redirected_csv = "csv" in reply2 and "upload" in reply2

        assert used_modify or not redirected_csv, (
            f"Step 2: Expected modify_vendor or at least not a CSV redirect. "
            f"Tools: {tools_used}, Reply: {result2['reply'][:200]}"
        )
        print(f"  Step 2 PASS: single-vendor request used tools={tools_used}")


if __name__ == "__main__":
    import sys
    setup_module()
    print(f"\nLoaded {len(TEST_VENDOR_IDS)} test vendors\n")

    test_classes = [
        TestSuccessCases,
        TestColumnErrors,
        TestIdErrors,
        TestValueErrors,
        TestEdgeCases,
        TestModeSwitch,
    ]

    passed = 0
    failed = 0
    errors = []

    try:
        for cls in test_classes:
            print(f"\n{'='*60}")
            print(f"  {cls.__name__}")
            print(f"{'='*60}")
            instance = cls()
            methods = [m for m in dir(instance) if m.startswith("test_")]
            for method_name in sorted(methods):
                method = getattr(instance, method_name)
                try:
                    method()
                    passed += 1
                except Exception as e:
                    failed += 1
                    errors.append(f"  FAIL: {cls.__name__}.{method_name}: {e}")
                    print(f"  FAIL: {method_name}: {e}")
    finally:
        teardown_module()

    print(f"\n{'='*60}")
    print(f"  RESULTS: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(e)
        sys.exit(1)
    else:
        print("\nAll tests passed!")
