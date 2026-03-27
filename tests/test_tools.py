"""Integration tests for mcp_server.tools handlers.

All Firestore access is mocked. Tests verify that handlers correctly
orchestrate resolution, period parsing, filter validation, and data
aggregation, returning the expected response contract.
"""

from unittest.mock import patch, MagicMock

import pytest

from mcp_server.tools import (
    handle_vendor_lookup,
    handle_vendor_count,
    handle_spend_total,
    handle_spend_by_vendor,
    handle_spend_by_dimension,
    handle_top_vendors,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

SAMPLE_VENDORS = [
    {
        "id": "v_acme", "name": "Acme Corp", "paymentMethod": "ACH",
        "accountType": "Business", "track1099": True, "department": "Engineering",
        "owner": "Alice", "hide": False, "aliases": ["Acme"],
    },
    {
        "id": "v_beta", "name": "Beta Inc", "paymentMethod": "Check",
        "accountType": "Individual", "track1099": True, "department": "Marketing",
        "owner": "Bob", "hide": False,
    },
    {
        "id": "v_gamma", "name": "Gamma LLC", "paymentMethod": "ACH",
        "accountType": "Business", "track1099": False, "department": "Engineering",
        "owner": "Alice", "hide": False,
    },
]

SAMPLE_SPEND = [
    {"vendorId": "v_acme", "vendorName": "Acme Corp", "month": "2026-01",
     "totalAmount": 10000.00, "billCount": 5, "paymentMethod": "ACH",
     "track1099": True, "department": "Engineering"},
    {"vendorId": "v_acme", "vendorName": "Acme Corp", "month": "2026-02",
     "totalAmount": 15000.00, "billCount": 3, "paymentMethod": "ACH",
     "track1099": True, "department": "Engineering"},
    {"vendorId": "v_beta", "vendorName": "Beta Inc", "month": "2026-01",
     "totalAmount": 5000.00, "billCount": 2, "paymentMethod": "Check",
     "track1099": True, "department": "Marketing"},
    {"vendorId": "v_beta", "vendorName": "Beta Inc", "month": "2026-02",
     "totalAmount": 8000.00, "billCount": 4, "paymentMethod": "Check",
     "track1099": True, "department": "Marketing"},
    {"vendorId": "v_gamma", "vendorName": "Gamma LLC", "month": "2026-01",
     "totalAmount": 20000.00, "billCount": 1, "paymentMethod": "ACH",
     "track1099": False, "department": "Engineering"},
]


def _mock_firestore(vendors=None, spend=None):
    """Create a mock Firestore client with vendor and spend collections."""
    if vendors is None:
        vendors = SAMPLE_VENDORS
    if spend is None:
        spend = SAMPLE_SPEND

    mock_db = MagicMock()

    def collection_factory(name):
        mock_coll = MagicMock()

        if name == "vendors":
            vendor_docs = []
            for v in vendors:
                doc = MagicMock()
                doc.id = v["id"]
                doc.to_dict.return_value = {k: v2 for k, v2 in v.items() if k != "id"}
                vendor_docs.append(doc)
            mock_coll.stream.return_value = vendor_docs

            def vendor_where(base_vendors):
                def _where(field, op, value):
                    filtered = [
                        v for v in base_vendors
                        if (op == "==" and v.get(field) == value)
                    ]
                    chained = MagicMock()
                    chained_docs = []
                    for v in filtered:
                        doc = MagicMock()
                        doc.id = v["id"]
                        doc.to_dict.return_value = {k: v2 for k, v2 in v.items() if k != "id"}
                        chained_docs.append(doc)
                    chained.stream.return_value = chained_docs
                    chained.where.side_effect = vendor_where(filtered)
                    return chained
                return _where

            mock_coll.where.side_effect = vendor_where(vendors)

            def doc_ref(doc_id):
                ref = MagicMock()
                snap = MagicMock()
                match = next((v for v in vendors if v["id"] == doc_id), None)
                if match:
                    snap.exists = True
                    snap.id = doc_id
                    snap.to_dict.return_value = {k: v2 for k, v2 in match.items() if k != "id"}
                else:
                    snap.exists = False
                    snap.id = doc_id
                ref.get.return_value = snap
                return ref

            mock_coll.document.side_effect = doc_ref

        elif name == "vendor_spend":
            def apply_where(field, op, value):
                filtered_spend = [
                    s for s in spend
                    if (op == "==" and s.get(field) == value)
                    or (op == ">=" and s.get(field, "") >= value)
                    or (op == "<=" and s.get(field, "") <= value)
                ]
                chained = MagicMock()
                chained_docs = []
                for s in filtered_spend:
                    doc = MagicMock()
                    doc.to_dict.return_value = s
                    chained_docs.append(doc)
                chained.stream.return_value = chained_docs
                chained.where.side_effect = lambda f, o, v: apply_where_on(
                    filtered_spend, f, o, v
                )
                return chained

            def apply_where_on(base, field, op, value):
                filtered = [
                    s for s in base
                    if (op == "==" and s.get(field) == value)
                    or (op == ">=" and s.get(field, "") >= value)
                    or (op == "<=" and s.get(field, "") <= value)
                ]
                chained = MagicMock()
                chained_docs = []
                for s in filtered:
                    doc = MagicMock()
                    doc.to_dict.return_value = s
                    chained_docs.append(doc)
                chained.stream.return_value = chained_docs
                chained.where.side_effect = lambda f, o, v: apply_where_on(
                    filtered, f, o, v
                )
                return chained

            spend_docs = []
            for s in spend:
                doc = MagicMock()
                doc.to_dict.return_value = s
                spend_docs.append(doc)
            mock_coll.stream.return_value = spend_docs
            mock_coll.where.side_effect = lambda f, o, v: apply_where(f, o, v)

        return mock_coll

    mock_db.collection.side_effect = collection_factory
    return mock_db


def _patch_all():
    """Return a stack of patches for Firestore access across all modules."""
    mock_db = _mock_firestore()
    return (
        patch("mcp_server.tools.get_db", return_value=mock_db),
        patch("mcp_server.tools.get_hidden_vendor_ids", return_value=set()),
        patch("mcp_server.resolver.get_db", return_value=mock_db),
    )


# ── vendor_lookup ────────────────────────────────────────────────────────

class TestVendorLookup:
    def test_by_name(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_lookup({"vendor": "Acme Corp"})
            assert result["status"] == "ok"
            assert result["vendor_id"] == "v_acme"
            assert result["data"]["name"] == "Acme Corp"

    def test_by_alias(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_lookup({"vendor": "Acme"})
            assert result["status"] == "ok"
            assert result["vendor_id"] == "v_acme"

    def test_not_found(self):
        p1, p2, p3 = _patch_all()
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

    def test_with_filter(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_count({"filters": {"track1099": True}})
            assert result["status"] == "ok"
            assert result["data"]["count"] == 2

    def test_with_group_by(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_count({"group_by": "department"})
            assert result["status"] == "ok"
            counts = result["data"]["counts"]
            assert counts["Engineering"] == 2
            assert counts["Marketing"] == 1

    def test_invalid_filter(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_vendor_count({"filters": {"paymentMethod": "Bitcoin"}})
            assert result["status"] == "invalid_filter"


# ── spend_total ──────────────────────────────────────────────────────────

class TestSpendTotal:
    def test_all_time(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_total({})
            assert result["status"] == "ok"
            assert result["data"]["totalAmount"] == 58000.00
            assert result["data"]["vendorCount"] == 3

    def test_with_period(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_total({"period": "2026-01"})
            assert result["status"] == "ok"
            assert result["data"]["totalAmount"] == 35000.00

    def test_with_filter(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_total({"filters": {"track1099": True}})
            assert result["status"] == "ok"
            assert result["data"]["totalAmount"] == 38000.00

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

    def test_with_caller_context_restricted(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_total(
                {}, caller_context={"allowed_vendor_ids": ["v_acme"]}
            )
            assert result["status"] == "ok"
            assert result["data"]["vendorCount"] == 1


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

    def test_ambiguous_vendor(self):
        """When vendor name matches multiple, return ambiguous."""
        mock_db = _mock_firestore(
            vendors=[
                {"id": "v_1", "name": "Acme Corp", "hide": False},
                {"id": "v_2", "name": "Acme Logistics", "hide": False},
            ]
        )
        with (
            patch("mcp_server.tools.get_db", return_value=mock_db),
            patch("mcp_server.tools.get_hidden_vendor_ids", return_value=set()),
            patch("mcp_server.resolver.get_db", return_value=mock_db),
        ):
            result = handle_spend_by_vendor({"vendor": "Acme"})
            assert result["status"] == "ambiguous"
            assert len(result["candidates"]) == 2

    def test_vendor_not_found(self):
        p1, p2, p3 = _patch_all()
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

    def test_by_track1099(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_by_dimension({"dimension": "track1099"})
            assert result["status"] == "ok"
            groups = result["data"]["groups"]
            assert "True" in groups
            assert "False" in groups

    def test_with_period_and_filter(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_spend_by_dimension({
                "dimension": "paymentMethod",
                "period": "2026-Q1",
                "filters": {"track1099": True},
            })
            assert result["status"] == "ok"
            groups = result["data"]["groups"]
            total = sum(g["totalAmount"] for g in groups.values())
            assert total == 38000.00

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

    def test_top_1(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_top_vendors({"n": 1})
            assert result["status"] == "ok"
            vendors = result["data"]["vendors"]
            assert len(vendors) == 1
            assert vendors[0]["vendor_id"] == "v_acme"

    def test_with_filter(self):
        p1, p2, p3 = _patch_all()
        with p1, p2, p3:
            result = handle_top_vendors({"filters": {"paymentMethod": "ACH"}})
            assert result["status"] == "ok"
            vendors = result["data"]["vendors"]
            for v in vendors:
                assert v["vendor_id"] in ("v_acme", "v_gamma")

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
