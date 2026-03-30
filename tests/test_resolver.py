"""Unit tests for mcp_server.resolver.

Pure helper functions are tested directly. Functions that touch Postgres
(resolve_vendor, resolve_filter, validate_filters) use mocked pools.
"""

from unittest.mock import patch, MagicMock
from contextlib import contextmanager

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


# ── Mock helpers ─────────────────────────────────────────────────────────

class _MockCursor:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _MockConn:
    def __init__(self, query_map=None):
        self._query_map = query_map or {}

    def execute(self, sql, params=None):
        for pattern, result in self._query_map.items():
            if pattern in sql:
                if isinstance(result, list):
                    return _MockCursor(rows=result)
                else:
                    return _MockCursor(row=result)
        return _MockCursor()


class _MockPool:
    def __init__(self, query_map=None):
        self._conn = _MockConn(query_map)

    @contextmanager
    def connection(self):
        yield self._conn


def _pool_with_vendors(vendors, id_match=None):
    """Build a mock pool for resolver tests.

    vendors: list of dicts with id, name, aliases
    id_match: dict to return for UUID/source_system_id lookup (or None)
    """
    return _MockPool({
        "WHERE id::text": id_match,
        "FROM vendors ORDER BY": vendors,
        "DISTINCT": [{"val": v.get("department")} for v in vendors if v.get("department")],
    })


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

    @patch("mcp_server.resolver.get_pool")
    def test_exact_dynamic_match(self, mock_get_pool):
        mock_get_pool.return_value = _MockPool({
            "DISTINCT": [{"val": "Engineering"}, {"val": "Marketing"}],
        })
        result = resolve_filter("department", "Engineering")
        assert result["status"] == "ok"
        assert result["value"] == "Engineering"

    @patch("mcp_server.resolver.get_pool")
    def test_case_insensitive_dynamic(self, mock_get_pool):
        mock_get_pool.return_value = _MockPool({
            "DISTINCT": [{"val": "Marketing"}],
        })
        result = resolve_filter("department", "marketing")
        assert result["status"] == "ok"
        assert result["value"] == "Marketing"

    @patch("mcp_server.resolver.get_pool")
    def test_invalid_dynamic_value(self, mock_get_pool):
        mock_get_pool.return_value = _MockPool({
            "DISTINCT": [{"val": "Engineering"}, {"val": "Marketing"}],
        })
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

    @patch("mcp_server.resolver.get_pool")
    def test_empty_identifier(self, mock_get_pool):
        result = resolve_vendor("")
        assert result["status"] == "not_found"

    @patch("mcp_server.resolver.get_pool")
    def test_whitespace_only(self, mock_get_pool):
        result = resolve_vendor("   ")
        assert result["status"] == "not_found"

    @patch("mcp_server.resolver.get_pool")
    def test_exact_id_match(self, mock_get_pool):
        mock_get_pool.return_value = _pool_with_vendors(
            [{"id": "v_123", "name": "Acme Corp", "aliases": None}],
            id_match={"id": "v_123", "name": "Acme Corp"},
        )
        result = resolve_vendor("v_123")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_123"
        assert result["vendor_name"] == "Acme Corp"

    @patch("mcp_server.resolver.get_pool")
    def test_exact_name_match(self, mock_get_pool):
        mock_get_pool.return_value = _pool_with_vendors([
            {"id": "v_1", "name": "Acme Corp", "aliases": None},
            {"id": "v_2", "name": "Beta Inc", "aliases": None},
        ])
        result = resolve_vendor("Acme Corp")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"

    @patch("mcp_server.resolver.get_pool")
    def test_case_insensitive_name(self, mock_get_pool):
        mock_get_pool.return_value = _pool_with_vendors([
            {"id": "v_1", "name": "Acme Corp", "aliases": None},
        ])
        result = resolve_vendor("acme corp")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"

    @patch("mcp_server.resolver.get_pool")
    def test_alias_match(self, mock_get_pool):
        mock_get_pool.return_value = _pool_with_vendors([
            {"id": "v_1", "name": "Amazon Web Services", "aliases": ["AWS", "Amazon"]},
        ])
        result = resolve_vendor("AWS")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"
        assert result["vendor_name"] == "Amazon Web Services"

    @patch("mcp_server.resolver.get_pool")
    def test_normalised_match(self, mock_get_pool):
        mock_get_pool.return_value = _pool_with_vendors([
            {"id": "v_1", "name": "O'Brien & Associates, LLC", "aliases": None},
        ])
        result = resolve_vendor("obrien associates llc")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"

    @patch("mcp_server.resolver.get_pool")
    def test_token_match(self, mock_get_pool):
        mock_get_pool.return_value = _pool_with_vendors([
            {"id": "v_1", "name": "Jennifer Chen-Manwell", "aliases": None},
            {"id": "v_2", "name": "Robert Johnson", "aliases": None},
        ])
        result = resolve_vendor("jennifer")
        assert result["status"] == "ok"
        assert result["vendor_id"] == "v_1"

    @patch("mcp_server.resolver.get_pool")
    def test_ambiguous_token_match(self, mock_get_pool):
        mock_get_pool.return_value = _pool_with_vendors([
            {"id": "v_1", "name": "Acme Corporation LLC", "aliases": None},
            {"id": "v_2", "name": "Acme Logistics Inc", "aliases": None},
        ])
        result = resolve_vendor("Acme")
        assert result["status"] == "ambiguous"
        assert len(result["candidates"]) == 2

    @patch("mcp_server.resolver.get_pool")
    def test_not_found(self, mock_get_pool):
        mock_get_pool.return_value = _pool_with_vendors([
            {"id": "v_1", "name": "Acme Corp", "aliases": None},
        ])
        result = resolve_vendor("Nonexistent Vendor XYZ")
        assert result["status"] == "not_found"

    @patch("mcp_server.resolver.get_pool")
    def test_ambiguous_caps_to_10(self, mock_get_pool):
        vendors = [{"id": f"v_{i}", "name": f"Acme Division {i}", "aliases": None} for i in range(15)]
        mock_get_pool.return_value = _pool_with_vendors(vendors)
        result = resolve_vendor("Acme")
        assert result["status"] == "ambiguous"
        assert len(result["candidates"]) <= 10
