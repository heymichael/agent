"""Unit tests for mcp_server.resolver.

Pure helper functions are tested directly. Functions that touch Firestore
(resolve_vendor, resolve_filter, validate_filters) use mocked DB clients.
"""

from unittest.mock import patch, MagicMock

import pytest

from mcp_server.resolver import (
    _normalise,
    _token_match,
    resolve_vendor,
    resolve_filter,
    validate_filters,
    ENUM_FIELDS,
    RESOLVE_FIELDS,
)


# ── _normalise ───────────────────────────────────────────────────────────

class TestNormalise:
    def test_lowercase(self):
        assert _normalise("ACME Corp") == "acme corp"

    def test_strips_punctuation(self):
        assert _normalise("Acme, Inc.") == "acme inc"

    def test_collapses_whitespace(self):
        assert _normalise("  foo   bar  ") == "foo bar"

    def test_empty_string(self):
        assert _normalise("") == ""

    def test_special_chars(self):
        assert _normalise("O'Brien & Associates, LLC") == "obrien associates llc"


# ── _token_match ─────────────────────────────────────────────────────────

class TestTokenMatch:
    def test_all_tokens_present(self):
        assert _token_match(["acme", "corp"], "acme corporation corp") is True

    def test_missing_token(self):
        assert _token_match(["acme", "logistics"], "acme corporation") is False

    def test_single_token(self):
        assert _token_match(["jen"], "jennifer chen-manwell") is True

    def test_empty_tokens(self):
        assert _token_match([], "anything") is True


# ── resolve_filter: enum fields ──────────────────────────────────────────

class TestResolveFilterEnum:
    def test_exact_match(self):
        result = resolve_filter("paymentMethod", "ACH")
        assert result["status"] == "ok"
        assert result["value"] == "ACH"

    def test_case_insensitive(self):
        result = resolve_filter("paymentMethod", "ach")
        assert result["status"] == "ok"
        assert result["value"] == "ACH"

    def test_invalid_value(self):
        result = resolve_filter("paymentMethod", "Bitcoin")
        assert result["status"] == "invalid_filter"
        assert "Bitcoin" == result["provided"]
        assert "ACH" in result["valid_values"]

    def test_boolean_field_true(self):
        result = resolve_filter("track1099", True)
        assert result["status"] == "ok"
        assert result["value"] is True

    def test_boolean_field_false(self):
        result = resolve_filter("track1099", False)
        assert result["status"] == "ok"
        assert result["value"] is False

    def test_unknown_field(self):
        result = resolve_filter("nonexistent", "value")
        assert result["status"] == "invalid_filter"
        assert "Unknown filter field" in result.get("message", "")

    def test_all_enum_fields_have_values(self):
        for field, values in ENUM_FIELDS.items():
            assert len(values) >= 2, f"{field} should have at least 2 valid values"
            for v in values:
                result = resolve_filter(field, v)
                assert result["status"] == "ok", f"{field}={v} should be valid"


# ── resolve_filter: dynamic fields ───────────────────────────────────────

class TestResolveFilterDynamic:
    def _mock_vendor_docs(self, vendors):
        """Create a mock Firestore client returning given vendor dicts."""
        mock_db = MagicMock()
        mock_docs = []
        for v in vendors:
            doc = MagicMock()
            doc.to_dict.return_value = v
            mock_docs.append(doc)
        mock_db.collection.return_value.stream.return_value = mock_docs
        return mock_db

    @patch("mcp_server.resolver.get_db")
    def test_exact_dynamic_match(self, mock_get_db):
        mock_get_db.return_value = self._mock_vendor_docs([
            {"department": "Engineering"},
            {"department": "Marketing"},
            {"department": "Engineering"},
        ])
        result = resolve_filter("department", "Engineering")
        assert result["status"] == "ok"
        assert result["value"] == "Engineering"

    @patch("mcp_server.resolver.get_db")
    def test_case_insensitive_dynamic(self, mock_get_db):
        mock_get_db.return_value = self._mock_vendor_docs([
            {"department": "Marketing"},
        ])
        result = resolve_filter("department", "marketing")
        assert result["status"] == "ok"
        assert result["value"] == "Marketing"

    @patch("mcp_server.resolver.get_db")
    def test_invalid_dynamic_value(self, mock_get_db):
        mock_get_db.return_value = self._mock_vendor_docs([
            {"department": "Engineering"},
            {"department": "Marketing"},
        ])
        result = resolve_filter("department", "Sales")
        assert result["status"] == "invalid_filter"
        assert "Sales" == result["provided"]
        assert "Engineering" in result["valid_values"]
        assert "Marketing" in result["valid_values"]


# ── validate_filters ─────────────────────────────────────────────────────

class TestValidateFilters:
    def test_none_filters(self):
        assert validate_filters(None) is None

    def test_empty_filters(self):
        assert validate_filters({}) is None

    def test_valid_enum_filter(self):
        filters = {"paymentMethod": "ACH", "track1099": True}
        result = validate_filters(filters)
        assert result is None

    def test_invalid_stops_on_first(self):
        filters = {"paymentMethod": "Bitcoin", "track1099": True}
        result = validate_filters(filters)
        assert result is not None
        assert result["status"] == "invalid_filter"
        assert result["field"] == "paymentMethod"

    def test_canonicalises_values(self):
        filters = {"paymentMethod": "ach"}
        validate_filters(filters)
        assert filters["paymentMethod"] == "ACH"


# ── resolve_vendor ───────────────────────────────────────────────────────

class TestResolveVendor:
    def _mock_db_with_vendors(self, vendors, doc_by_id=None):
        """Build a mock Firestore client.

        vendors: list of dicts (each must have 'id' and 'name')
        doc_by_id: dict mapping doc_id -> dict for exact ID lookups
        """
        mock_db = MagicMock()

        def get_doc(doc_id):
            snap = MagicMock()
            if doc_by_id and doc_id in doc_by_id:
                snap.exists = True
                snap.id = doc_id
                snap.to_dict.return_value = doc_by_id[doc_id]
            else:
                snap.exists = False
            return snap

        mock_db.collection.return_value.document.return_value.get = MagicMock(
            side_effect=lambda: get_doc(
                mock_db.collection.return_value.document.call_args[0][0]
            )
        )

        mock_docs = []
        for v in vendors:
            doc = MagicMock()
            doc.id = v["id"]
            doc.to_dict.return_value = {k: v2 for k, v2 in v.items() if k != "id"}
            mock_docs.append(doc)
        mock_db.collection.return_value.stream.return_value = mock_docs

        # Handle document().get() calls with specific IDs
        def doc_factory(doc_id):
            mock_doc_ref = MagicMock()
            snap = MagicMock()
            if doc_by_id and doc_id in doc_by_id:
                snap.exists = True
                snap.id = doc_id
                snap.to_dict.return_value = doc_by_id[doc_id]
            else:
                snap.exists = False
                snap.id = doc_id
            mock_doc_ref.get.return_value = snap
            return mock_doc_ref

        mock_db.collection.return_value.document.side_effect = doc_factory
        return mock_db

    @patch("mcp_server.resolver.get_db")
    def test_empty_identifier(self, mock_get_db):
        result = resolve_vendor("")
        assert result["status"] == "not_found"

    @patch("mcp_server.resolver.get_db")
    def test_whitespace_only(self, mock_get_db):
        result = resolve_vendor("   ")
        assert result["status"] == "not_found"

    @patch("mcp_server.resolver.get_db")
    def test_exact_id_match(self, mock_get_db):
        mock_get_db.return_value = self._mock_db_with_vendors(
            [{"id": "v_123", "name": "Acme Corp"}],
            doc_by_id={"v_123": {"name": "Acme Corp"}},
        )
        result = resolve_vendor("v_123")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_123"
        assert result["vendor_name"] == "Acme Corp"

    @patch("mcp_server.resolver.get_db")
    def test_exact_name_match(self, mock_get_db):
        mock_get_db.return_value = self._mock_db_with_vendors(
            [
                {"id": "v_1", "name": "Acme Corp"},
                {"id": "v_2", "name": "Beta Inc"},
            ],
        )
        result = resolve_vendor("Acme Corp")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"

    @patch("mcp_server.resolver.get_db")
    def test_case_insensitive_name(self, mock_get_db):
        mock_get_db.return_value = self._mock_db_with_vendors(
            [{"id": "v_1", "name": "Acme Corp"}],
        )
        result = resolve_vendor("acme corp")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"

    @patch("mcp_server.resolver.get_db")
    def test_alias_match(self, mock_get_db):
        mock_get_db.return_value = self._mock_db_with_vendors(
            [{"id": "v_1", "name": "Amazon Web Services", "aliases": ["AWS", "Amazon"]}],
        )
        result = resolve_vendor("AWS")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"
        assert result["vendor_name"] == "Amazon Web Services"

    @patch("mcp_server.resolver.get_db")
    def test_alias_case_insensitive(self, mock_get_db):
        mock_get_db.return_value = self._mock_db_with_vendors(
            [{"id": "v_1", "name": "Amazon Web Services", "aliases": ["AWS"]}],
        )
        result = resolve_vendor("aws")
        assert result["status"] == "ok"

    @patch("mcp_server.resolver.get_db")
    def test_normalised_match(self, mock_get_db):
        mock_get_db.return_value = self._mock_db_with_vendors(
            [{"id": "v_1", "name": "O'Brien & Associates, LLC"}],
        )
        result = resolve_vendor("obrien associates llc")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"

    @patch("mcp_server.resolver.get_db")
    def test_token_match(self, mock_get_db):
        mock_get_db.return_value = self._mock_db_with_vendors(
            [
                {"id": "v_1", "name": "Jennifer Chen-Manwell"},
                {"id": "v_2", "name": "Robert Johnson"},
            ],
        )
        result = resolve_vendor("jennifer")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"

    @patch("mcp_server.resolver.get_db")
    def test_ambiguous_token_match(self, mock_get_db):
        mock_get_db.return_value = self._mock_db_with_vendors(
            [
                {"id": "v_1", "name": "Acme Corporation LLC"},
                {"id": "v_2", "name": "Acme Logistics Inc"},
            ],
        )
        result = resolve_vendor("Acme")
        assert result["status"] == "ambiguous"
        assert len(result["candidates"]) == 2
        ids = {c["vendor_id"] for c in result["candidates"]}
        assert ids == {"v_1", "v_2"}

    @patch("mcp_server.resolver.get_db")
    def test_not_found(self, mock_get_db):
        mock_get_db.return_value = self._mock_db_with_vendors(
            [{"id": "v_1", "name": "Acme Corp"}],
        )
        result = resolve_vendor("Nonexistent Vendor XYZ")
        assert result["status"] == "not_found"

    @patch("mcp_server.resolver.get_db")
    def test_no_aliases_field(self, mock_get_db):
        """Vendors without aliases array should not crash alias matching."""
        mock_get_db.return_value = self._mock_db_with_vendors(
            [{"id": "v_1", "name": "Simple Vendor"}],
        )
        result = resolve_vendor("Simple Vendor")
        assert result["status"] == "ok"

    @patch("mcp_server.resolver.get_db")
    def test_ambiguous_caps_to_10(self, mock_get_db):
        """Token matches should be capped at 10 candidates."""
        vendors = [{"id": f"v_{i}", "name": f"Acme Division {i}"} for i in range(15)]
        mock_get_db.return_value = self._mock_db_with_vendors(vendors)
        result = resolve_vendor("Acme")
        assert result["status"] == "ambiguous"
        assert len(result["candidates"]) <= 10
