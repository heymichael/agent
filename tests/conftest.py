"""Shared test fixtures and helpers for both BDD e2e tests and plain unit tests."""

import os
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

pytest_plugins = ["tests.stochastic_plugin"]

load_dotenv(Path(__file__).resolve().parent.parent / ".env", interpolate=False)

from service.costs import calculate_cost  # noqa: E402 (after dotenv load)

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

# ── Per-test cost tracking ───────────────────────────────────────────────

_current_test_usage: list[dict] = []


def _track_usage(result: dict) -> None:
    """Extract the usage dict from a /chat response and accumulate it."""
    usage = result.get("usage")
    if usage:
        _current_test_usage.append(usage)


def _sum_test_cost() -> dict:
    """Aggregate all tracked usage into a single cost summary."""
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    model = None
    for u in _current_test_usage:
        totals["prompt_tokens"] += u.get("prompt_tokens", 0)
        totals["completion_tokens"] += u.get("completion_tokens", 0)
        totals["total_tokens"] += u.get("total_tokens", 0)
        model = model or u.get("model")
    cost = None
    if model:
        cost = calculate_cost(model, totals["prompt_tokens"], totals["completion_tokens"])
    return {**totals, "model": model, "cost_usd": cost, "api_calls": len(_current_test_usage)}


@pytest.fixture(autouse=True)
def _reset_cost_tracker():
    """Clear usage accumulator before each test."""
    _current_test_usage.clear()
    yield


# ── Multi-org tenancy: default caller_org_slug for unit tests ───────────
#
# Phase 3 of multi-org tenancy (task 254) makes tool handlers in
# `service.tools` and `mcp_server.tools` read the active org slug from a
# contextvar set by the `/chat` endpoint at request entry. Unit tests
# don't go through `/chat`, so we auto-set a default slug here and reset
# it after each test. Tests that need to assert the "no slug" path can
# call `tools.set_caller_org_slug(None)` themselves inside the test body.

DEFAULT_TEST_ORG_SLUG = "arcade"


@pytest.fixture(autouse=True)
def _default_caller_org_slug():
    """Set a default caller org slug for the duration of each test."""
    from service import tools as _tools  # local to avoid import-time DB cost
    _tools.set_caller_org_slug(DEFAULT_TEST_ORG_SLUG)
    try:
        yield DEFAULT_TEST_ORG_SLUG
    finally:
        _tools.set_caller_org_slug(None)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Inject per-test cost metadata into json-report.

    Writes to both report.user_properties (for stochastic plugin to read
    per-iteration cost) and item.user_properties (so pytest-json-report
    captures it on the teardown report).
    """
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and _current_test_usage:
        cost_data = _sum_test_cost()
        report.user_properties.append(("cost", cost_data))
        item.user_properties.append(("cost", cost_data))


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
    result = resp.json()
    _track_usage(result)
    return result


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
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()
    _track_usage(result)
    return result


def chat_with_context(prompt: str, *, table_view: dict | None = None, headers=None):
    """POST to /chat with optional tableView context."""
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "context": {"app": "vendors"},
    }
    if table_view:
        payload["context"]["tableView"] = table_view
    resp = requests.post(
        f"{BASE_URL}/chat",
        json=payload,
        headers=headers or DEFAULT_HEADERS,
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()
    _track_usage(result)
    return result


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
    result = resp.json()
    _track_usage(result)
    return result


def make_csv(headers: list[str], rows: list[list[str]]) -> str:
    """Build a CSV string from column headers and data rows."""
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(c) for c in row))
    return "\n".join(lines)
