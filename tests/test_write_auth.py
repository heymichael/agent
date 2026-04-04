"""Tests for vendor write access control.

Covers: _check_write_auth, execute_modify_vendor ACL gate,
and execute_process_vendor_csv ACL gate.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from service import pg_client
from service.tools import (
    _check_write_auth,
    execute_modify_vendor,
    execute_process_vendor_csv,
    process_csv_upload,
    VENDOR_CSV_PROFILE,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

VENDOR_ALPHA = {"id": "v-alpha", "name": "Alpha Corp"}
VENDOR_BETA = {"id": "v-beta", "name": "Beta Inc"}

FINANCE_ADMIN_CTX = {
    "user_id": "uid-admin",
    "roles": ["finance_admin"],
    "allowed_departments": [],
    "allowed_vendor_ids": [],
    "denied_vendor_ids": [],
}

SCOPED_USER_CTX = {
    "user_id": "uid-scoped",
    "roles": [],
    "allowed_departments": ["Engineering"],
    "allowed_vendor_ids": [],
    "denied_vendor_ids": [],
}


def _mock_access(user_ctx, effective_ids):
    """Return patches for get_user_access_context and resolve_effective_vendor_ids."""
    return (
        patch.object(pg_client, "get_user_access_context", return_value=user_ctx),
        patch.object(pg_client, "resolve_effective_vendor_ids", return_value=effective_ids),
    )


# ── _check_write_auth ────────────────────────────────────────────────────

class TestCheckWriteAuth:

    def test_finance_admin_allowed(self):
        p1, p2 = _mock_access(FINANCE_ADMIN_CTX, [])
        with p1, p2:
            result = _check_write_auth("admin@co.com", "v-alpha", "Alpha Corp")
        assert result is None

    def test_scoped_user_allowed_vendor(self):
        p1, p2 = _mock_access(SCOPED_USER_CTX, ["v-alpha"])
        with p1, p2:
            result = _check_write_auth("user@co.com", "v-alpha", "Alpha Corp")
        assert result is None

    def test_scoped_user_denied_vendor(self):
        p1, p2 = _mock_access(SCOPED_USER_CTX, ["v-alpha"])
        with p1, p2:
            result = _check_write_auth("user@co.com", "v-beta", "Beta Inc")
        assert result is not None
        data = json.loads(result)
        assert data["status"] == "not_authorized"
        assert "Beta Inc" in data["message"]

    def test_no_email_denied(self):
        result = _check_write_auth("", "v-alpha")
        assert result is not None
        data = json.loads(result)
        assert data["status"] == "not_authorized"

    def test_unknown_user_denied(self):
        with patch.object(pg_client, "get_user_access_context", return_value=None):
            result = _check_write_auth("nobody@co.com", "v-alpha")
        assert result is not None
        data = json.loads(result)
        assert data["status"] == "not_authorized"

    def test_falls_back_to_vendor_id_in_message(self):
        p1, p2 = _mock_access(SCOPED_USER_CTX, [])
        with p1, p2:
            result = _check_write_auth("user@co.com", "v-beta")
        data = json.loads(result)
        assert "v-beta" in data["message"]


# ── execute_modify_vendor ACL gate ───────────────────────────────────────

class TestModifyVendorAuth:

    def _mock_resolve(self, vendor):
        return pg_client.VendorMatch(vendor=vendor, match="exact")

    def test_authorized_user_gets_open_edit(self):
        p1, p2 = _mock_access(SCOPED_USER_CTX, ["v-alpha"])
        with (
            patch.object(pg_client, "resolve_vendor_by_identifier", return_value=self._mock_resolve(VENDOR_ALPHA)),
            p1, p2,
        ):
            result = json.loads(execute_modify_vendor({"identifier": "Alpha Corp"}, caller_email="user@co.com"))
        assert result.get("action") == "open_edit"

    def test_unauthorized_user_gets_not_authorized(self):
        p1, p2 = _mock_access(SCOPED_USER_CTX, ["v-alpha"])
        with (
            patch.object(pg_client, "resolve_vendor_by_identifier", return_value=self._mock_resolve(VENDOR_BETA)),
            p1, p2,
        ):
            result = json.loads(execute_modify_vendor({"identifier": "Beta Inc"}, caller_email="user@co.com"))
        assert result["status"] == "not_authorized"
        assert "Beta Inc" in result["message"]

    def test_finance_admin_bypasses_check(self):
        p1, p2 = _mock_access(FINANCE_ADMIN_CTX, [])
        with (
            patch.object(pg_client, "resolve_vendor_by_identifier", return_value=self._mock_resolve(VENDOR_BETA)),
            p1, p2,
        ):
            result = json.loads(execute_modify_vendor({"identifier": "Beta Inc"}, caller_email="admin@co.com"))
        assert result.get("action") == "open_edit"

    def test_not_found_before_auth_check(self):
        """Auth check should not run if the vendor doesn't exist."""
        with patch.object(pg_client, "resolve_vendor_by_identifier", return_value=None):
            result = json.loads(execute_modify_vendor({"identifier": "Ghost"}, caller_email="user@co.com"))
        assert result["ok"] is False
        assert "not found" in result["error"]


# ── execute_process_vendor_csv ACL gate ──────────────────────────────────

class TestProcessVendorCsvAuth:

    def _mock_csv_result(self, vendor_ids_and_names):
        """Build a mock process_csv_upload result with updates."""
        updates = [
            {VENDOR_CSV_PROFILE.pk_key: vid, "vendor_name": name, "changes": {"name": "X"}}
            for vid, name in vendor_ids_and_names
        ]
        return {
            "ok": True,
            "action": "confirm_csv_batch",
            "updates": updates,
            "summary": {"vendor_count": len(updates)},
        }

    def _patches(self, csv_result, user_ctx, effective_ids):
        return (
            patch("service.app.get_request_attachments", return_value=[{"filename": "test.csv", "content": "id\nv1"}]),
            patch("service.tools.process_csv_upload", return_value=csv_result),
            *_mock_access(user_ctx, effective_ids),
        )

    def test_authorized_user_gets_confirm_batch(self):
        csv_result = self._mock_csv_result([("v-alpha", "Alpha Corp")])
        p_att, p_csv, p1, p2 = self._patches(csv_result, SCOPED_USER_CTX, ["v-alpha"])
        with p_att, p_csv, p1, p2:
            result = json.loads(execute_process_vendor_csv({}, caller_email="user@co.com"))
        assert result["ok"] is True
        assert result["action"] == "confirm_csv_batch"

    def test_unauthorized_vendors_rejected(self):
        csv_result = self._mock_csv_result([("v-alpha", "Alpha Corp"), ("v-beta", "Beta Inc")])
        p_att, p_csv, p1, p2 = self._patches(csv_result, SCOPED_USER_CTX, ["v-alpha"])
        with p_att, p_csv, p1, p2:
            result = json.loads(execute_process_vendor_csv({}, caller_email="user@co.com"))
        assert result["status"] == "not_authorized"
        assert "Beta Inc" in result["message"]
        assert result["denied_vendors"] == ["Beta Inc"]

    def test_finance_admin_bypasses_check(self):
        csv_result = self._mock_csv_result([("v-alpha", "Alpha Corp"), ("v-beta", "Beta Inc")])
        p_att, p_csv, p1, p2 = self._patches(csv_result, FINANCE_ADMIN_CTX, [])
        with p_att, p_csv, p1, p2:
            result = json.loads(execute_process_vendor_csv({}, caller_email="admin@co.com"))
        assert result["ok"] is True

    def test_no_access_user_fully_denied(self):
        csv_result = self._mock_csv_result([("v-alpha", "Alpha Corp")])
        p_att, p_csv, p1, p2 = self._patches(csv_result, None, [])
        with p_att, p_csv, p1, p2:
            result = json.loads(execute_process_vendor_csv({}, caller_email="nobody@co.com"))
        assert result["status"] == "not_authorized"

    def test_validation_errors_returned_before_auth(self):
        """If CSV validation fails, the error should return without auth check."""
        error_result = {"ok": False, "stage": "column_check", "errors": [{"column": "bad"}]}
        p_att, p_csv, p1, p2 = self._patches(error_result, SCOPED_USER_CTX, [])
        with p_att, p_csv, p1, p2:
            result = json.loads(execute_process_vendor_csv({}, caller_email="user@co.com"))
        assert result["ok"] is False
        assert result["stage"] == "column_check"


# ── Contractor filtering via _build_caller_context ───────────────────────

class TestContractorFiltering:
    """Verify that resolve_effective_vendor_ids receives user_id for contractor filtering."""

    def test_user_id_passed_to_resolve(self):
        """_build_caller_context passes user_id from access context to resolve_effective_vendor_ids."""
        from service.tools import _build_caller_context

        with (
            patch.object(pg_client, "get_user_access_context", return_value=SCOPED_USER_CTX),
            patch.object(pg_client, "resolve_effective_vendor_ids", return_value=["v-alpha"]) as mock_resolve,
        ):
            ctx = _build_caller_context("user@co.com")

        mock_resolve.assert_called_once_with(
            ["Engineering"], [], [],
            user_id="uid-scoped",
        )
        assert ctx == {"allowed_vendor_ids": ["v-alpha"], "is_finance_admin": False}

    def test_finance_admin_skips_resolve(self):
        """finance_admin bypasses resolve_effective_vendor_ids entirely."""
        from service.tools import _build_caller_context

        with (
            patch.object(pg_client, "get_user_access_context", return_value=FINANCE_ADMIN_CTX),
            patch.object(pg_client, "resolve_effective_vendor_ids") as mock_resolve,
        ):
            ctx = _build_caller_context("admin@co.com")

        mock_resolve.assert_not_called()
        assert ctx == {"is_finance_admin": True}

    def test_contractor_excluded_without_grant(self):
        """A contractor vendor should be excluded when no user_contractor_access row exists."""
        p1, p2 = _mock_access(SCOPED_USER_CTX, ["v-alpha"])
        with p1, p2:
            result = _check_write_auth("user@co.com", "v-contractor", "ContractorCo")
        assert result is not None
        data = json.loads(result)
        assert data["status"] == "not_authorized"

    def test_contractor_included_with_grant(self):
        """A contractor vendor should be accessible when resolve returns it (grant exists)."""
        p1, p2 = _mock_access(SCOPED_USER_CTX, ["v-alpha", "v-contractor"])
        with p1, p2:
            result = _check_write_auth("user@co.com", "v-contractor", "ContractorCo")
        assert result is None
