"""Tests for org_slug scoping in pg_client and the caller-context helpers.

Phase 3 of multi-org tenancy (task 254 / strategy 197-r2). These tests
verify that:

1. Vendor and spend query helpers in `service.pg_client` thread
   `org_slug` into the SQL they emit (defense-in-depth gate 5/6 — even
   if a caller hands in cross-tenant IDs, the rows are filtered out at
   the database).
2. `service.app._resolve_caller_access` and
   `service.tools._build_caller_context` propagate the active org slug
   into `resolve_effective_vendor_ids`, and the `finance_admin` bypass
   continues to skip the per-vendor ACL while the org filter still
   applies (it lives below the bypass).
3. `pg_client.list_users` filters by membership join.

All Postgres access is mocked via `pg_client.get_pool` so these tests
run without a database. We assert on the SQL string and parameter list
to prove org_slug shows up where it should.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from service import pg_client, tools
from service.app import _resolve_caller_access

pytestmark = [pytest.mark.expense_analytics, pytest.mark.vendor_management]


# ── Test helpers ─────────────────────────────────────────────────────────


def _norm(sql: str) -> str:
    """Collapse whitespace so SQL substring asserts ignore formatting."""
    return re.sub(r"\s+", " ", sql).strip()


@contextmanager
def _mock_pool(rows: list[dict] | None = None):
    """Patch pg_client.get_pool with a recording cursor.

    The yielded MagicMock is the cursor. Inspect `cursor.execute.call_args`
    for SQL and parameters used by the function under test.
    """
    rows = rows or []
    cursor = MagicMock()
    cursor.execute.return_value.fetchall.return_value = rows
    cursor.execute.return_value.fetchone.return_value = rows[0] if rows else None

    conn = MagicMock()
    conn.execute.side_effect = lambda sql, params=None: cursor.execute(sql, params)

    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value = conn
    pool.connection.return_value.__exit__.return_value = False

    with patch.object(pg_client, "get_pool", return_value=pool):
        yield cursor


def _last_sql_and_params(cursor: MagicMock) -> tuple[str, tuple]:
    """Return the (normalised SQL, params tuple) of the most recent execute()."""
    args, kwargs = cursor.execute.call_args
    return _norm(args[0]), args[1] if len(args) > 1 else kwargs.get("params") or ()


# ── pg_client.list_vendors ──────────────────────────────────────────────


class TestListVendorsOrgScoping:
    """list_vendors must add a `WHERE v.org_slug = %s` clause."""

    def test_list_vendors_filters_by_org_slug(self):
        with _mock_pool(rows=[]) as cursor:
            pg_client.list_vendors("arcade")

        sql, params = _last_sql_and_params(cursor)
        assert "v.org_slug = %s" in sql
        assert params == ("arcade",)

    def test_list_vendors_requires_org_slug_arg(self):
        """Calling without org_slug must fail at type-check / call time, not silently."""
        with pytest.raises(TypeError):
            pg_client.list_vendors()  # type: ignore[call-arg]


# ── pg_client.query_spend_by_vendor_ids ─────────────────────────────────


class TestQuerySpendByVendorIdsOrgScoping:
    """Spend queries must INNER JOIN on vendors.org_slug."""

    def test_spend_query_inner_joins_on_org_slug(self):
        with _mock_pool(rows=[]) as cursor:
            pg_client.query_spend_by_vendor_ids(
                ["v1", "v2"], "2026-01", "2026-03", "arcade",
            )

        sql, params = _last_sql_and_params(cursor)
        assert "INNER JOIN vendors v ON v.id = s.vendor_id AND v.org_slug = %s" in sql
        assert params[0] == "arcade"  # org_slug is the first param
        assert params[1] == ["v1", "v2"]  # vendor_ids comes next

    def test_spend_query_requires_org_slug_arg(self):
        with pytest.raises(TypeError):
            pg_client.query_spend_by_vendor_ids(
                ["v1"], "2026-01", "2026-03",  # type: ignore[call-arg]
            )


# ── pg_client.resolve_effective_vendor_ids ──────────────────────────────


class TestResolveEffectiveVendorIdsOrgScoping:
    """The effective-set resolver must intersect with the active org's vendors."""

    def test_resolve_effective_intersects_with_org(self):
        with _mock_pool(rows=[{"id": "v1"}, {"id": "v2"}]) as cursor:
            pg_client.resolve_effective_vendor_ids(
                allowed_departments=["Engineering"],
                allowed_vendor_ids=["v_extra"],
                denied_vendor_ids=[],
                org_slug="arcade",
            )

        sql, params = _last_sql_and_params(cursor)
        assert "INTERSECT SELECT id FROM vendors WHERE org_slug = %s" in sql
        # org_slug appears as the 4th param of the base SQL
        assert "arcade" in params

    def test_resolve_effective_with_user_id_scopes_contractor_filter_too(self):
        """The contractor exclusion must also be org-scoped (uca + vendors join)."""
        with _mock_pool(rows=[]) as cursor:
            pg_client.resolve_effective_vendor_ids(
                allowed_departments=[],
                allowed_vendor_ids=["v1"],
                denied_vendor_ids=[],
                org_slug="haderach",
                user_id="u-1",
            )

        sql, params = _last_sql_and_params(cursor)
        assert "v.is_contractor = true" in sql
        assert "v.org_slug = %s" in sql
        # Two appearances of haderach: once in INTERSECT, once in
        # contractor exclusion subquery.
        assert params.count("haderach") == 2


# ── pg_client.list_users ────────────────────────────────────────────────


class TestListUsersMembershipFilter:
    """list_users must filter by user_org_memberships join."""

    def test_list_users_no_role_filter_joins_memberships(self):
        with _mock_pool(rows=[]) as cursor:
            pg_client.list_users("arcade")

        sql, params = _last_sql_and_params(cursor)
        assert "FROM user_org_memberships uom" in sql
        assert "uom.org_slug = %s" in sql
        assert params == ("arcade",)

    def test_list_users_with_role_filter_keeps_membership_join(self):
        with _mock_pool(rows=[]) as cursor:
            pg_client.list_users("arcade", roles=["finance_admin"])

        sql, params = _last_sql_and_params(cursor)
        assert "uc.role_names && %s" in sql
        assert "uom.org_slug = %s" in sql
        assert params == (["finance_admin"], "arcade")


# ── _resolve_caller_access (app.py) ─────────────────────────────────────


class TestResolveCallerAccessOrgScoping:
    """The endpoint-side helper must thread org_slug through to pg_client."""

    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    def test_finance_admin_bypass_returns_none(self, mock_ctx, mock_resolve):
        """finance_admin returns None (full access *within active org*).

        The endpoint then queries `pg_client.list_vendors(org_slug)`, so
        org scoping still applies — only the per-user ACL is bypassed.
        """
        mock_ctx.return_value = {"roles": ["finance_admin"]}

        result = _resolve_caller_access({"email": "admin@arcade.com"}, "arcade")

        assert result is None
        mock_resolve.assert_not_called()

    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    def test_restricted_user_passes_org_slug_to_resolve(self, mock_ctx, mock_resolve):
        mock_ctx.return_value = {
            "user_id": "u-1",
            "roles": ["viewer"],
            "allowed_departments": ["Eng"],
            "allowed_vendor_ids": [],
            "denied_vendor_ids": [],
        }
        mock_resolve.return_value = ["v1", "v2"]

        result = _resolve_caller_access({"email": "u@arcade.com"}, "arcade")

        assert result == {"v1", "v2"}
        # org_slug is the 4th positional arg.
        mock_resolve.assert_called_once_with(
            ["Eng"], [], [], "arcade", user_id="u-1",
        )


# ── _build_caller_context (tools.py) ────────────────────────────────────


class TestBuildCallerContextOrgScoping:
    """The tool-handler-side helper must read org_slug from the contextvar."""

    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    def test_finance_admin_context_carries_org_slug(self, mock_ctx, mock_resolve):
        mock_ctx.return_value = {"roles": ["finance_admin"]}
        tools.set_caller_org_slug("arcade")

        try:
            ctx = tools._build_caller_context("admin@arcade.com")
        finally:
            tools.set_caller_org_slug(None)

        assert ctx == {"is_finance_admin": True, "org_slug": "arcade"}
        mock_resolve.assert_not_called()

    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    def test_restricted_user_context_passes_org_slug(self, mock_ctx, mock_resolve):
        mock_ctx.return_value = {
            "user_id": "u-1",
            "roles": ["viewer"],
            "allowed_departments": [],
            "allowed_vendor_ids": ["v_a"],
            "denied_vendor_ids": [],
        }
        mock_resolve.return_value = ["v_a"]
        tools.set_caller_org_slug("haderach")

        try:
            ctx = tools._build_caller_context("u@haderach.com")
        finally:
            tools.set_caller_org_slug(None)

        assert ctx == {
            "allowed_vendor_ids": ["v_a"],
            "is_finance_admin": False,
            "org_slug": "haderach",
        }
        mock_resolve.assert_called_once_with(
            [], ["v_a"], [], "haderach", user_id="u-1",
        )

    @patch.object(pg_client, "resolve_effective_vendor_ids")
    @patch.object(pg_client, "get_user_access_context")
    def test_no_org_slug_yields_empty_allowed(self, mock_ctx, mock_resolve):
        """Without an active org, restricted users see no vendors (fail closed)."""
        mock_ctx.return_value = {
            "user_id": "u-1",
            "roles": ["viewer"],
            "allowed_departments": ["Eng"],
            "allowed_vendor_ids": [],
            "denied_vendor_ids": [],
        }
        tools.set_caller_org_slug(None)

        ctx = tools._build_caller_context("u@arcade.com")

        assert ctx == {
            "allowed_vendor_ids": [],
            "is_finance_admin": False,
            "org_slug": None,
        }
        mock_resolve.assert_not_called()


# ── _require_caller_org_slug ────────────────────────────────────────────


class TestRequireCallerOrgSlug:
    """Tool helpers that hit org-scoped SQL must fail loudly when slug is unset."""

    def test_unset_raises_runtime_error(self):
        tools.set_caller_org_slug(None)

        with pytest.raises(RuntimeError) as exc:
            tools._require_caller_org_slug()

        assert "Caller org slug not set" in str(exc.value)

    def test_set_returns_value(self):
        tools.set_caller_org_slug("arcade")
        try:
            assert tools._require_caller_org_slug() == "arcade"
        finally:
            tools.set_caller_org_slug(None)


# ── User-record CRUD org scoping ────────────────────────────────────────


class TestGetUserInOrgScoping:
    """`get_user_in_org` must membership-gate the lookup."""

    def _user_row(self, *, uid: str = "u-1", email: str = "x@arcade.com") -> dict:
        return {"id": uid, "email": email, "first_name": "X", "last_name": "Y"}

    def test_returns_user_when_member(self):
        user_row = self._user_row()
        membership_row = {"?column?": 1}

        with _mock_pool() as cursor:
            cursor.execute.return_value.fetchone.side_effect = [user_row, membership_row]
            result = pg_client.get_user_in_org("x@arcade.com", "arcade")

        assert result is not None
        assert result["email"] == "x@arcade.com"

    def test_returns_none_when_not_member(self):
        user_row = self._user_row()

        with _mock_pool() as cursor:
            # First fetchone returns the user, second (membership probe) returns None.
            cursor.execute.return_value.fetchone.side_effect = [user_row, None]
            result = pg_client.get_user_in_org("x@arcade.com", "haderach")

        assert result is None

    def test_returns_none_when_user_missing(self):
        with _mock_pool() as cursor:
            cursor.execute.return_value.fetchone.side_effect = [None]
            result = pg_client.get_user_in_org("ghost@arcade.com", "arcade")

        assert result is None


class TestUpdateUserOrgScoping:
    """`update_user` must membership-gate the target user."""

    def test_raises_when_target_not_member(self):
        user_row = {"id": "u-1", "email": "x@arcade.com", "first_name": "X", "last_name": "Y"}

        with _mock_pool() as cursor:
            cursor.execute.return_value.fetchone.side_effect = [user_row, None]
            with pytest.raises(ValueError, match="not found"):
                pg_client.update_user("x@arcade.com", "haderach", first_name="New")

    def test_raises_when_user_missing(self):
        with _mock_pool() as cursor:
            cursor.execute.return_value.fetchone.side_effect = [None]
            with pytest.raises(ValueError, match="not found"):
                pg_client.update_user("ghost@arcade.com", "arcade", first_name="N")


class TestDeleteUserOrgScoping:
    """`delete_user` must membership-gate the target user."""

    def test_returns_false_when_target_not_member(self):
        user_row = {"id": "u-1"}

        with _mock_pool() as cursor:
            cursor.execute.return_value.fetchone.side_effect = [user_row, None]
            result = pg_client.delete_user("x@arcade.com", "haderach")

        assert result is False

    def test_returns_false_when_user_missing(self):
        with _mock_pool() as cursor:
            cursor.execute.return_value.fetchone.side_effect = [None]
            result = pg_client.delete_user("ghost@arcade.com", "arcade")

        assert result is False


class TestCreateUserOrgScoping:
    """`create_user` must add a membership row in the active org."""

    def test_inserts_membership_in_active_org(self):
        with _mock_pool() as cursor:
            # 1) duplicate-check returns no row
            # 2) INSERT into users RETURNING the new row
            # 3) INSERT into memberships
            # 4) get_user_context_row returns the created context
            # fetchone is invoked for: (1) duplicate-email probe, (2) the
            # user INSERT RETURNING, (3) the final _get_user_context_row.
            # The membership INSERT and any role INSERTs do not call fetchone.
            cursor.execute.return_value.fetchone.side_effect = [
                None,
                {"id": "new-uid", "email": "new@arcade.com", "first_name": "N", "last_name": "U"},
                {
                    "id": "new-uid",
                    "email": "new@arcade.com",
                    "first_name": "N",
                    "last_name": "U",
                    "role_names": [],
                    "allowed_departments": [],
                    "allowed_vendor_ids": [],
                    "denied_vendor_ids": [],
                    "orgs": [{"slug": "arcade"}],
                },
            ]

            result = pg_client.create_user("new@arcade.com", "N", "U", [], "arcade")

        assert result["email"] == "new@arcade.com"

        membership_calls = [
            call for call in cursor.execute.call_args_list
            if "user_org_memberships" in call.args[0]
        ]
        assert len(membership_calls) == 1
        assert membership_calls[0].args[1] == ("new-uid", "arcade")

    def test_raises_when_email_already_exists(self):
        with _mock_pool() as cursor:
            cursor.execute.return_value.fetchone.side_effect = [{"id": "existing"}]
            with pytest.raises(ValueError, match="already exists"):
                pg_client.create_user("dup@arcade.com", "D", "U", [], "arcade")
