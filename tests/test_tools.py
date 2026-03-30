"""Integration tests for mcp_server.tools handlers.

All Postgres access is mocked via pg_client.get_pool. Tests verify that
handlers correctly orchestrate resolution, period parsing, filter
validation, and SQL-backed aggregation, returning the expected response
contract.
"""

from unittest.mock import patch, MagicMock
from contextlib import contextmanager
from decimal import Decimal

import pytest

from mcp_server.tools import (
    handle_vendor_lookup,
    handle_vendor_count,
    handle_spend_total,
    handle_spend_by_vendor,
    handle_spend_by_dimension,
    handle_top_vendors,
)


# ── Mock pool infrastructure ─────────────────────────────────────────────

SAMPLE_VENDORS = [
    {"id": "v_acme", "name": "Acme Corp", "aliases": ["Acme"],
     "payment_method": "ACH", "account_type": "Business", "track_1099": True,
     "source_system": "billcom", "source_system_id": "bc_acme"},
    {"id": "v_beta", "name": "Beta Inc", "aliases": None,
     "payment_method": "Check", "account_type": "Individual", "track_1099": True,
     "source_system": "billcom", "source_system_id": "bc_beta"},
    {"id": "v_gamma", "name": "Gamma LLC", "aliases": None,
     "payment_method": "ACH", "account_type": "Business", "track_1099": False,
     "source_system": "billcom", "source_system_id": "bc_gamma"},
]

SAMPLE_VENDOR_API = {
    "id": "v_acme", "name": "Acme Corp", "sourceSystem": "billcom",
    "sourceSystemId": "bc_acme", "department": "Engineering",
    "paymentMethod": "ACH", "accountType": "Business", "track1099": True,
    "aliases": ["Acme"],
}


class MockCursor:
    """Configurable mock cursor that returns pre-set results."""

    def __init__(self, results=None, single=None):
        self._results = results or []
        self._single = single

    def fetchall(self):
        return self._results

    def fetchone(self):
        return self._single


class MockConnection:
    """Mock connection whose execute() returns results based on SQL patterns."""

    def __init__(self, query_results=None):
        self._query_results = query_results or {}

    def execute(self, sql, params=None):
        for pattern, result in self._query_results.items():
            if pattern in sql:
                if isinstance(result, list):
                    return MockCursor(results=result)
                else:
                    return MockCursor(single=result)
        return MockCursor()

    def cursor(self):
        return self


class MockPool:
    """Mock connection pool."""

    def __init__(self, query_results=None):
        self._conn = MockConnection(query_results)

    @contextmanager
    def connection(self):
        yield self._conn


def _build_mock_pool(query_results=None):
    """Build a mock pool with SQL query pattern → result mapping."""
    return MockPool(query_results or {})


def _default_pool():
    """Pool with standard vendor/spend data for most tests."""
    return _build_mock_pool({
        "WHERE id::text": {"id": "v_acme", "name": "Acme Corp"},
        "FROM vendors ORDER BY": [
            {"id": "v_acme", "name": "Acme Corp", "aliases": ["Acme"]},
            {"id": "v_beta", "name": "Beta Inc", "aliases": None},
            {"id": "v_gamma", "name": "Gamma LLC", "aliases": None},
        ],
        "COUNT(*)": {"cnt": 3},
        "v.id::text AS vendor_id, v.name AS vendor_name": [
            {"vendor_id": "v_acme", "vendor_name": "Acme Corp", "total": Decimal("25000.00"), "bills": 8},
            {"vendor_id": "v_gamma", "vendor_name": "Gamma LLC", "total": Decimal("20000.00"), "bills": 1},
            {"vendor_id": "v_beta", "vendor_name": "Beta Inc", "total": Decimal("13000.00"), "bills": 6},
        ],
        "COALESCE(SUM(s.total_amount), 0)": {"total": Decimal("58000.00"), "bills": 13, "vendors": 3},
        "TO_CHAR(s.date": [
            {"month": "2026-01", "total_amount": Decimal("10000.00"), "bill_count": 5},
            {"month": "2026-02", "total_amount": Decimal("15000.00"), "bill_count": 3},
        ],
        "AS grp,": [
            {"grp": "ACH", "total": Decimal("45000.00"), "bills": 9, "vendors": 2},
            {"grp": "Check", "total": Decimal("13000.00"), "bills": 6, "vendors": 1},
        ],
        "DISTINCT": [
            {"val": "Engineering"},
            {"val": "Marketing"},
        ],
    })


def _patch_all(pool=None):
    """Patch get_pool across tools and resolver modules."""
    if pool is None:
        pool = _default_pool()
    return (
        patch("mcp_server.tools.get_pool", return_value=pool),
        patch("mcp_server.tools.get_vendor", return_value=SAMPLE_VENDOR_API),
        patch("mcp_server.resolver.get_pool", return_value=pool),
    )


# ── vendor_lookup ────────────────────────────────────────────────────────

class TestVendorLookup:
    def test_by_name(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_lookup({"vendor": "Acme Corp"})
            assert result["status"] == "ok"
            assert result["vendor_id"] == "v_acme"

    def test_by_alias(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_lookup({"vendor": "Acme"})
            assert result["status"] == "ok"
            assert result["vendor_id"] == "v_acme"

    def test_not_found(self):
        pool = _build_mock_pool({
            "WHERE id::text": None,
            "FROM vendors ORDER BY": [],
        })
        p1, p2, p3 = _patch_all(pool)
        with p1, p2, p3:
            result = handle_vendor_lookup({"vendor": "Nonexistent"})
            assert result["status"] == "not_found"

    def test_empty_vendor(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_lookup({"vendor": ""})
            assert result["status"] == "not_found"

    def test_missing_vendor_param(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_lookup({})
            assert result["status"] == "not_found"


# ── vendor_count ─────────────────────────────────────────────────────────

class TestVendorCount:
    def test_total_count(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_count({})
            assert result["status"] == "ok"
            assert result["data"]["count"] == 3

    def test_invalid_filter(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_count({"filters": {"paymentMethod": "Bitcoin"}})
            assert result["status"] == "invalid_filter"

    def test_with_group_by(self):
        pool = _build_mock_pool({
            "COALESCE(": [
                {"grp": "Engineering", "cnt": 2},
                {"grp": "Marketing", "cnt": 1},
            ],
        })
        p1, p2, p3 = _patch_all(pool)
        with p1, p2, p3:
            result = handle_vendor_count({"group_by": "department"})
            assert result["status"] == "ok"
            counts = result["data"]["counts"]
            assert counts["Engineering"] == 2
            assert counts["Marketing"] == 1


# ── spend_total ──────────────────────────────────────────────────────────

class TestSpendTotal:
    def test_all_time(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_total({})
            assert result["status"] == "ok"
            assert result["data"]["totalAmount"] == 58000.00
            assert result["data"]["vendorCount"] == 3

    def test_invalid_period(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_total({"period": "garbage"})
            assert result["status"] == "invalid_filter"
            assert result["field"] == "period"

    def test_with_caller_context_finance_admin(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_total({}, caller_context={"is_finance_admin": True})
            assert result["status"] == "ok"
            assert result["data"]["totalAmount"] == 58000.00


# ── spend_by_vendor ──────────────────────────────────────────────────────

class TestSpendByVendor:
    def test_single_vendor(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_by_vendor({"vendor": "Acme Corp"})
            assert result["status"] == "ok"
            assert result["vendor_id"] == "v_acme"
            assert result["data"]["totalAmount"] == 25000.00
            assert len(result["data"]["months"]) == 2

    def test_vendor_not_found(self):
        pool = _build_mock_pool({
            "WHERE id::text": None,
            "FROM vendors ORDER BY": [],
        })
        p1, p2, p3 = _patch_all(pool)
        with p1, p2, p3:
            result = handle_spend_by_vendor({"vendor": "Nonexistent"})
            assert result["status"] == "not_found"

    def test_all_vendors(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_by_vendor({})
            assert result["status"] == "ok"
            assert result["data"]["totalVendors"] == 3


# ── spend_by_dimension ───────────────────────────────────────────────────

class TestSpendByDimension:
    def test_by_payment_method(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_by_dimension({"dimension": "paymentMethod"})
            assert result["status"] == "ok"
            groups = result["data"]["groups"]
            assert "ACH" in groups
            assert "Check" in groups
            assert groups["ACH"]["totalAmount"] == 45000.00
            assert groups["Check"]["totalAmount"] == 13000.00

    def test_missing_dimension(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_by_dimension({})
            assert result["status"] == "invalid_filter"


# ── top_vendors ──────────────────────────────────────────────────────────

class TestTopVendors:
    def test_default_top_10(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_top_vendors({})
            assert result["status"] == "ok"
            vendors = result["data"]["vendors"]
            assert len(vendors) == 3
            assert vendors[0]["vendor_name"] == "Acme Corp"

    def test_ranked_by_amount_descending(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_top_vendors({})
            vendors = result["data"]["vendors"]
            amounts = [v["totalAmount"] for v in vendors]
            assert amounts == sorted(amounts, reverse=True)

    def test_invalid_period(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_top_vendors({"period": "not-a-period"})
            assert result["status"] == "invalid_filter"
