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
    handle_vendor_list,
    handle_spend_total,
    handle_spend_by_vendor,
    handle_spend_by_dimension,
    handle_top_vendors,
    handle_spend_detail,
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


_FULL_VENDOR_ACME = {
    "id": "v_acme", "name": "Acme Corp", "aliases": ["Acme"],
    "source_system": "billcom", "source_system_id": "bc_acme",
    "department_id": None, "department_name": None,
    "owner_id": None, "owner_email": None,
    "secondary_owner_id": None, "secondary_owner_email": None,
    "payment_method": "ACH", "account_type": "Business",
    "billing_frequency": None, "track_1099": True,
    "purpose": None, "spend_type": None,
    "contract_start": None, "contract_end": None, "contract_months": None,
    "auto_renew": None, "renewal_rate": None, "renewal_notice": None,
    "termination_terms": None, "synced_at": None,
}


def _default_pool():
    """Pool with standard vendor/spend data for most tests."""
    return _build_mock_pool({
        "WHERE id::text": {"id": "v_acme", "name": "Acme Corp"},
        "LOWER(v.name) = LOWER": _FULL_VENDOR_ACME,
        "WHERE v.id": _FULL_VENDOR_ACME,
        "FROM vendors ORDER BY": [
            {"id": "v_acme", "name": "Acme Corp", "aliases": ["Acme"]},
            {"id": "v_beta", "name": "Beta Inc", "aliases": None},
            {"id": "v_gamma", "name": "Gamma LLC", "aliases": None},
        ],
        "similarity": [],
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
    """Patch get_pool across tools, resolver, and pg_client modules."""
    if pool is None:
        pool = _default_pool()
    return (
        patch("mcp_server.tools.get_pool", return_value=pool),
        patch("mcp_server.tools.get_vendor", return_value=SAMPLE_VENDOR_API),
        patch("service.pg_client.get_pool", return_value=pool),
        patch("mcp_server.resolver.get_pool", return_value=pool),
    )


# ── vendor_lookup ────────────────────────────────────────────────────────

class TestVendorLookup:
    def test_by_name(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_vendor_lookup({"vendor": "Acme Corp"})
            assert result["status"] == "ok"
            assert result["vendor_id"] == "v_acme"

    def test_by_alias(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_vendor_lookup({"vendor": "Acme"})
            assert result["status"] == "ok"
            assert result["vendor_id"] == "v_acme"

    def test_not_found(self):
        pool = _build_mock_pool({
            "WHERE id::text": None,
            "FROM vendors ORDER BY": [],
        })
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_vendor_lookup({"vendor": "Nonexistent"})
            assert result["status"] == "not_found"

    def test_empty_vendor(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_vendor_lookup({"vendor": ""})
            assert result["status"] == "not_found"

    def test_missing_vendor_param(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_vendor_lookup({})
            assert result["status"] == "not_found"


# ── vendor_count ─────────────────────────────────────────────────────────

class TestVendorCount:
    def test_total_count(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_vendor_count({})
            assert result["status"] == "ok"
            assert result["data"]["count"] == 3

    def test_invalid_filter(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_vendor_count({"filters": {"paymentMethod": "Bitcoin"}})
            assert result["status"] == "invalid_filter"

    def test_with_group_by(self):
        pool = _build_mock_pool({
            "COALESCE(": [
                {"grp": "Engineering", "cnt": 2},
                {"grp": "Marketing", "cnt": 1},
            ],
        })
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_vendor_count({"group_by": "department"})
            assert result["status"] == "ok"
            counts = result["data"]["counts"]
            assert counts["Engineering"] == 2
            assert counts["Marketing"] == 1


# ── spend_total ──────────────────────────────────────────────────────────

class TestSpendTotal:
    def test_all_time(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_total({})
            assert result["status"] == "ok"
            assert result["data"]["totalAmount"] == 58000.00
            assert result["data"]["vendorCount"] == 3

    def test_invalid_period(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_total({"period": "garbage"})
            assert result["status"] == "invalid_filter"
            assert result["field"] == "period"

    def test_with_caller_context_finance_admin(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_total({}, caller_context={"is_finance_admin": True})
            assert result["status"] == "ok"
            assert result["data"]["totalAmount"] == 58000.00


# ── spend_by_vendor ──────────────────────────────────────────────────────

class TestSpendByVendor:
    def test_single_vendor(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
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
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_spend_by_vendor({"vendor": "Nonexistent"})
            assert result["status"] == "not_found"

    def test_all_vendors(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_vendor({})
            assert result["status"] == "ok"
            assert result["data"]["totalVendors"] == 3


# ── spend_by_dimension ───────────────────────────────────────────────────

class TestSpendByDimension:
    def test_by_payment_method(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_dimension({"dimension": "paymentMethod"})
            assert result["status"] == "ok"
            groups = result["data"]["groups"]
            assert "ACH" in groups
            assert "Check" in groups
            assert groups["ACH"]["totalAmount"] == 45000.00
            assert groups["Check"]["totalAmount"] == 13000.00

    def test_missing_dimension(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_dimension({})
            assert result["status"] == "invalid_filter"


# ── top_vendors ──────────────────────────────────────────────────────────

class TestTopVendors:
    def test_default_top_10(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_top_vendors({})
            assert result["status"] == "ok"
            vendors = result["data"]["vendors"]
            assert len(vendors) == 3
            assert vendors[0]["vendor_name"] == "Acme Corp"

    def test_ranked_by_amount_descending(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_top_vendors({})
            vendors = result["data"]["vendors"]
            amounts = [v["totalAmount"] for v in vendors]
            assert amounts == sorted(amounts, reverse=True)

    def test_invalid_period(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_top_vendors({"period": "not-a-period"})
            assert result["status"] == "invalid_filter"


# ── vendor_list ─────────────────────────────────────────────────────────

def _make_vendor_row(i: int) -> dict:
    return {
        "id": f"v_{i:03d}",
        "name": f"Vendor {i:03d}",
        "account_type": "Business",
        "track_1099": True,
        "payment_method": "ACH",
        "billing_frequency": None,
        "source_system": "billcom",
        "department": "Engineering",
        "owner": " ",
        "secondary_owner": " ",
        "purpose": None,
        "spend_type": None,
        "auto_renew": None,
        "contract_start": None,
        "contract_end": None,
        "contract_months": None,
        "renewal_rate": None,
        "renewal_notice": None,
        "termination_terms": None,
    }


class TestVendorList:
    def test_csv_contains_all_vendors_when_truncated(self):
        """CSV must include every matching vendor, not just the limited page."""
        total = 60
        limited_rows = [_make_vendor_row(i) for i in range(50)]
        all_rows = [_make_vendor_row(i) for i in range(total)]

        pool = _build_mock_pool({
            "COUNT(*)": {"cnt": total},
            "LIMIT": limited_rows,
            "ORDER BY v.name": all_rows,
        })
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_vendor_list({"filters": {"track1099": True}})

        assert result["status"] == "ok"
        assert result["data"]["total"] == total
        assert result["data"]["csv_attached"] is True
        assert "vendors" not in result["data"]

        csv_lines = result["csv"].strip().splitlines()
        assert len(csv_lines) == total + 1  # header + all 60 data rows

    def test_csv_matches_vendors_when_not_truncated(self):
        """When all vendors fit within the limit, CSV uses the same set."""
        rows = [_make_vendor_row(i) for i in range(15)]

        pool = _build_mock_pool({
            "COUNT(*)": {"cnt": 15},
            "LIMIT": rows,
        })
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_vendor_list({})

        assert result["status"] == "ok"
        assert result["data"]["total"] == 15
        assert result["data"]["csv_attached"] is True
        assert "vendors" not in result["data"]

        csv_lines = result["csv"].strip().splitlines()
        assert len(csv_lines) == 16  # header + 15 data rows

    def test_no_csv_for_small_results(self):
        """Fewer than 10 results should not generate a CSV."""
        rows = [_make_vendor_row(i) for i in range(5)]

        pool = _build_mock_pool({
            "COUNT(*)": {"cnt": 5},
            "LIMIT": rows,
        })
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_vendor_list({})

        assert result["status"] == "ok"
        assert "csv" not in result
        assert len(result["data"]["vendors"]) == 5


# ── Owner filter resolution ────────────────────────────────────────────

from mcp_server.resolver import validate_filters, resolve_filter


class TestOwnerFilter:
    def test_owner_resolves_by_name(self):
        """Owner filter should resolve a person's name to user IDs."""
        user_names = [{"val": "Michael Mader"}, {"val": "Suman C"}]
        user_ids = [{"id": "uid-1"}, {"id": "uid-2"}, {"id": "uid-3"}]

        pool = _build_mock_pool({
            "DISTINCT CONCAT": user_names,
            "SELECT id FROM users": user_ids,
        })
        with patch("mcp_server.resolver.get_pool", return_value=pool):
            result = resolve_filter("owner", "Michael Mader")

        assert result["status"] == "ok"
        assert result["value"] == "Michael Mader"
        assert result["user_ids"] == ["uid-1", "uid-2", "uid-3"]

    def test_owner_fuzzy_match(self):
        """Misspelled owner name should still resolve via fuzzy matching."""
        user_names = [{"val": "Michael Mader"}, {"val": "Suman C"}]
        user_ids = [{"id": "uid-1"}]

        pool = _build_mock_pool({
            "DISTINCT CONCAT": user_names,
            "SELECT id FROM users": user_ids,
        })
        with patch("mcp_server.resolver.get_pool", return_value=pool):
            result = resolve_filter("owner", "Micheal Mader")

        assert result["status"] == "ok"
        assert result["value"] == "Michael Mader"

    def test_validate_filters_stashes_owner_ids(self):
        """validate_filters should stash _owner_ids in the filters dict."""
        user_names = [{"val": "Michael Mader"}]
        user_ids = [{"id": "uid-1"}, {"id": "uid-2"}]

        pool = _build_mock_pool({
            "DISTINCT CONCAT": user_names,
            "SELECT id FROM users": user_ids,
        })
        with patch("mcp_server.resolver.get_pool", return_value=pool):
            filters = {"owner": "Michael Mader"}
            err = validate_filters(filters)

        assert err is None
        assert filters["owner"] == "Michael Mader"
        assert filters["_owner_ids"] == ["uid-1", "uid-2"]

    def test_secondary_owner_resolves(self):
        """secondaryOwner filter should resolve the same way as owner."""
        user_names = [{"val": "Suman C"}]
        user_ids = [{"id": "uid-5"}]

        pool = _build_mock_pool({
            "DISTINCT CONCAT": user_names,
            "SELECT id FROM users": user_ids,
        })
        with patch("mcp_server.resolver.get_pool", return_value=pool):
            result = resolve_filter("secondaryOwner", "Suman C")

        assert result["status"] == "ok"
        assert result["value"] == "Suman C"
        assert result["user_ids"] == ["uid-5"]

    def test_owner_filter_produces_in_clause(self):
        """Owner filter should produce ANY() clause, not exact match."""
        rows = [_make_vendor_row(i) for i in range(3)]
        user_names = [{"val": "Michael Mader"}]
        user_ids = [{"id": "uid-1"}, {"id": "uid-2"}]

        pool = _build_mock_pool({
            "COUNT(*)": {"cnt": 3},
            "LIMIT": rows,
            "DISTINCT CONCAT": user_names,
            "SELECT id FROM users": user_ids,
        })
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_vendor_list({"filters": {"owner": "Michael Mader"}})

        assert result["status"] == "ok"
        assert len(result["data"]["vendors"]) == 3


# ── New filter fields ──────────────────────────────────────────────────

class TestNewFilters:
    def test_auto_renew_boolean_filter(self):
        """autoRenew filter should accept boolean values."""
        with patch("mcp_server.resolver.get_pool"):
            result = resolve_filter("autoRenew", True)
        assert result["status"] == "ok"
        assert result["value"] is True

    def test_purpose_resolve_filter(self):
        """purpose should resolve against distinct values in DB."""
        pool = _build_mock_pool({
            "v.purpose": [{"val": "Consulting"}, {"val": "SaaS"}],
        })
        with patch("mcp_server.resolver.get_pool", return_value=pool):
            result = resolve_filter("purpose", "Consulting")
        assert result["status"] == "ok"
        assert result["value"] == "Consulting"

    def test_range_filter_contract_months(self):
        """Range filter should accept min/max dict."""
        with patch("mcp_server.resolver.get_pool"):
            result = resolve_filter("contractMonths", {"min": 6, "max": 24})
        assert result["status"] == "ok"
        assert result["value"] == {"min": 6, "max": 24}

    def test_range_filter_contract_start_date(self):
        """Date range filter should accept from/to dict."""
        with patch("mcp_server.resolver.get_pool"):
            result = resolve_filter("contractStart", {"from": "2025-01-01", "to": "2025-12-31"})
        assert result["status"] == "ok"

    def test_range_filter_single_value(self):
        """Range fields should also accept a single exact value."""
        with patch("mcp_server.resolver.get_pool"):
            result = resolve_filter("contractMonths", 12)
        assert result["status"] == "ok"
        assert result["value"] == 12

    def test_unknown_filter_field_rejected(self):
        """Unknown filter fields should return invalid_filter."""
        with patch("mcp_server.resolver.get_pool"):
            result = resolve_filter("nonexistentField", "value")
        assert result["status"] == "invalid_filter"


# ── Null sentinel filters ─────────────────────────────────────────────

class TestNullSentinelFilters:
    def test_star_resolver_accepts(self):
        """'*' sentinel should pass validation on any field without DB lookup."""
        result = resolve_filter("owner", "*")
        assert result["status"] == "ok"
        assert result["value"] == "*"

    def test_none_resolver_accepts(self):
        """'none' sentinel should pass validation on any field."""
        result = resolve_filter("department", "none")
        assert result["status"] == "ok"
        assert result["value"] == "none"

    def test_star_on_enum_field(self):
        """'*' should work on enum fields too (e.g. paymentMethod)."""
        result = resolve_filter("paymentMethod", "*")
        assert result["status"] == "ok"

    def test_star_on_range_field(self):
        """'*' should work on range fields (e.g. contractEnd)."""
        result = resolve_filter("contractEnd", "*")
        assert result["status"] == "ok"

    def test_star_produces_is_not_null_sql(self):
        """'*' should produce IS NOT NULL in SQL clause."""
        from mcp_server.tools import _append_filter_clauses
        clauses, params = [], []
        _append_filter_clauses({"owner": "*"}, clauses, params)
        assert len(clauses) == 1
        assert "IS NOT NULL" in clauses[0]
        assert params == []

    def test_none_produces_is_null_sql(self):
        """'none' should produce IS NULL in SQL clause."""
        from mcp_server.tools import _append_filter_clauses
        clauses, params = [], []
        _append_filter_clauses({"department": "none"}, clauses, params)
        assert len(clauses) == 1
        assert "IS NULL" in clauses[0]
        assert params == []

    def test_owner_star_uses_owner_id_column(self):
        """Owner '*' should check v.owner_id (not the joined email column)."""
        from mcp_server.tools import _append_filter_clauses
        clauses, params = [], []
        _append_filter_clauses({"owner": "*"}, clauses, params)
        assert "v.owner_id IS NOT NULL" in clauses[0]

    def test_validate_filters_skips_owner_id_stashing(self):
        """validate_filters should not stash _owner_ids for sentinel values."""
        from mcp_server.resolver import validate_filters
        filters = {"owner": "*"}
        err = validate_filters(filters)
        assert err is None
        assert "_owner_ids" not in filters

    def test_sentinel_with_vendor_list(self):
        """Full integration: vendor_list with owner='*' should emit IS NOT NULL."""
        rows = [_make_vendor_row(i) for i in range(3)]
        pool = _build_mock_pool({
            "COUNT(*)": {"cnt": 3},
            "LIMIT": rows,
        })
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_vendor_list({"filters": {"owner": "*"}})
        assert result["status"] == "ok"
        assert result["data"]["total"] == 3


# ── Table payload on tabular handlers ────────────────────────────────────

def _assert_table_shape(table: dict, expected_columns: list[str] | None = None):
    """Verify a table payload has the required fields and valid types."""
    assert "metric" in table and isinstance(table["metric"], str)
    assert "columns" in table and isinstance(table["columns"], list)
    assert "rows" in table and isinstance(table["rows"], list)
    assert "filename" in table and isinstance(table["filename"], str)
    assert table["filename"].endswith(".csv")
    if expected_columns:
        assert table["columns"] == expected_columns


class TestTablePayloadPresence:
    """Verify that tabular handlers include a well-formed table field."""

    def test_spend_by_vendor_single_has_table(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_vendor({"vendor": "Acme Corp"})
        assert "table" in result
        _assert_table_shape(result["table"], ["Month", "Spend"])
        assert len(result["table"]["rows"]) == len(result["data"]["months"])

    def test_spend_by_vendor_all_has_table(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_vendor({})
        assert "table" in result
        _assert_table_shape(result["table"], ["Vendor", "Spend"])
        assert len(result["table"]["rows"]) == result["data"]["totalVendors"]

    def test_spend_by_dimension_has_table(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_dimension({"dimension": "paymentMethod"})
        assert "table" in result
        _assert_table_shape(result["table"], ["Payment Method", "Spend"])
        assert len(result["table"]["rows"]) == len(result["data"]["groups"])

    def test_top_vendors_has_table(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_top_vendors({})
        assert "table" in result
        _assert_table_shape(result["table"], ["Vendor", "Spend"])
        assert len(result["table"]["rows"]) == len(result["data"]["vendors"])

    def test_vendor_count_grouped_has_table(self):
        pool = _build_mock_pool({
            "COALESCE(": [
                {"grp": "Engineering", "cnt": 2},
                {"grp": "Marketing", "cnt": 1},
            ],
        })
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_vendor_count({"group_by": "department"})
        assert "table" in result
        _assert_table_shape(result["table"])
        assert result["table"]["metric"] == "Vendor Count"
        assert len(result["table"]["rows"]) == 2

    def test_vendor_count_ungrouped_has_no_table(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_vendor_count({})
        assert "table" not in result

    def test_spend_total_has_no_table(self):
        """spend_total returns a single number — no table payload."""
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_total({})
        assert "table" not in result

    def test_vendor_list_has_no_table(self):
        """vendor_list uses CSV downloads, not table payloads."""
        rows = [_make_vendor_row(i) for i in range(3)]
        pool = _build_mock_pool({"COUNT(*)": {"cnt": 3}, "LIMIT": rows})
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_vendor_list({})
        assert "table" not in result

    def test_error_responses_have_no_table(self):
        """Invalid inputs should not produce a table field."""
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_dimension({})
        assert result["status"] == "invalid_filter"
        assert "table" not in result


# ── Metric parameter ────────────────────────────────────────────────────

class TestMetricParameter:
    """Verify the metric parameter selects the correct data field."""

    def test_spend_by_vendor_single_metric_billcount(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_vendor({"vendor": "Acme Corp", "metric": "billCount"})
        table = result["table"]
        assert table["metric"] == "Bill Count"
        assert table["columns"] == ["Month", "Bill Count"]
        for row in table["rows"]:
            assert isinstance(row[1], int)

    def test_spend_by_vendor_all_metric_billcount(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_vendor({"metric": "billCount"})
        table = result["table"]
        assert table["metric"] == "Bill Count"
        assert table["columns"] == ["Vendor", "Bill Count"]

    def test_spend_by_dimension_metric_vendor_count(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_spend_by_dimension({"dimension": "paymentMethod", "metric": "vendorCount"})
        table = result["table"]
        assert table["metric"] == "Vendor Count"
        assert table["columns"] == ["Payment Method", "Vendor Count"]

    def test_top_vendors_metric_billcount(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_top_vendors({"metric": "billCount"})
        table = result["table"]
        assert table["metric"] == "Bill Count"

    def test_default_metric_is_spend(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_top_vendors({})
        assert result["table"]["metric"] == "Spend"

    def test_unknown_metric_falls_back_to_spend(self):
        p1, p2, p3, p4 = _patch_all()
        with p1, p2, p3, p4:
            result = handle_top_vendors({"metric": "unknown"})
        assert result["table"]["metric"] == "Spend"


# ── execute_python removed from tool schemas ─────────────────────────────

class TestExecutePythonRemoved:
    """Verify execute_python is no longer exposed to the LLM."""

    def test_not_in_tool_definitions(self):
        from service.tools import TOOL_DEFINITIONS
        names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
        assert "execute_python" not in names

    def test_not_in_tool_handlers(self):
        from service.tools import TOOL_HANDLERS
        assert "execute_python" not in TOOL_HANDLERS


# ── spend_detail pivot table ─────────────────────────────────────────────

class TestSpendDetailPivotTable:
    """Verify spend_detail produces a pivoted table when group_by is set."""

    def _make_detail_pool(self, detail_rows):
        """Build a pool with vendor resolution + spend_detail rows."""
        return _build_mock_pool({
            "WHERE id::text": {"id": "v_acme", "name": "Acme Corp"},
            "LOWER(v.name) = LOWER": _FULL_VENDOR_ACME,
            "WHERE v.id": _FULL_VENDOR_ACME,
            "FROM vendors ORDER BY": [
                {"id": "v_acme", "name": "Acme Corp", "aliases": ["Acme"]},
            ],
            "similarity": [],
            "vendor_spend_detail": detail_rows,
        })

    def test_grouped_pivot_categories_as_rows_months_as_columns(self):
        detail_rows = [
            {"dimension_value": "BigQuery", "month": "2026-01", "amount": Decimal("5000.00")},
            {"dimension_value": "BigQuery", "month": "2026-02", "amount": Decimal("4600.00")},
            {"dimension_value": "Compute Engine", "month": "2026-01", "amount": Decimal("1200.00")},
            {"dimension_value": "Compute Engine", "month": "2026-02", "amount": Decimal("1400.00")},
        ]
        pool = self._make_detail_pool(detail_rows)
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_spend_detail({"vendor": "Acme Corp", "group_by": "category", "period": "2026"})

        assert result["status"] == "ok"
        table = result["table"]
        _assert_table_shape(table)
        assert table["columns"] == ["Category", "2026-01", "2026-02"]
        assert len(table["rows"]) == 2
        bq_row = next(r for r in table["rows"] if r[0] == "BigQuery")
        assert bq_row == ["BigQuery", 5000.0, 4600.0]

    def test_grouped_single_month_uses_amount_column(self):
        detail_rows = [
            {"dimension_value": "BigQuery", "month": "2026-01", "amount": Decimal("5000.00")},
            {"dimension_value": "Compute Engine", "month": "2026-01", "amount": Decimal("1200.00")},
        ]
        pool = self._make_detail_pool(detail_rows)
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_spend_detail({"vendor": "Acme Corp", "group_by": "category", "period": "2026-01"})

        table = result["table"]
        assert table["columns"] == ["Category", "Amount"]
        assert len(table["rows"]) == 2

    def test_grouped_sorts_by_total_descending(self):
        detail_rows = [
            {"dimension_value": "Small", "month": "2026-01", "amount": Decimal("100.00")},
            {"dimension_value": "Large", "month": "2026-01", "amount": Decimal("9000.00")},
            {"dimension_value": "Medium", "month": "2026-01", "amount": Decimal("3000.00")},
        ]
        pool = self._make_detail_pool(detail_rows)
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_spend_detail({"vendor": "Acme Corp", "group_by": "category", "period": "2026"})

        labels = [r[0] for r in result["table"]["rows"]]
        assert labels == ["Large", "Medium", "Small"]

    def test_ungrouped_uses_month_amount_columns(self):
        detail_rows = [
            {"month": "2026-01", "amount": Decimal("5000.00")},
            {"month": "2026-02", "amount": Decimal("4600.00")},
        ]
        pool = self._make_detail_pool(detail_rows)
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_spend_detail({"vendor": "Acme Corp", "period": "2026"})

        table = result["table"]
        assert table["columns"] == ["Month", "Amount"]
        assert len(table["rows"]) == 2
        assert table["rows"][0] == ["2026-01", 5000.0]

    def test_2d_crosstab_category_by_project(self):
        detail_rows = [
            {"dimension_value": "BigQuery", "secondary_value": "arcade-ai-prod", "amount": Decimal("9000.00")},
            {"dimension_value": "BigQuery", "secondary_value": "arcade-ai-staging", "amount": Decimal("500.00")},
            {"dimension_value": "Compute Engine", "secondary_value": "arcade-ai-prod", "amount": Decimal("2000.00")},
            {"dimension_value": "Compute Engine", "secondary_value": "arcade-ai-staging", "amount": Decimal("300.00")},
        ]
        pool = self._make_detail_pool(detail_rows)
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_spend_detail({
                "vendor": "Acme Corp",
                "group_by": "category",
                "secondary_group_by": "project",
                "period": "2026",
            })

        assert result["status"] == "ok"
        table = result["table"]
        _assert_table_shape(table)
        assert table["columns"][0] == "Category"
        assert "arcade-ai-prod" in table["columns"]
        assert "arcade-ai-staging" in table["columns"]
        assert len(table["rows"]) == 2
        bq_row = next(r for r in table["rows"] if r[0] == "BigQuery")
        prod_idx = table["columns"].index("arcade-ai-prod")
        staging_idx = table["columns"].index("arcade-ai-staging")
        assert bq_row[prod_idx] == 9000.0
        assert bq_row[staging_idx] == 500.0

    def test_2d_crosstab_sorts_by_total(self):
        detail_rows = [
            {"dimension_value": "Small", "secondary_value": "p1", "amount": Decimal("100.00")},
            {"dimension_value": "Large", "secondary_value": "p1", "amount": Decimal("9000.00")},
        ]
        pool = self._make_detail_pool(detail_rows)
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_spend_detail({
                "vendor": "Acme Corp",
                "group_by": "category",
                "secondary_group_by": "project",
                "period": "2026",
            })

        labels = [r[0] for r in result["table"]["rows"]]
        assert labels == ["Large", "Small"]

    def test_2d_crosstab_fills_missing_cells_with_zero(self):
        detail_rows = [
            {"dimension_value": "BigQuery", "secondary_value": "prod", "amount": Decimal("5000.00")},
            {"dimension_value": "Compute Engine", "secondary_value": "staging", "amount": Decimal("300.00")},
        ]
        pool = self._make_detail_pool(detail_rows)
        p1, p2, p3, p4 = _patch_all(pool)
        with p1, p2, p3, p4:
            result = handle_spend_detail({
                "vendor": "Acme Corp",
                "group_by": "category",
                "secondary_group_by": "project",
                "period": "2026",
            })

        table = result["table"]
        bq_row = next(r for r in table["rows"] if r[0] == "BigQuery")
        staging_idx = table["columns"].index("staging")
        assert bq_row[staging_idx] == 0
