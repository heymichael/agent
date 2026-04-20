"""Tests for REST endpoints with per-user access control.

Uses FastAPI TestClient with dependency overrides for auth and
@patch for Postgres access via pg_client.
"""

from unittest.mock import patch, MagicMock

import pytest

pytestmark = [pytest.mark.expense_analytics, pytest.mark.vendor_management]

from fastapi.testclient import TestClient

from service.app import app, get_verified_user
from service.auth import get_caller_enabled_apps
from service import pg_client


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

# Default to Arcade's full app set so existing tests don't have to thread
# entitlement awareness through every assertion. Tests that want to
# exercise require_app's 403 path pass `enabled_apps=[]` (or a list
# excluding the relevant slug).
_DEFAULT_ENABLED_APPS = [
    "expenses", "vendors", "vendor_administration", "system_administration",
]


def _make_client(
    email="test@example.com",
    active_org_slug="arcade",
    enabled_apps=None,
):
    """Create a TestClient with auth overridden to return the given email.

    Phase 3 of multi-org tenancy (task 254): the override now also
    sets `active_org_slug` since most endpoints call
    `_require_active_org(caller)` and reject callers without one.
    Pass `active_org_slug=None` explicitly to test the
    `Active-Org-Required` (400) path.

    Phase 4 of multi-org tenancy (task 254): also overrides
    `get_caller_enabled_apps` so the `require_app(...)` dependencies on
    domain endpoints don't fall through to a real DB lookup. Defaults
    to Arcade's enabled-apps set; tests that need to exercise the
    `App-Not-Enabled` 403 path pass `enabled_apps=[]` or omit the
    relevant slug.
    """
    app.dependency_overrides[get_verified_user] = lambda: {
        "email": email,
        "active_org_slug": active_org_slug,
    }
    app.dependency_overrides[get_caller_enabled_apps] = lambda: list(
        enabled_apps if enabled_apps is not None else _DEFAULT_ENABLED_APPS
    )
    client = TestClient(app)
    return client


def _teardown():
    app.dependency_overrides.clear()


def _access_ctx(roles=None, departments=None, vendor_ids=None, denied=None):
    """Build a user access context dict."""
    return {
        "user_id": "uid-test",
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
    """The /me endpoint must return the authenticated user's profile or appropriate error codes."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "get_user")
    def test_returns_current_user(self, mock_get):
        """An authenticated user with a database record must receive their full profile."""
        mock_get.return_value = SAMPLE_USER
        client = _make_client("test@example.com")

        resp = client.get("/me")

        assert resp.status_code == 200
        assert resp.json()["email"] == "test@example.com"
        assert resp.json()["roles"] == ["viewer"]
        mock_get.assert_called_once_with("test@example.com")

    @patch.object(pg_client, "get_user")
    def test_normalizes_email(self, mock_get):
        """Email addresses must be lowercased and trimmed before database lookup."""
        mock_get.return_value = SAMPLE_USER
        client = _make_client("  Test@Example.COM  ")

        resp = client.get("/me")

        assert resp.status_code == 200
        mock_get.assert_called_once_with("test@example.com")

    @patch.object(pg_client, "get_user")
    def test_user_not_found_returns_404(self, mock_get):
        """An authenticated email with no database record must return 404."""
        mock_get.return_value = None
        client = _make_client("nobody@example.com")

        resp = client.get("/me")

        assert resp.status_code == 404

    @patch("service.auth._DEV_AUTH_EMAIL", None)
    def test_unauthenticated_returns_401(self):
        """Requests without valid authentication must be rejected with 401."""
        app.dependency_overrides.clear()
        client = TestClient(app)

        resp = client.get("/me")

        assert resp.status_code == 401


# ── GET /qbo/callback ─────────────────────────────────────────────────────

class TestQboCallback:
    """The QBO OAuth callback must enforce CSRF protection and render user-facing HTML responses."""

    STATE = "test-csrf-state-token"

    def _client_with_state(self):
        client = TestClient(app)
        client.cookies.set("qbo_oauth_state", self.STATE)
        return client

    @patch("service.app.exchange_code_for_tokens")
    def test_success_renders_html_with_token(self, mock_exchange):
        """A valid code exchange must render an HTML success page containing the token and realm."""
        mock_exchange.return_value = {
            "refresh_token": "refresh-token-123",
            "expires_in": 3600,
            "x_refresh_token_expires_in": 8640000,
        }
        client = self._client_with_state()

        resp = client.get("/qbo/callback", params={
            "code": "abc", "realmId": "12345", "state": self.STATE,
        })

        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "QuickBooks connected" in resp.text
        assert "refresh-token-123" in resp.text
        assert "12345" in resp.text
        assert 'href="/integrations/quickbooks/connect"' in resp.text
        assert "Return to Haderach" in resp.text

    def test_missing_code_renders_error_html(self):
        """A callback missing the authorization code must render a user-facing error page."""
        client = TestClient(app)

        resp = client.get("/qbo/callback")

        assert resp.status_code == 400
        assert "text/html" in resp.headers["content-type"]
        assert "QuickBooks connection failed" in resp.text
        assert "Missing authorization code" in resp.text
        assert "Try connecting again" in resp.text

    def test_intuit_error_renders_error_html(self):
        """An Intuit-reported error param must render a user-facing error page with the error detail."""
        client = TestClient(app)

        resp = client.get("/qbo/callback", params={"error": "access_denied"})

        assert resp.status_code == 400
        assert "text/html" in resp.headers["content-type"]
        assert "QuickBooks connection failed" in resp.text
        assert "access_denied" in resp.text
        assert "Try connecting again" in resp.text

    def test_csrf_mismatch_returns_403(self):
        """A state param that doesn't match the cookie must be rejected as a CSRF violation."""
        client = self._client_with_state()

        resp = client.get("/qbo/callback", params={
            "code": "abc", "realmId": "12345", "state": "wrong-state",
        })

        assert resp.status_code == 403
        assert "CSRF validation failed" in resp.text
        assert "Try connecting again" in resp.text

    def test_csrf_missing_cookie_returns_403(self):
        """A callback without a CSRF cookie must be rejected even if the state param is present."""
        client = TestClient(app)

        resp = client.get("/qbo/callback", params={
            "code": "abc", "realmId": "12345", "state": "some-state",
        })

        assert resp.status_code == 403
        assert "CSRF validation failed" in resp.text

    def test_csrf_missing_state_param_returns_403(self):
        """A callback without a state query param must be rejected as a CSRF violation."""
        client = self._client_with_state()

        resp = client.get("/qbo/callback", params={
            "code": "abc", "realmId": "12345",
        })

        assert resp.status_code == 403
        assert "CSRF validation failed" in resp.text


# ── GET /vendors ─────────────────────────────────────────────────────────

class TestListVendors:
    """Vendor listing must enforce role-based and department-scoped access control."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    @patch.object(pg_client, "list_vendors")
    def test_finance_admin_sees_all(self, mock_list, mock_access, mock_resolve):
        """Finance admins must see every vendor without effective-ID resolution."""
        mock_list.return_value = SAMPLE_VENDORS
        mock_access.return_value = _access_ctx(roles=["finance_admin"])
        client = _make_client()

        resp = client.get("/vendors")

        assert resp.status_code == 200
        assert len(resp.json()) == 3
        mock_resolve.assert_not_called()

    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    @patch.object(pg_client, "list_vendors")
    def test_restricted_user_sees_allowed_only(self, mock_list, mock_access, mock_resolve):
        """Non-admin users must only see vendors within their allowed departments."""
        mock_list.return_value = SAMPLE_VENDORS
        mock_access.return_value = _access_ctx(departments=["Engineering"])
        mock_resolve.return_value = ["v_acme", "v_gamma"]
        client = _make_client()

        resp = client.get("/vendors")

        assert resp.status_code == 200
        ids = [v["id"] for v in resp.json()]
        assert ids == ["v_acme", "v_gamma"]

    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    @patch.object(pg_client, "list_vendors")
    def test_no_user_doc_sees_nothing(self, mock_list, mock_access, mock_resolve):
        """A user with no access context document must receive an empty vendor list."""
        mock_list.return_value = SAMPLE_VENDORS
        mock_access.return_value = None
        client = _make_client()

        resp = client.get("/vendors")

        assert resp.status_code == 200
        assert resp.json() == []
        mock_resolve.assert_not_called()

    @patch("service.auth._DEV_AUTH_EMAIL", None)
    def test_unauthenticated_returns_401(self):
        """Unauthenticated requests to /vendors must be rejected with 401."""
        app.dependency_overrides.clear()
        client = TestClient(app)

        resp = client.get("/vendors")

        assert resp.status_code == 401

    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    @patch.object(pg_client, "list_vendors")
    def test_denied_vendor_excluded(self, mock_list, mock_access, mock_resolve):
        """Explicitly denied vendor IDs must be excluded even if the department is allowed."""
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
    """Spend queries must enforce the same access-control rules as vendor listing."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "query_spend_by_vendor_ids")
    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    def test_finance_admin_gets_all_requested(self, mock_access, mock_resolve, mock_spend):
        """Finance admins must receive spend data for all requested vendor IDs without filtering."""
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
        mock_spend.assert_called_once_with(["v_acme"], "2026-01", "2026-02", "arcade")
        mock_resolve.assert_not_called()

    @patch.object(pg_client, "query_spend_by_vendor_ids")
    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    def test_restricted_user_intersects_with_effective_set(self, mock_access, mock_resolve, mock_spend):
        """Requested vendor IDs must be intersected with the user's effective set before querying spend."""
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
        mock_spend.assert_called_once_with(["v_acme"], "2026-01", "2026-01", "arcade")

    @patch.object(pg_client, "query_spend_by_vendor_ids")
    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    def test_no_user_doc_returns_empty(self, mock_access, mock_resolve, mock_spend):
        """A user with no access context must receive empty spend data, not an error."""
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
        mock_spend.assert_called_once_with([], "2026-01", "2026-02", "arcade")

    def test_missing_params_returns_422(self):
        """Omitting required query params (from/to) must return 422, not a server error."""
        client = _make_client()

        resp = client.get("/spend", params={"vendor_ids": ["v_acme"]})

        assert resp.status_code == 422

    @patch("service.auth._DEV_AUTH_EMAIL", None)
    def test_unauthenticated_returns_401(self):
        """Unauthenticated requests to /spend must be rejected with 401."""
        app.dependency_overrides.clear()
        client = TestClient(app)

        resp = client.get("/spend", params={
            "vendor_ids": ["v_acme"],
            "from": "2026-01",
            "to": "2026-02",
        })

        assert resp.status_code == 401

    @patch.object(pg_client, "query_spend_by_vendor_ids")
    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    def test_response_shape(self, mock_access, mock_resolve, mock_spend):
        """Spend responses must contain a 'data' array with vendor/month/amount keys."""
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


# ── POST /feedback/site ──────────────────────────────────────────────────

class TestSiteFeedback:
    """Site feedback submission must validate required fields and resolve the submitting user."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "insert_site_feedback")
    @patch.object(pg_client, "get_user_id_by_email")
    def test_submit_feedback(self, mock_uid, mock_insert):
        """Valid feedback with all fields must be persisted with the resolved user ID."""
        mock_uid.return_value = "user-uuid-123"
        client = _make_client()

        resp = client.post("/feedback/site", json={
            "app_id": "vendors",
            "open_panes": {"chat": True, "analytics": False, "data": False},
            "feedback_text": "Great app!",
        })

        assert resp.status_code == 201
        assert resp.json() == {"ok": True}
        mock_insert.assert_called_once_with(
            user_id="user-uuid-123",
            app_id="vendors",
            open_panes={"chat": True, "analytics": False, "data": False},
            feedback_text="Great app!",
        )

    @patch.object(pg_client, "insert_site_feedback")
    @patch.object(pg_client, "get_user_id_by_email")
    def test_submit_feedback_null_panes(self, mock_uid, mock_insert):
        """Feedback without open_panes must be accepted with panes stored as null."""
        mock_uid.return_value = "user-uuid-123"
        client = _make_client()

        resp = client.post("/feedback/site", json={
            "app_id": "card",
            "feedback_text": "Needs dark mode",
        })

        assert resp.status_code == 201
        mock_insert.assert_called_once_with(
            user_id="user-uuid-123",
            app_id="card",
            open_panes=None,
            feedback_text="Needs dark mode",
        )

    @patch.object(pg_client, "get_user_id_by_email")
    def test_unknown_user_returns_403(self, mock_uid):
        """An email that cannot be resolved to a user ID must be rejected with 403."""
        mock_uid.return_value = None
        client = _make_client()

        resp = client.post("/feedback/site", json={
            "app_id": "vendors",
            "feedback_text": "Hello",
        })

        assert resp.status_code == 403

    @patch("service.auth._DEV_AUTH_EMAIL", None)
    def test_unauthenticated_returns_401(self):
        """Unauthenticated feedback submissions must be rejected with 401."""
        app.dependency_overrides.clear()
        client = TestClient(app)

        resp = client.post("/feedback/site", json={
            "app_id": "vendors",
            "feedback_text": "Hello",
        })

        assert resp.status_code == 401

    def test_missing_feedback_text_returns_422(self):
        """Omitting the required feedback_text field must return 422."""
        client = _make_client()

        resp = client.post("/feedback/site", json={
            "app_id": "vendors",
        })

        assert resp.status_code == 422

    def test_missing_app_id_returns_422(self):
        """Omitting the required app_id field must return 422."""
        client = _make_client()

        resp = client.post("/feedback/site", json={
            "feedback_text": "Hello",
        })

        assert resp.status_code == 422


# ── Contractor management endpoints ─────────────────────────────────────

SAMPLE_VENDOR_ALPHA = {
    "id": "v_alpha", "name": "Alpha Corp", "department": "Engineering",
    "isContractor": False,
}

FINANCE_ADMIN_USER = {
    "id": "uid-fa",
    "email": "fa@example.com",
    "firstName": "Finance",
    "lastName": "Admin",
    "roles": ["finance_admin"],
    "allowedDepartments": [],
    "allowedVendorIds": [],
    "deniedVendorIds": [],
    "allowedVendors": [],
}

REGULAR_USER = {
    "id": "uid-reg",
    "email": "user@example.com",
    "firstName": "Regular",
    "lastName": "User",
    "roles": ["viewer"],
    "allowedDepartments": [],
    "allowedVendorIds": [],
    "deniedVendorIds": [],
    "allowedVendors": [],
}


class TestSetContractor:
    """Only finance admins may toggle a vendor's contractor status."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "set_vendor_is_contractor")
    @patch.object(pg_client, "get_user_id_by_email")
    @patch.object(pg_client, "get_user")
    def test_finance_admin_can_set(self, mock_get_user, mock_uid, mock_set):
        """A finance admin must be able to set a vendor's contractor flag."""
        mock_get_user.return_value = FINANCE_ADMIN_USER
        mock_uid.return_value = "uid-fa"
        mock_set.return_value = {**SAMPLE_VENDOR_ALPHA, "isContractor": True}
        client = _make_client("fa@example.com")

        resp = client.patch("/vendors/v_alpha/contractor", json={"is_contractor": True})

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_set.assert_called_once_with("v_alpha", True, "uid-fa", "arcade")

    @patch.object(pg_client, "get_user")
    def test_non_finance_admin_rejected(self, mock_get_user):
        """Non-finance-admin users must be rejected with 403 when toggling contractor status."""
        mock_get_user.return_value = REGULAR_USER
        client = _make_client("user@example.com")

        resp = client.patch("/vendors/v_alpha/contractor", json={"is_contractor": True})

        assert resp.status_code == 403


class TestListContractors:
    """Only finance admins may list contractor vendors."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "list_contractor_vendors")
    @patch.object(pg_client, "get_user")
    def test_finance_admin_can_list(self, mock_get_user, mock_list):
        """A finance admin must receive the full contractor vendor list."""
        mock_get_user.return_value = FINANCE_ADMIN_USER
        mock_list.return_value = [SAMPLE_VENDOR_ALPHA]
        client = _make_client("fa@example.com")

        resp = client.get("/vendors/contractors")

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @patch.object(pg_client, "get_user")
    def test_non_finance_admin_rejected(self, mock_get_user):
        """Non-finance-admin users must be rejected with 403 when listing contractors."""
        mock_get_user.return_value = REGULAR_USER
        client = _make_client("user@example.com")

        resp = client.get("/vendors/contractors")

        assert resp.status_code == 403


class TestContractorAccessGrant:
    """Contractor access grants require finance-admin role and a valid vendor."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "grant_contractor_access")
    @patch.object(pg_client, "get_vendor")
    @patch.object(pg_client, "get_user_id_by_email")
    @patch.object(pg_client, "get_user")
    def test_grant_access(self, mock_get_user, mock_uid, mock_get_vendor, mock_grant):
        """A finance admin must be able to grant a target user access to a contractor vendor."""
        mock_get_user.return_value = FINANCE_ADMIN_USER
        mock_uid.side_effect = lambda e: "uid-fa" if e == "fa@example.com" else "uid-target"
        mock_get_vendor.return_value = SAMPLE_VENDOR_ALPHA
        client = _make_client("fa@example.com")

        resp = client.post(
            "/vendors/v_alpha/access",
            json={"user_email": "target@example.com"},
        )

        assert resp.status_code == 201
        assert resp.json()["ok"] is True
        mock_grant.assert_called_once_with("uid-target", "v_alpha", "uid-fa")

    @patch.object(pg_client, "get_user")
    def test_non_finance_admin_rejected(self, mock_get_user):
        """Non-finance-admin users must be rejected with 403 when granting contractor access."""
        mock_get_user.return_value = REGULAR_USER
        client = _make_client("user@example.com")

        resp = client.post(
            "/vendors/v_alpha/access",
            json={"user_email": "target@example.com"},
        )

        assert resp.status_code == 403


class TestContractorAccessRevoke:
    """Contractor access revocation requires finance-admin role and an existing vendor."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "revoke_contractor_access")
    @patch.object(pg_client, "get_vendor")
    @patch.object(pg_client, "get_user_id_by_email")
    @patch.object(pg_client, "get_user")
    def test_revoke_access(self, mock_get_user, mock_uid, mock_get_vendor, mock_revoke):
        """A finance admin must be able to revoke a target user's access to a contractor vendor."""
        mock_get_user.return_value = FINANCE_ADMIN_USER
        mock_uid.return_value = "uid-target"
        mock_get_vendor.return_value = SAMPLE_VENDOR_ALPHA
        client = _make_client("fa@example.com")

        resp = client.delete("/vendors/v_alpha/access/target@example.com")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_revoke.assert_called_once_with("uid-target", "v_alpha")

    @patch.object(pg_client, "get_user")
    def test_non_finance_admin_rejected(self, mock_get_user):
        """Non-finance-admin users must be rejected with 403 when revoking contractor access."""
        mock_get_user.return_value = REGULAR_USER
        client = _make_client("user@example.com")

        resp = client.delete("/vendors/v_alpha/access/target@example.com")

        assert resp.status_code == 403

    @patch.object(pg_client, "get_vendor")
    @patch.object(pg_client, "get_user_id_by_email")
    @patch.object(pg_client, "get_user")
    def test_vendor_not_found(self, mock_get_user, mock_uid, mock_get_vendor):
        """Revoking access for a nonexistent vendor must return 404."""
        mock_get_user.return_value = FINANCE_ADMIN_USER
        mock_uid.return_value = "uid-target"
        mock_get_vendor.return_value = None
        client = _make_client("fa@example.com")

        resp = client.delete("/vendors/v_alpha/access/target@example.com")

        assert resp.status_code == 404


class TestListContractorAccess:
    """Listing contractor access grants requires finance-admin role and a valid vendor."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "list_contractor_access")
    @patch.object(pg_client, "get_vendor")
    @patch.object(pg_client, "get_user")
    def test_list_access(self, mock_get_user, mock_get_vendor, mock_list):
        """A finance admin must see the full list of users with access to a contractor vendor."""
        mock_get_user.return_value = FINANCE_ADMIN_USER
        mock_get_vendor.return_value = SAMPLE_VENDOR_ALPHA
        mock_list.return_value = [
            {"userId": "uid-1", "email": "u1@co.com",
             "firstName": "A", "lastName": "B",
             "grantedBy": "fa@co.com", "grantedAt": "2026-01-01T00:00:00Z"},
        ]
        client = _make_client("fa@example.com")

        resp = client.get("/vendors/v_alpha/access")

        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["email"] == "u1@co.com"

    @patch.object(pg_client, "get_user")
    def test_non_finance_admin_rejected(self, mock_get_user):
        """Non-finance-admin users must be rejected with 403 when listing contractor access."""
        mock_get_user.return_value = REGULAR_USER
        client = _make_client("user@example.com")

        resp = client.get("/vendors/v_alpha/access")

        assert resp.status_code == 403


# ── QBO auth error handling ──────────────────────────────────────────────

class TestQboAuthErrors:
    """QBO token operations must surface structured QBOAuthError for all failure modes."""

    @patch("service.qbo_auth.requests.post")
    @patch("service.qbo_auth._load_creds")
    def test_refresh_invalid_grant_raises_qbo_auth_error(self, mock_creds, mock_post):
        """An invalid_grant response during token refresh must raise QBOAuthError with re-auth guidance."""
        from service.qbo_auth import refresh_access_token, QBOAuthError

        mock_creds.return_value = {
            "client_id": "id", "client_secret": "secret", "refresh_token": "old-token",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Token expired",
        }
        mock_resp.text = '{"error":"invalid_grant"}'
        mock_post.return_value = mock_resp

        with pytest.raises(QBOAuthError) as exc_info:
            refresh_access_token()

        assert exc_info.value.error_code == "invalid_grant"
        assert "Re-authorize" in str(exc_info.value)

    @patch("service.qbo_auth.requests.post")
    @patch("service.qbo_auth._load_creds")
    def test_refresh_unknown_error_raises_qbo_auth_error(self, mock_creds, mock_post):
        """A non-400 server error during token refresh must raise QBOAuthError with the status code."""
        from service.qbo_auth import refresh_access_token, QBOAuthError

        mock_creds.return_value = {
            "client_id": "id", "client_secret": "secret", "refresh_token": "old-token",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"error": "server_error"}
        mock_resp.text = '{"error":"server_error"}'
        mock_post.return_value = mock_resp

        with pytest.raises(QBOAuthError) as exc_info:
            refresh_access_token()

        assert exc_info.value.error_code == "server_error"
        assert "500" in str(exc_info.value)

    def test_missing_refresh_token_raises_qbo_auth_error(self):
        """A missing refresh token in credentials must raise QBOAuthError before any network call."""
        from service.qbo_auth import refresh_access_token, QBOAuthError

        with patch("service.qbo_auth._load_creds", return_value={"client_id": "id", "client_secret": "s"}):
            with pytest.raises(QBOAuthError) as exc_info:
                refresh_access_token()

        assert exc_info.value.error_code == "missing_refresh_token"

    @patch("service.qbo_auth.requests.post")
    @patch("service.qbo_auth._load_creds")
    def test_exchange_invalid_grant_raises_qbo_auth_error(self, mock_creds, mock_post):
        """An invalid_grant during code exchange must raise QBOAuthError, not a generic exception."""
        from service.qbo_auth import exchange_code_for_tokens, QBOAuthError

        mock_creds.return_value = {"client_id": "id", "client_secret": "secret"}
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Code expired",
        }
        mock_resp.text = '{"error":"invalid_grant"}'
        mock_post.return_value = mock_resp

        with pytest.raises(QBOAuthError) as exc_info:
            exchange_code_for_tokens("bad-code", "http://localhost/cb")

        assert exc_info.value.error_code == "invalid_grant"


# ── QBO discovery document ───────────────────────────────────────────────

class TestQboDiscovery:
    """QBO discovery must fetch and cache endpoint URLs, falling back on network errors."""

    def setup_method(self):
        import service.qbo_auth as mod
        self._mod = mod
        mod._discovery_cache.clear()

    @patch("service.qbo_auth.requests.get")
    def test_discovery_fetches_and_caches(self, mock_get):
        """Discovery URLs must be fetched once and then served from cache on subsequent calls."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/oauth2",
            "token_endpoint": "https://token.example.com/bearer",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        assert self._mod._get_auth_url() == "https://auth.example.com/oauth2"
        assert self._mod._get_token_url() == "https://token.example.com/bearer"

        # Second call should use cache, not fetch again
        self._mod._get_auth_url()
        assert mock_get.call_count == 1

    @patch("service.qbo_auth.requests.get")
    def test_discovery_falls_back_on_error(self, mock_get):
        """A network failure during discovery must fall back to hardcoded URLs, not raise."""
        mock_get.side_effect = Exception("network error")

        assert self._mod._get_auth_url() == self._mod._FALLBACK_AUTH_URL
        assert self._mod._get_token_url() == self._mod._FALLBACK_TOKEN_URL
