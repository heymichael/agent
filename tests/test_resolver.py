"""Unit tests for mcp_server.resolver (filter validation).

Vendor resolution tests live in test_vendor_resolver.py.
"""

from unittest.mock import patch
from contextlib import contextmanager

import pytest

pytestmark = [pytest.mark.expense_analytics, pytest.mark.vendor_management]

from mcp_server.resolver import (
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
