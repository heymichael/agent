"""Tests for REST endpoints with per-user access control.

Uses FastAPI TestClient with dependency overrides for auth and
@patch for Firestore access, matching the mocking style in test_tools.py.
"""

from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from service.app import app, get_verified_user
from service import firestore_client


# ── Helpers ──────────────────────────────────────────────────────────────

SAMPLE_VENDORS = [
    {"id": "v_acme", "name": "Acme Corp", "department": "Engineering"},
    {"id": "v_beta", "name": "Beta Inc", "department": "Marketing"},
    {"id": "v_gamma", "name": "Gamma LLC", "department": "Engineering"},
]

SAMPLE_SPEND = [
    {"vendor": "Acme Corp", "month": "2026-01", "amount": 10000.0},
    {"vendor": "Acme Corp", "month": "2026-02", "amount": 15000.0},
    {"vendor": "Beta Inc", "month": "2026-01", "amount": 5000.0},
    {"vendor": "Beta Inc", "month": "2026-02", "amount": 8000.0},
    {"vendor": "Gamma LLC", "month": "2026-01", "amount": 20000.0},
]


def _make_client(email="test@example.com"):
    """Create a TestClient with auth overridden to return the given email."""
    app.dependency_overrides[get_verified_user] = lambda: {"email": email}
    client = TestClient(app)
    return client


def _teardown():
    app.dependency_overrides.clear()


def _access_ctx(roles=None, departments=None, vendor_ids=None, denied=None):
    """Build a user access context dict."""
    return {
        "roles": roles or [],
        "allowed_departments": departments or [],
        "allowed_vendor_ids": vendor_ids or [],
        "denied_vendor_ids": denied or [],
    }


# ── GET /me ──────────────────────────────────────────────────────────────

SAMPLE_USER = {
    "email": "test@example.com",
    "firstName": "Test",
    "lastName": "User",
    "roles": ["viewer"],
    "allowedDepartments": [],
    "allowedVendorIds": [],
    "deniedVendorIds": [],
    "allowedVendors": [],
}


class TestGetCurrentUser:

    def teardown_method(self):
        _teardown()

    @patch.object(firestore_client, "get_user")
    def test_returns_current_user(self, mock_get):
        mock_get.return_value = SAMPLE_USER
        client = _make_client("test@example.com")

        resp = client.get("/me")

        assert resp.status_code == 200
        assert resp.json()["email"] == "test@example.com"
        assert resp.json()["roles"] == ["viewer"]
        mock_get.assert_called_once_with("test@example.com")

    @patch.object(firestore_client, "get_user")
    def test_normalizes_email(self, mock_get):
        mock_get.return_value = SAMPLE_USER
        client = _make_client("  Test@Example.COM  ")

        resp = client.get("/me")

        assert resp.status_code == 200
        mock_get.assert_called_once_with("test@example.com")

    @patch.object(firestore_client, "get_user")
    def test_user_not_found_returns_404(self, mock_get):
        mock_get.return_value = None
        client = _make_client("nobody@example.com")

        resp = client.get("/me")

        assert resp.status_code == 404

    def test_unauthenticated_returns_401(self):
        app.dependency_overrides.clear()
        client = TestClient(app)

        resp = client.get("/me")

        assert resp.status_code == 401


# ── GET /vendors ─────────────────────────────────────────────────────────

class TestListVendors:

    def teardown_method(self):
        _teardown()

    @patch.object(firestore_client, "resolve_effective_vendor_ids")
    @patch.object(firestore_client, "get_user_access_context")
    @patch.object(firestore_client, "list_vendors")
    def test_finance_admin_sees_all(self, mock_list, mock_access, mock_resolve):
        mock_list.return_value = SAMPLE_VENDORS
        mock_access.return_value = _access_ctx(roles=["finance_admin"])
        client = _make_client()

        resp = client.get("/vendors")

        assert resp.status_code == 200
        assert len(resp.json()) == 3
        mock_resolve.assert_not_called()

    @patch.object(firestore_client, "resolve_effective_vendor_ids")
    @patch.object(firestore_client, "get_user_access_context")
    @patch.object(firestore_client, "list_vendors")
    def test_restricted_user_sees_allowed_only(self, mock_list, mock_access, mock_resolve):
        mock_list.return_value = SAMPLE_VENDORS
        mock_access.return_value = _access_ctx(departments=["Engineering"])
        mock_resolve.return_value = ["v_acme", "v_gamma"]
        client = _make_client()

        resp = client.get("/vendors")

        assert resp.status_code == 200
        ids = [v["id"] for v in resp.json()]
        assert ids == ["v_acme", "v_gamma"]

    @patch.object(firestore_client, "resolve_effective_vendor_ids")
    @patch.object(firestore_client, "get_user_access_context")
    @patch.object(firestore_client, "list_vendors")
    def test_no_user_doc_sees_nothing(self, mock_list, mock_access, mock_resolve):
        mock_list.return_value = SAMPLE_VENDORS
        mock_access.return_value = None
        client = _make_client()

        resp = client.get("/vendors")

        assert resp.status_code == 200
        assert resp.json() == []
        mock_resolve.assert_not_called()

    def test_unauthenticated_returns_401(self):
        app.dependency_overrides.clear()
        client = TestClient(app)

        resp = client.get("/vendors")

        assert resp.status_code == 401

    @patch.object(firestore_client, "resolve_effective_vendor_ids")
    @patch.object(firestore_client, "get_user_access_context")
    @patch.object(firestore_client, "list_vendors")
    def test_denied_vendor_excluded(self, mock_list, mock_access, mock_resolve):
        mock_list.return_value = SAMPLE_VENDORS
        mock_access.return_value = _access_ctx(
            departments=["Engineering"],
            denied=["v_gamma"],
        )
        mock_resolve.return_value = ["v_acme"]
        client = _make_client()

        resp = client.get("/vendors")

        assert resp.status_code == 200
        ids = [v["id"] for v in resp.json()]
        assert ids == ["v_acme"]


# ── GET /spend ───────────────────────────────────────────────────────────

class TestGetSpend:

    def teardown_method(self):
        _teardown()

    @patch.object(firestore_client, "query_spend_by_vendor_ids")
    @patch.object(firestore_client, "resolve_effective_vendor_ids")
    @patch.object(firestore_client, "get_user_access_context")
    def test_finance_admin_gets_all_requested(self, mock_access, mock_resolve, mock_spend):
        mock_access.return_value = _access_ctx(roles=["finance_admin"])
        mock_spend.return_value = SAMPLE_SPEND[:2]
        client = _make_client()

        resp = client.get("/spend", params={
            "vendor_ids": ["v_acme"],
            "from": "2026-01",
            "to": "2026-02",
        })

        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 2
        mock_spend.assert_called_once_with(["v_acme"], "2026-01", "2026-02")
        mock_resolve.assert_not_called()

    @patch.object(firestore_client, "query_spend_by_vendor_ids")
    @patch.object(firestore_client, "resolve_effective_vendor_ids")
    @patch.object(firestore_client, "get_user_access_context")
    def test_restricted_user_intersects_with_effective_set(self, mock_access, mock_resolve, mock_spend):
        mock_access.return_value = _access_ctx(departments=["Engineering"])
        mock_resolve.return_value = ["v_acme", "v_gamma"]
        mock_spend.return_value = [SAMPLE_SPEND[0]]
        client = _make_client()

        resp = client.get("/spend", params={
            "vendor_ids": ["v_acme", "v_beta"],
            "from": "2026-01",
            "to": "2026-01",
        })

        assert resp.status_code == 200
        mock_spend.assert_called_once_with(["v_acme"], "2026-01", "2026-01")

    @patch.object(firestore_client, "query_spend_by_vendor_ids")
    @patch.object(firestore_client, "resolve_effective_vendor_ids")
    @patch.object(firestore_client, "get_user_access_context")
    def test_no_user_doc_returns_empty(self, mock_access, mock_resolve, mock_spend):
        mock_access.return_value = None
        mock_spend.return_value = []
        client = _make_client()

        resp = client.get("/spend", params={
            "vendor_ids": ["v_acme"],
            "from": "2026-01",
            "to": "2026-02",
        })

        assert resp.status_code == 200
        assert resp.json()["data"] == []
        mock_spend.assert_called_once_with([], "2026-01", "2026-02")

    def test_missing_params_returns_422(self):
        client = _make_client()

        resp = client.get("/spend", params={"vendor_ids": ["v_acme"]})

        assert resp.status_code == 422

    def test_unauthenticated_returns_401(self):
        app.dependency_overrides.clear()
        client = TestClient(app)

        resp = client.get("/spend", params={
            "vendor_ids": ["v_acme"],
            "from": "2026-01",
            "to": "2026-02",
        })

        assert resp.status_code == 401

    @patch.object(firestore_client, "query_spend_by_vendor_ids")
    @patch.object(firestore_client, "resolve_effective_vendor_ids")
    @patch.object(firestore_client, "get_user_access_context")
    def test_response_shape(self, mock_access, mock_resolve, mock_spend):
        mock_access.return_value = _access_ctx(roles=["finance_admin"])
        mock_spend.return_value = [
            {"vendor": "Acme Corp", "month": "2026-01", "amount": 10000.0},
        ]
        client = _make_client()

        resp = client.get("/spend", params={
            "vendor_ids": ["v_acme"],
            "from": "2026-01",
            "to": "2026-01",
        })

        data = resp.json()
        assert "data" in data
        row = data["data"][0]
        assert set(row.keys()) == {"vendor", "month", "amount"}
