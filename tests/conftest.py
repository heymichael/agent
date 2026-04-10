"""Shared test fixtures and helpers for both BDD e2e tests and plain unit tests."""

import os
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", interpolate=False)

BASE_URL = "http://127.0.0.1:8080"
DEFAULT_HEADERS = {"Content-Type": "application/json"}

PRODUCT_DEPT_ID = "614dfa63-bf2e-45c1-b84e-f48cc386037b"
ENGINEERING_DEPT_ID = "dfe5fe4d-67df-4419-aec8-19ba0a9ed508"

SCOPED_USER_EMAIL = "michael.d.mader@gmail.com"
SCOPED_HEADERS = {"Content-Type": "application/json", "X-Test-Email": SCOPED_USER_EMAIL}

_TEST_VENDOR_SPECS = [
    ("Test Vendor Alpha",   "test-alpha",   "dfe5fe4d-67df-4419-aec8-19ba0a9ed508", "89f8ec71-db35-4c86-be14-f435bd6a6ca7", "monthly",     "Widget supply"),
    ("Test Vendor Bravo",   "test-bravo",   "dfe5fe4d-67df-4419-aec8-19ba0a9ed508", "89f8ec71-db35-4c86-be14-f435bd6a6ca7", "annual",      "Cloud hosting"),
    ("Test Vendor Charlie", "test-charlie", "038ec2d2-ef5b-4cf9-a99c-15e717399e36", "e1ee8bee-5b98-4d0f-839e-6b2e570aa2f0", "usage-based", "Ad platform"),
    ("Test Vendor Delta",   "test-delta",   "038ec2d2-ef5b-4cf9-a99c-15e717399e36", "e1ee8bee-5b98-4d0f-839e-6b2e570aa2f0", "monthly",     "Email marketing"),
    ("Test Vendor Echo",    "test-echo",    "a37df929-61dd-437e-b2cc-c5acda327a4c", "4f809c5e-4263-478a-b103-9e264e59aba2", "annual",      "Accounting software"),
    ("Test Vendor Foxtrot", "test-foxtrot", "a37df929-61dd-437e-b2cc-c5acda327a4c", "4f809c5e-4263-478a-b103-9e264e59aba2", None,          None),
]

_ACL_VENDOR_SPECS = [
    ("ACL Test Vendor Allowed",  "acl-test-allowed",  PRODUCT_DEPT_ID),
    ("ACL Test Vendor Denied",   "acl-test-denied",   ENGINEERING_DEPT_ID),
]


def _get_db_pool():
    from service.pg_client import get_pool
    return get_pool()


@pytest.fixture(scope="session")
def test_vendor_ids():
    """Insert source_system='testing' vendors and yield name->UUID mapping.

    Tears down all testing vendors at session end, even on failure.
    """
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

    resp = requests.get(f"{BASE_URL}/vendors", timeout=10)
    resp.raise_for_status()
    ids: dict[str, str] = {}
    for v in resp.json():
        if v.get("sourceSystem") == "testing":
            ids[v["name"]] = v["id"]
    assert len(ids) >= 6, f"Expected >=6 test vendors, found {len(ids)}: {list(ids)}"

    yield ids

    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM vendors WHERE source_system = 'testing'")
        conn.commit()
        print(f"\n  Cleanup: deleted {cur.rowcount} test vendors")


@pytest.fixture(scope="session")
def acl_vendor_ids():
    """Insert ACL test vendors (allowed/denied) and yield name->UUID mapping."""
    pool = _get_db_pool()
    with pool.connection() as conn:
        for name, ssid, dept_id in _ACL_VENDOR_SPECS:
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

    resp = requests.get(f"{BASE_URL}/vendors", timeout=10)
    resp.raise_for_status()
    ids: dict[str, str] = {}
    for v in resp.json():
        if v.get("sourceSystem") == "testing" and v["name"].startswith("ACL Test"):
            ids[v["name"]] = v["id"]
    assert len(ids) >= 2, f"Expected >=2 ACL vendors, found {len(ids)}: {list(ids)}"

    yield ids

    with pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM vendors WHERE source_system = 'testing' "
            "AND source_system_id LIKE 'acl-test-%%'"
        )
        conn.commit()
        print(f"\n  ACL cleanup: deleted {cur.rowcount} test vendor(s)")


@pytest.fixture()
def context():
    """Mutable dict for passing state between BDD steps within a single scenario."""
    return {}


# ── Shared helpers (importable by step definitions and legacy tests) ─────


def chat(prompt: str, *, headers=None, attachments=None):
    """POST to /chat with a single user message and return the parsed response."""
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "context": {"app": "vendors"},
    }
    if attachments:
        payload["attachments"] = attachments
    resp = requests.post(
        f"{BASE_URL}/chat",
        json=payload,
        headers=headers or DEFAULT_HEADERS,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def scoped_chat(prompt: str, *, attachments=None):
    """POST to /chat as the scoped (non-admin) user."""
    return chat(prompt, headers=SCOPED_HEADERS, attachments=attachments)


def chat_with_csv(csv_content: str, filename: str = "test.csv", prompt: str = "",
                  *, headers=None):
    """POST to /chat with a CSV attachment using the standard bulk-edit preamble."""
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
    resp = requests.post(
        f"{BASE_URL}/chat",
        json=payload,
        headers=headers or DEFAULT_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def chat_multi_turn(messages: list[dict], *, headers=None):
    """POST to /chat with a full message history and return the parsed response."""
    payload = {
        "messages": messages,
        "context": {"app": "vendors"},
    }
    resp = requests.post(
        f"{BASE_URL}/chat",
        json=payload,
        headers=headers or DEFAULT_HEADERS,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def make_csv(headers: list[str], rows: list[list[str]]) -> str:
    """Build a CSV string from column headers and data rows."""
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(c) for c in row))
    return "\n".join(lines)
