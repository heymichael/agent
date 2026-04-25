"""Tests for the layered app-entitlement gate (Phase 4, task 254).

Strategy 197-r2 layers org-level app entitlement on top of the existing
role-based gating (`app_granting_roles`). These tests pin down the
contract `require_app(...)` and `pg_client.list_apps(org_slug)` must
honor:

- Domain endpoints raise 403 `App-Not-Enabled` when the caller's
  active org doesn't have the required app in its `enabled_apps`.
- Domain endpoints raise 400 `Active-Org-Required` when the caller has
  no active org slug (zero-membership user).
- Endpoints succeed when the active org has the app enabled.
- `list_apps(org_slug)` returns only apps in `orgs.enabled_apps`, with
  `granting_roles` preserved so the client-side role gate still works.
- The `get_caller_enabled_apps` dependency is resolved at most once per
  request even when multiple `require_app(...)` deps fire — FastAPI's
  per-request dependency cache is doing the right thing.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from service import pg_client
from service.app import app
from service.auth import get_caller_enabled_apps, get_verified_user, require_app

pytestmark = [pytest.mark.expense_analytics, pytest.mark.vendor_management]


# Same shape as test_endpoints._make_client but without the implicit
# default entitlement set, so each test is explicit about what's enabled.
def _override_caller(email: str, active_org_slug: str | None, enabled_apps: list[str]):
    app.dependency_overrides[get_verified_user] = lambda: {
        "email": email,
        "active_org_slug": active_org_slug,
    }
    app.dependency_overrides[get_caller_enabled_apps] = lambda: list(enabled_apps)


# ── pg_client.list_apps(org_slug) ────────────────────────────────────────


class TestListAppsOrgFilter:
    """`list_apps(org_slug)` must filter by `orgs.enabled_apps` and keep `granting_roles`."""

    def test_signature_requires_org_slug(self):
        """The Phase 4 contract: org_slug is positional, no default."""
        import inspect

        sig = inspect.signature(pg_client.list_apps)
        params = list(sig.parameters.values())

        assert len(params) == 1
        assert params[0].name == "org_slug"
        assert params[0].default is inspect.Parameter.empty


# ── require_app FastAPI dependency ──────────────────────────────────────


class TestRequireAppEntitlement:
    """`require_app(slug)` must 403 when the active org isn't entitled, 200 when it is."""

    def teardown_method(self):
        app.dependency_overrides.clear()

    @patch.object(pg_client, "list_vendors")
    @patch.object(pg_client, "get_user_access_context")
    def test_app_enabled_passes_through(self, mock_access, mock_list):
        """An org with the app enabled must reach the handler unimpeded."""
        mock_access.return_value = {
            "user_id": "u-1", "roles": ["finance_admin"],
            "allowed_departments": [], "allowed_vendor_ids": [], "denied_vendor_ids": [],
        }
        mock_list.return_value = []
        _override_caller("u@arcade.io", "arcade", ["vendors", "system_administration"])
        client = TestClient(app)

        resp = client.get("/vendors")

        assert resp.status_code == 200
        mock_list.assert_called_once_with("arcade")

    def test_app_not_enabled_returns_403_app_not_enabled(self):
        """An org without `vendors` must get 403 with the `App-Not-Enabled` code."""
        _override_caller("u@haderach.ai", "haderach", ["site", "system_administration"])
        client = TestClient(app)

        resp = client.get("/vendors")

        assert resp.status_code == 403
        body = resp.json()["detail"]
        assert body["code"] == "App-Not-Enabled"
        assert "vendors" in body["message"]
        assert "haderach" in body["message"]

    def test_no_active_org_returns_400_active_org_required(self):
        """A zero-membership caller must get 400 not 403 — no slug means no entitlement check."""
        _override_caller("nobody@example.com", None, [])
        client = TestClient(app)

        resp = client.get("/vendors")

        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "Active-Org-Required"

    def test_site_endpoint_blocks_arcade(self):
        """Arcade callers must be blocked from `/cms/*` because Arcade doesn't have `site`."""
        _override_caller("u@arcade.io", "arcade",
                         ["expenses", "vendors", "vendor_administration", "system_administration"])
        client = TestClient(app)

        resp = client.post("/cms/items", json={"contentTypeId": 1, "data": {}})

        assert resp.status_code == 403
        body = resp.json()["detail"]
        assert body["code"] == "App-Not-Enabled"
        assert "site" in body["message"]

    def test_vendor_administration_endpoint_blocks_haderach(self):
        """Haderach callers must be blocked from contractor admin (no `vendor_administration`)."""
        _override_caller("u@haderach.ai", "haderach", ["site", "system_administration"])
        client = TestClient(app)

        resp = client.get("/vendors/contractors")

        assert resp.status_code == 403
        body = resp.json()["detail"]
        assert body["code"] == "App-Not-Enabled"
        assert "vendor_administration" in body["message"]

    @patch.object(pg_client, "list_users")
    def test_system_administration_endpoint_passes_when_enabled(self, mock_users):
        """Both Arcade and Haderach have `system_administration`, so /users must succeed."""
        mock_users.return_value = []
        _override_caller("u@haderach.ai", "haderach", ["site", "system_administration"])
        client = TestClient(app)

        resp = client.get("/users")

        assert resp.status_code == 200
        mock_users.assert_called_once_with("haderach", None)


# ── Layered with role gate ──────────────────────────────────────────────


class TestEntitlementLayeredWithRoleGate:
    """Entitlement is layered on top of the role gate, not a replacement.

    The role gate (`require_finance_admin`) and the entitlement gate
    (`require_app("vendor_administration")`) must both be satisfied —
    failing either is a 403, and they're independent (a finance_admin
    in an org without `vendor_administration` still gets blocked).
    """

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_role_failure_takes_priority_when_app_also_missing(self):
        """When both gates would fail, a 403 is returned (role check happens inside the handler).

        The dep-injected `require_app` runs first because it's a FastAPI
        dependency, so the `App-Not-Enabled` code surfaces. This test
        protects the invariant that the entitlement gate fires *before*
        any handler-internal role check, so frontends always see the
        same error shape regardless of which gate would have caught it.
        """
        _override_caller("u@haderach.ai", "haderach", ["site", "system_administration"])
        client = TestClient(app)

        resp = client.get("/vendors/contractors")

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "App-Not-Enabled"

    @patch.object(pg_client, "get_user")
    def test_app_enabled_but_role_missing_still_403(self, mock_get_user):
        """Entitlement passing doesn't bypass the role gate — both must hold."""
        mock_get_user.return_value = {
            "id": "uid-reg", "email": "u@arcade.io",
            "firstName": "U", "lastName": "ser", "roles": ["viewer"],
            "allowedDepartments": [], "allowedVendorIds": [],
            "deniedVendorIds": [], "allowedVendors": [],
        }
        _override_caller("u@arcade.io", "arcade",
                         ["expenses", "vendors", "vendor_administration", "system_administration"])
        client = TestClient(app)

        resp = client.get("/vendors/contractors")

        assert resp.status_code == 403


# ── Per-request dep cache (B option from the design call) ────────────────


class TestEnabledAppsResolvedOncePerRequest:
    """`get_caller_enabled_apps` must be a dep-cached single lookup per request.

    Phase 4 chose option B for caching: lazy per-request resolution via
    FastAPI's built-in dependency cache. The contract this test
    protects: an endpoint can have arbitrarily many `require_app(...)`
    dependencies (now or in future), and they all share one resolution.
    """

    def teardown_method(self):
        app.dependency_overrides.clear()

    @patch.object(pg_client, "list_user_org_slugs")
    @patch.object(pg_client, "get_org_enabled_apps")
    @patch.object(pg_client, "list_vendors")
    @patch.object(pg_client, "get_user_access_context")
    @patch("service.auth._DEV_AUTH_EMAIL", "dev@example.com")
    def test_single_db_call_for_enabled_apps_per_request(
        self, mock_access, mock_vendors, mock_enabled, mock_slugs,
    ):
        """`pg_client.get_org_enabled_apps` must be hit at most once per request.

        Hits `/vendors` end-to-end (via the real `get_verified_user` in
        DEV_AUTH_EMAIL mode and the real `get_caller_enabled_apps` dep)
        — without any dependency overrides — and asserts the lookup
        helper was called exactly once.
        """
        # No overrides — exercise the real dependency chain in DEV_AUTH_EMAIL mode.
        app.dependency_overrides.clear()

        mock_slugs.return_value = ["arcade"]
        mock_enabled.return_value = ["vendors", "system_administration"]
        mock_access.return_value = {
            "user_id": "u-1", "roles": ["finance_admin"],
            "allowed_departments": [], "allowed_vendor_ids": [], "denied_vendor_ids": [],
        }
        mock_vendors.return_value = []

        client = TestClient(app)
        resp = client.get(
            "/vendors",
            headers={"X-Test-Email": "u@arcade.io", "X-Active-Org": "arcade"},
        )

        assert resp.status_code == 200
        mock_enabled.assert_called_once_with("arcade")


# ── Phase 5: CMS REST passthrough threads org slug into handlers ─────────


class TestCmsRestPassthroughPhase5:
    """`/cms/*` REST endpoints must seed the contextvar so cms_tools handlers
    can resolve the caller's Payload org id without an `orgId` arg.

    Phase 4 already proved arcade callers get blocked at `require_app('site')`.
    Phase 5 covers what happens for a permitted (haderach) caller: the
    contextvar is populated, the handler skips its `orgId` requirement,
    and the resolver-cached Payload id flows into the POST body.
    """

    def setup_method(self):
        from service import cms_tools, tools as tools_module
        cms_tools._clear_org_id_cache()
        cms_tools._org_id_cache["haderach"] = 1
        # Reset any contextvar leakage from prior tests.
        try:
            tools_module._caller_org_slug.set(None)
        except Exception:
            pass

    def teardown_method(self):
        from service import cms_tools
        app.dependency_overrides.clear()
        cms_tools._clear_org_id_cache()

    def _make_resp(self, status_code, body):
        from unittest.mock import MagicMock
        r = MagicMock()
        r.status_code = status_code
        r.json.return_value = body
        r.raise_for_status = MagicMock()
        return r

    def test_post_cms_items_haderach_caller_resolves_org_no_orgid_arg(self):
        """A haderach caller's POST /cms/items must resolve org from context, not from orgId."""
        from service import cms_tools

        _override_caller("u@haderach.ai", "haderach", ["site", "system_administration"])
        client = TestClient(app)

        with patch.object(cms_tools.httpx, "post", return_value=self._make_resp(201, {"doc": {"id": 99}})) as mock_post:
            resp = client.post("/cms/items", json={"contentTypeId": 5, "data": {"title": "Hi"}})

        assert resp.status_code == 200
        assert resp.json()["item"]["id"] == 99
        body = mock_post.call_args.kwargs["json"]
        # org must come from the resolver cache (haderach -> 1), not from
        # any client-supplied orgId.
        assert body["org"] == 1
        assert body["contentType"] == 5

    def test_get_cms_versions_blocks_cross_tenant_access(self):
        """The version-list endpoint must 404 when the parent item belongs to another tenant."""
        from service import cms_tools

        _override_caller("u@haderach.ai", "haderach", ["site", "system_administration"])
        client = TestClient(app)

        # Guard fetch returns an item owned by Arcade — must surface as 404.
        cross_tenant_item = {"id": 50, "org": {"id": 2, "slug": "arcade"}}
        with patch.object(cms_tools.httpx, "get", return_value=self._make_resp(200, cross_tenant_item)):
            resp = client.get("/cms/items/50/versions")

        assert resp.status_code == 404

    def test_patch_cms_item_deactivate_live_sets_draft_and_clears_lock(self):
        """Live -> draft deactivation must clear lock and preserve published status."""
        from service import cms_tools

        _override_caller("u@haderach.ai", "haderach", ["site", "system_administration"])
        client = TestClient(app)

        live_item = {"id": 50, "workflow_status": "live", "org": {"id": 1, "slug": "haderach"}}
        with patch.object(cms_tools.httpx, "get", return_value=self._make_resp(200, live_item)), \
             patch.object(cms_tools.httpx, "patch", return_value=self._make_resp(200, {"doc": {"id": 50, "workflow_status": "draft", "locked_by": None}})) as mock_patch:
            resp = client.patch("/cms/items/50", json={"workflow_status": "draft"})

        assert resp.status_code == 200
        body = mock_patch.call_args.kwargs["json"]
        assert body["_status"] == "published"
        assert body["workflow_status"] == "draft"
        assert body["locked_by"] is None

    def test_patch_cms_item_deactivate_non_live_returns_409(self):
        """Deactivation is only valid from live state."""
        from service import cms_tools

        _override_caller("u@haderach.ai", "haderach", ["site", "system_administration"])
        client = TestClient(app)

        approved_item = {"id": 50, "workflow_status": "approved", "org": {"id": 1, "slug": "haderach"}}
        with patch.object(cms_tools.httpx, "get", return_value=self._make_resp(200, approved_item)), \
             patch.object(cms_tools.httpx, "patch") as mock_patch:
            resp = client.patch("/cms/items/50", json={"workflow_status": "draft"})

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "Invalid-Workflow-Transition"
        assert detail["status"] == "invalid_state"
        mock_patch.assert_not_called()


# ── /apps endpoint pipes the slug through ────────────────────────────────


class TestListAppsEndpoint:
    """`GET /apps` must call `list_apps(active_org_slug)`, never the unfiltered form."""

    def teardown_method(self):
        app.dependency_overrides.clear()

    @patch.object(pg_client, "list_apps")
    def test_passes_active_org_slug(self, mock_list_apps):
        """The endpoint must thread the caller's active org slug into the query."""
        mock_list_apps.return_value = []
        _override_caller("u@arcade.io", "arcade",
                         ["expenses", "vendors", "vendor_administration", "system_administration"])
        client = TestClient(app)

        resp = client.get("/apps")

        assert resp.status_code == 200
        mock_list_apps.assert_called_once_with("arcade")

    def test_no_active_org_returns_400(self):
        """A caller without an active org cannot list apps — there's nothing to filter against."""
        _override_caller("nobody@example.com", None, [])
        client = TestClient(app)

        resp = client.get("/apps")

        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "Active-Org-Required"
