"""
End-to-end tests for write access control with a scoped (non-admin) user.

Uses real test vendors (source_system='testing') and hits the live
/chat endpoint.  The scoped user (michael.d.mader@gmail.com) has
allowed_departments=['Product'], so vendors in Product are editable
and vendors in other departments are denied.

Requires:
  - Agent running on localhost:8080 with DEV_AUTH_EMAIL set
  - Cloud SQL Proxy on localhost:5433
  - The X-Test-Email header override in service/auth.py

Test layer: Authorization
-------------------------
Every test in this file exercises the authorization gate — the layer
that sits between input validation and business logic.  Input
validation (bad columns, malformed UUIDs, etc.) is user-agnostic and
covered in test_csv_e2e.py.  Business logic (confirm dialogs, actual
edits) is also covered there via the admin bypass.  This file tests
the only layer whose outcome depends on caller identity.

Class → scenario mapping:
  TestModifyVendorAccess — single-vendor edit allowed / denied
  TestCsvAccessControl   — bulk CSV allowed / denied / mixed
"""

import os
import requests

import pytest

pytestmark = pytest.mark.vendor_management

BASE = "http://127.0.0.1:8080"

SCOPED_USER_EMAIL = "michael.d.mader@gmail.com"
SCOPED_HEADERS = {"Content-Type": "application/json", "X-Test-Email": SCOPED_USER_EMAIL}

PRODUCT_DEPT_ID = "614dfa63-bf2e-45c1-b84e-f48cc386037b"
ENGINEERING_DEPT_ID = "dfe5fe4d-67df-4419-aec8-19ba0a9ed508"

TEST_VENDOR_IDS: dict[str, str] = {}

_TEST_VENDOR_SPECS = [
    ("ACL Test Vendor Allowed",  "acl-test-allowed",  PRODUCT_DEPT_ID),
    ("ACL Test Vendor Denied",   "acl-test-denied",   ENGINEERING_DEPT_ID),
]


def _get_db_pool():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), interpolate=False)
    from service.pg_client import get_pool
    return get_pool()


def _insert_test_vendors():
    pool = _get_db_pool()
    with pool.connection() as conn:
        for name, ssid, dept_id in _TEST_VENDOR_SPECS:
            conn.execute(
                """INSERT INTO vendors
                       (name, source_system, source_system_id, department_id,
                        is_contractor, created_at, modified_at)
                   VALUES (%s, 'testing', %s, %s, false, NOW(), NOW())
                   ON CONFLICT (source_system, source_system_id)
                   DO UPDATE SET is_contractor = false""",
                (name, ssid, dept_id),
            )
        conn.commit()


def _delete_test_vendors():
    pool = _get_db_pool()
    with pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM vendors WHERE source_system = 'testing' "
            "AND source_system_id LIKE 'acl-test-%'"
        )
        conn.commit()
        print(f"\n  ACL cleanup: deleted {cur.rowcount} test vendor(s)")


def setup_module():
    _insert_test_vendors()
    resp = requests.get(f"{BASE}/vendors", timeout=10)
    resp.raise_for_status()
    for v in resp.json():
        if v.get("sourceSystem") == "testing" and v["name"].startswith("ACL Test"):
            TEST_VENDOR_IDS[v["name"]] = v["id"]
    assert len(TEST_VENDOR_IDS) >= 2, (
        f"Expected >=2 ACL test vendors, found {len(TEST_VENDOR_IDS)}: {list(TEST_VENDOR_IDS)}"
    )
    print(f"  ACL test vendors: {TEST_VENDOR_IDS}")


def teardown_module():
    _delete_test_vendors()


def _id(name: str) -> str:
    return TEST_VENDOR_IDS[name]


def _chat(prompt: str, attachments=None):
    """Send a chat message as the scoped user and return the parsed response."""
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "context": {"app": "vendors"},
    }
    if attachments:
        payload["attachments"] = attachments
    resp = requests.post(
        f"{BASE}/chat", json=payload,
        headers=SCOPED_HEADERS, timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _make_csv(headers: list[str], rows: list[list[str]]) -> str:
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(c) for c in row))
    return "\n".join(lines)


# ── modify_vendor: scoped user ──────────────────────────────────────────

class TestModifyVendorAccess:
    """Verify modify_vendor enforces department-based write ACL."""

    def test_allowed_vendor_opens_edit(self):
        """Scoped user can trigger modify_vendor for a vendor in Product."""
        result = _chat(f"Open the edit form for ACL Test Vendor Allowed")
        tools = result.get("tool_calls_executed", [])
        reply = result["reply"].lower()
        assert "modify_vendor" in tools, (
            f"Expected modify_vendor call, got {tools}. Reply: {result['reply'][:300]}"
        )
        assert "not authorized" not in reply and "permission" not in reply, (
            f"Should NOT be denied for in-scope vendor. Reply: {result['reply'][:300]}"
        )
        print(f"  PASS: allowed vendor edit — tools={tools}")

    def test_denied_vendor_returns_not_authorized(self):
        """Scoped user is denied modify_vendor for a vendor in Engineering."""
        result = _chat(f"Open the edit form for ACL Test Vendor Denied")
        tools = result.get("tool_calls_executed", [])
        reply = result["reply"].lower()
        assert "modify_vendor" in tools, (
            f"Expected modify_vendor call, got {tools}. Reply: {result['reply'][:300]}"
        )
        has_denial = any(w in reply for w in [
            "not authorized", "permission", "don't have", "cannot edit", "not allowed",
        ])
        assert has_denial, (
            f"Expected denial message for out-of-scope vendor. Reply: {result['reply'][:300]}"
        )
        print(f"  PASS: denied vendor edit — tools={tools}")


# ── process_vendor_csv: scoped user ─────────────────────────────────────

class TestCsvAccessControl:
    """Verify process_vendor_csv enforces department-based write ACL."""

    def test_csv_with_denied_vendor_rejected(self):
        """CSV containing an Engineering vendor should be rejected."""
        denied_id = _id("ACL Test Vendor Denied")
        csv = _make_csv(["id", "purpose"], [[denied_id, "New purpose"]])
        result = _chat(
            "Process this vendor CSV",
            attachments=[{"filename": "test.csv", "content": csv, "mime": "text/csv"}],
        )
        reply = result["reply"].lower()
        has_denial = any(w in reply for w in [
            "not authorized", "permission", "don't have", "cannot edit", "not allowed",
        ])
        assert has_denial, (
            f"Expected denial for out-of-scope vendor. Reply: {result['reply'][:300]}"
        )
        assert not result.get("pending_actions"), (
            "Should NOT get confirm dialog for denied vendor"
        )
        print(f"  PASS: CSV with denied vendor rejected")

    def test_csv_with_mixed_vendors_rejected(self):
        """CSV mixing in-scope and out-of-scope vendors rejects the whole batch."""
        allowed_id = _id("ACL Test Vendor Allowed")
        denied_id = _id("ACL Test Vendor Denied")
        csv = _make_csv(
            ["id", "purpose"],
            [[allowed_id, "Updated purpose"], [denied_id, "Should be blocked"]],
        )
        result = _chat(
            "Process this vendor CSV",
            attachments=[{"filename": "test.csv", "content": csv, "mime": "text/csv"}],
        )
        reply = result["reply"].lower()
        has_denial = any(w in reply for w in [
            "not authorized", "permission", "don't have", "cannot edit", "not allowed",
        ])
        assert has_denial, (
            f"Expected denial for mixed CSV. Reply: {result['reply'][:300]}"
        )
        assert not result.get("pending_actions"), (
            "Should NOT get confirm dialog when any vendor is out of scope"
        )
        print(f"  PASS: mixed CSV (allowed + denied) rejected entirely")

    def test_csv_with_allowed_vendor_accepted(self):
        """CSV containing only a Product vendor should reach confirmation."""
        allowed_id = _id("ACL Test Vendor Allowed")
        csv = _make_csv(["id", "purpose"], [[allowed_id, "Updated purpose for ACL test"]])
        result = _chat(
            "Process this vendor CSV",
            attachments=[{"filename": "test.csv", "content": csv, "mime": "text/csv"}],
        )
        reply = result["reply"].lower()
        actions = result.get("pending_actions", [])
        has_confirm = any(a["type"] == "confirm_csv_batch" for a in actions)
        no_denial = "not authorized" not in reply and "permission" not in reply
        assert has_confirm or no_denial, (
            f"Expected confirm or no denial for in-scope vendor. Reply: {result['reply'][:300]}"
        )
        print(f"  PASS: CSV with allowed vendor accepted")
