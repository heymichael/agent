"""Tests for `X-Active-Org` resolution in `get_verified_user`.

Phase 2 of multi-org tenancy (task 254 / strategy 197-r2). The dependency
must surface `caller["active_org_slug"]` from the request header (or the
`active_org` Firebase claim if present), default it for single-membership
users, and enforce gate-3 membership at the auth layer so downstream code
can trust the slug.
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from service import pg_client
from service.app import app
from service.auth import _resolve_active_org_slug, get_verified_user

pytestmark = [pytest.mark.expense_analytics, pytest.mark.vendor_management]


# ── _resolve_active_org_slug (pure logic) ───────────────────────────────


class TestResolveActiveOrgSlug:
    """The slug resolver is the single source of truth for membership/header rules."""

    @patch.object(pg_client, "list_user_org_slugs")
    def test_explicit_header_for_member_org_returns_slug(self, mock_slugs):
        """An explicit X-Active-Org for an org the user belongs to must be honored."""
        mock_slugs.return_value = ["arcade", "haderach"]

        assert _resolve_active_org_slug("u@x.io", "arcade") == "arcade"

    @patch.object(pg_client, "list_user_org_slugs")
    def test_explicit_header_for_non_member_org_raises_403(self, mock_slugs):
        """An explicit X-Active-Org for an org the user does NOT belong to is forbidden."""
        mock_slugs.return_value = ["arcade"]

        with pytest.raises(HTTPException) as exc:
            _resolve_active_org_slug("u@x.io", "haderach")

        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "Active-Org-Forbidden"

    @patch.object(pg_client, "list_user_org_slugs")
    def test_no_header_single_membership_defaults(self, mock_slugs):
        """A single-membership user with no header must auto-default to that slug."""
        mock_slugs.return_value = ["haderach"]

        assert _resolve_active_org_slug("u@x.io", None) == "haderach"

    @patch.object(pg_client, "list_user_org_slugs")
    def test_no_header_multi_membership_raises_400(self, mock_slugs):
        """A multi-membership user with no header must be rejected to force a picker."""
        mock_slugs.return_value = ["arcade", "haderach"]

        with pytest.raises(HTTPException) as exc:
            _resolve_active_org_slug("u@x.io", None)

        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "Active-Org-Required"

    @patch.object(pg_client, "list_user_org_slugs")
    def test_no_header_zero_memberships_returns_none(self, mock_slugs):
        """A zero-membership user with no header must get None, not an error.

        Phase 2 does not enforce data scoping; endpoints that need an org
        will reject downstream, endpoints that don't (like `/me`) still
        respond.
        """
        mock_slugs.return_value = []

        assert _resolve_active_org_slug("u@x.io", None) is None

    @patch.object(pg_client, "list_user_org_slugs")
    def test_explicit_header_with_zero_memberships_raises_403(self, mock_slugs):
        """A zero-membership user explicitly asking for an org is still forbidden."""
        mock_slugs.return_value = []

        with pytest.raises(HTTPException) as exc:
            _resolve_active_org_slug("u@x.io", "arcade")

        assert exc.value.status_code == 403


# ── get_verified_user end-to-end (DEV_AUTH_EMAIL path) ───────────────────


@patch("service.auth._DEV_AUTH_EMAIL", "dev@example.com")
class TestGetVerifiedUserDevPath:
    """In DEV_AUTH_EMAIL mode, header parsing + slug resolution must still run.

    These tests exercise the full FastAPI dependency by hitting `/me` with
    auth NOT overridden (so the real `get_verified_user` runs), with
    `pg_client.get_user` and `pg_client.list_user_org_slugs` mocked.
    """

    def teardown_method(self):
        app.dependency_overrides.clear()

    @patch.object(pg_client, "get_user")
    @patch.object(pg_client, "list_user_org_slugs")
    def test_no_header_single_membership_defaults_to_only_slug(self, mock_slugs, mock_get):
        """A single-membership user gets the default slug attached transparently."""
        mock_slugs.return_value = ["haderach"]
        mock_get.return_value = {"email": "michael@haderach.ai", "roles": [], "orgs": []}

        client = TestClient(app)
        resp = client.get("/me", headers={"X-Test-Email": "michael@haderach.ai"})

        assert resp.status_code == 200
        mock_slugs.assert_called_once_with("michael@haderach.ai")

    @patch.object(pg_client, "get_user")
    @patch.object(pg_client, "list_user_org_slugs")
    def test_explicit_header_member_passes_through(self, mock_slugs, mock_get):
        """X-Active-Org for a member org must succeed."""
        mock_slugs.return_value = ["arcade", "haderach"]
        mock_get.return_value = {"email": "huy@heretic.fund", "roles": [], "orgs": []}

        client = TestClient(app)
        resp = client.get(
            "/me",
            headers={"X-Test-Email": "huy@heretic.fund", "X-Active-Org": "arcade"},
        )

        assert resp.status_code == 200

    @patch.object(pg_client, "list_user_org_slugs")
    def test_multi_membership_no_header_returns_400(self, mock_slugs):
        """A multi-membership user without X-Active-Org gets 400 with the documented code."""
        mock_slugs.return_value = ["arcade", "haderach"]

        client = TestClient(app)
        resp = client.get("/me", headers={"X-Test-Email": "polyglot@example.com"})

        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "Active-Org-Required"

    @patch.object(pg_client, "list_user_org_slugs")
    def test_explicit_header_non_member_returns_403(self, mock_slugs):
        """Asking for an org you're not a member of is forbidden, not silently overridden."""
        mock_slugs.return_value = ["arcade"]

        client = TestClient(app)
        resp = client.get(
            "/me",
            headers={"X-Test-Email": "huy@heretic.fund", "X-Active-Org": "haderach"},
        )

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "Active-Org-Forbidden"


# ── /me carries `orgs` from _context_row_to_user ─────────────────────────


class TestMeOrgsShape:
    """`/me` must surface the user's org memberships from the user_context view."""

    def teardown_method(self):
        app.dependency_overrides.clear()

    @patch.object(pg_client, "get_user")
    def test_me_includes_orgs_array(self, mock_get):
        """The shape returned by /me must carry `orgs: [{slug, name, enabledApps}]`."""
        app.dependency_overrides[get_verified_user] = lambda: {
            "email": "michael@haderach.ai",
            "active_org_slug": "haderach",
        }
        mock_get.return_value = {
            "email": "michael@haderach.ai",
            "firstName": "Michael",
            "lastName": "Mader",
            "roles": ["admin"],
            "orgs": [
                {"slug": "haderach", "name": "Haderach",
                 "enabledApps": ["site", "system_administration"]},
            ],
        }

        client = TestClient(app)
        resp = client.get("/me")

        assert resp.status_code == 200
        body = resp.json()
        assert body["orgs"] == [
            {"slug": "haderach", "name": "Haderach",
             "enabledApps": ["site", "system_administration"]},
        ]


# ── _context_row_to_user shape ───────────────────────────────────────────


class TestContextRowToUser:
    """The Python shaping helper must surface orgs verbatim from the view."""

    def test_orgs_passed_through_unchanged(self):
        """jsonb orgs from the view must serve through as-is (already camelCased)."""
        from service.pg_client import _context_row_to_user

        row = {
            "id": "uid-1",
            "email": "michael@haderach.ai",
            "first_name": "Michael",
            "last_name": "Mader",
            "role_names": ["admin"],
            "allowed_departments": [],
            "allowed_vendor_ids": [],
            "denied_vendor_ids": [],
            "allowed_vendors": [],
            "orgs": [
                {"slug": "haderach", "name": "Haderach",
                 "enabledApps": ["site", "system_administration"]},
            ],
        }

        out = _context_row_to_user(row)

        assert out["orgs"] == [
            {"slug": "haderach", "name": "Haderach",
             "enabledApps": ["site", "system_administration"]},
        ]

    def test_orgs_defaults_to_empty_list_when_missing(self):
        """A user with no memberships must surface as `orgs: []`, never null."""
        from service.pg_client import _context_row_to_user

        row = {
            "id": "uid-1",
            "email": "newbie@example.com",
            "first_name": "",
            "last_name": "",
            "role_names": [],
            "allowed_departments": [],
            "allowed_vendor_ids": [],
            "denied_vendor_ids": [],
            "allowed_vendors": [],
            "orgs": None,
        }

        out = _context_row_to_user(row)

        assert out["orgs"] == []
