"""Intent-aligned vendor analytics tool handlers.

Each handler accepts a dict of parameters (from LLM tool call) and an optional
``caller_context`` for spend-level access control.  All handlers return a dict
with a ``status`` field — one of ``ok``, ``ambiguous``, ``not_found``,
``not_authorized``, or ``invalid_filter``.

The handlers delegate to:
    * ``resolver.resolve_vendor`` — vendor name/ID/alias resolution
    * ``resolver.validate_filters`` — enum + dynamic filter validation
    * ``period_parser.parse_period`` — period string → month range
    * ``service.firestore_client`` — Firestore queries
"""

from __future__ import annotations

from typing import Any

from service.firestore_client import (
    get_db,
    VENDORS_COLLECTION,
    VENDOR_SPEND_COLLECTION,
    SEARCH_RETURN_FIELDS,
    get_hidden_vendor_ids,
)
from .resolver import resolve_vendor, validate_filters
from .period_parser import parse_period, PeriodParseError


# ── Helpers ──────────────────────────────────────────────────────────────

CallerContext = dict[str, Any] | None


def _period_or_error(period: str | None) -> tuple[str | None, str | None] | dict:
    """Parse a period string, returning the tuple or an invalid_filter dict."""
    try:
        return parse_period(period)
    except PeriodParseError as exc:
        return {
            "status": "invalid_filter",
            "field": "period",
            "provided": exc.raw,
            "valid_values": ["YYYY-MM", "YYYY-QN", "YYYY-HN", "YYYY", "YTD", "last-N-months"],
        }


def _check_spend_auth(
    caller_context: CallerContext,
    vendor_id: str | None = None,
    vendor_name: str | None = None,
) -> dict | None:
    """Check if caller is authorised for spend data. Returns an error dict
    or None if authorised.  When ``caller_context`` is None, access is
    unrestricted (dev/admin use)."""
    if caller_context is None:
        return None
    if caller_context.get("is_finance_admin"):
        return None
    allowed = caller_context.get("allowed_vendor_ids") or []
    if vendor_id and vendor_id not in allowed:
        name = vendor_name or vendor_id
        return {
            "status": "not_authorized",
            "message": f"You don't have access to spend data for {name}.",
        }
    return None


def _filter_allowed_spend_docs(
    docs: list[dict],
    caller_context: CallerContext,
) -> list[dict]:
    """For aggregate queries, silently scope docs to the caller's allowed
    vendor IDs.  Finance admins and None contexts pass through unfiltered."""
    if caller_context is None:
        return docs
    if caller_context.get("is_finance_admin"):
        return docs
    allowed = set(caller_context.get("allowed_vendor_ids") or [])
    if not allowed:
        return []
    return [d for d in docs if d.get("vendorId") in allowed]


def _apply_filters(data: dict, filters: dict) -> bool:
    """Return True if *data* matches all filter conditions."""
    for field, value in filters.items():
        if data.get(field) != value:
            return False
    return True


def _load_spend_docs(
    start_month: str | None,
    end_month: str | None,
    vendor_id: str | None = None,
) -> list[dict]:
    """Stream spend docs from Firestore with optional month range and vendor filter.

    When vendor_id is provided, queries only by vendorId (single-field index)
    and filters the month range in Python to avoid requiring a composite index.
    """
    db = get_db()
    hidden_ids = get_hidden_vendor_ids()
    ref = db.collection(VENDOR_SPEND_COLLECTION)

    if vendor_id:
        ref = ref.where("vendorId", "==", vendor_id)
    elif start_month or end_month:
        if start_month and end_month and start_month == end_month:
            ref = ref.where("month", "==", start_month)
        else:
            if start_month:
                ref = ref.where("month", ">=", start_month)
            if end_month:
                ref = ref.where("month", "<=", end_month)

    results = []
    for doc in ref.stream():
        data = doc.to_dict()
        if data.get("vendorId") in hidden_ids:
            continue
        month = data.get("month", "")
        if vendor_id and start_month and month < start_month:
            continue
        if vendor_id and end_month and month > end_month:
            continue
        results.append(data)
    return results


# ── Tool handlers ────────────────────────────────────────────────────────

def handle_vendor_lookup(args: dict, caller_context: CallerContext = None) -> dict:
    """Look up a single vendor by name, ID, or alias. Returns the full
    vendor profile.  Unrestricted — no spend filtering applied."""
    vendor_input = args.get("vendor", "")
    if not vendor_input:
        return {"status": "not_found", "message": "No vendor specified."}

    resolved = resolve_vendor(vendor_input)
    if resolved["status"] != "ok":
        return resolved

    db = get_db()
    snap = db.collection(VENDORS_COLLECTION).document(resolved["vendor_id"]).get()
    if not snap.exists:
        return {"status": "not_found", "message": f"Vendor doc '{resolved['vendor_id']}' not found."}

    data = snap.to_dict()
    profile = {"id": snap.id}
    for f in SEARCH_RETURN_FIELDS:
        if f in data:
            profile[f] = data[f]

    return {
        "status": "ok",
        "vendor_id": resolved["vendor_id"],
        "vendor_name": resolved["vendor_name"],
        "data": profile,
    }


def handle_vendor_count(args: dict, caller_context: CallerContext = None) -> dict:
    """Count vendors, optionally filtered and/or grouped by a dimension."""
    filters = dict(args.get("filters") or {})
    group_by = args.get("group_by")

    filter_err = validate_filters(filters)
    if filter_err:
        return filter_err

    db = get_db()
    ref = db.collection(VENDORS_COLLECTION)

    if filters and not group_by:
        for field, value in filters.items():
            ref = ref.where(field, "==", value)

    docs = ref.stream()
    matched: list[dict] = []
    for doc in docs:
        data = doc.to_dict()
        if data.get("hide"):
            continue
        if filters and group_by:
            if not _apply_filters(data, filters):
                continue
        matched.append(data)

    if group_by:
        counts: dict[str, int] = {}
        for data in matched:
            key = str(data.get(group_by, "Unknown"))
            counts[key] = counts.get(key, 0) + 1
        return {
            "status": "ok",
            "data": {"counts": counts, "total": len(matched)},
        }

    return {
        "status": "ok",
        "data": {"count": len(matched)},
    }


def handle_spend_total(args: dict, caller_context: CallerContext = None) -> dict:
    """Grand total spend for a period, optionally filtered."""
    period = args.get("period")
    filters = dict(args.get("filters") or {})

    parsed = _period_or_error(period)
    if isinstance(parsed, dict):
        return parsed
    start_month, end_month = parsed

    filter_err = validate_filters(filters)
    if filter_err:
        return filter_err

    docs = _load_spend_docs(start_month, end_month)
    docs = _filter_allowed_spend_docs(docs, caller_context)

    if filters:
        docs = [d for d in docs if _apply_filters(d, filters)]

    total = round(sum(d.get("totalAmount", 0) for d in docs), 2)
    bill_count = sum(d.get("billCount", 0) for d in docs)
    vendor_count = len({d.get("vendorId") for d in docs})

    return {
        "status": "ok",
        "data": {
            "totalAmount": total,
            "billCount": bill_count,
            "vendorCount": vendor_count,
            "period": {"start": start_month, "end": end_month},
        },
    }


def handle_spend_by_vendor(args: dict, caller_context: CallerContext = None) -> dict:
    """Spend for a single vendor (or all vendors) in a period."""
    vendor_input = args.get("vendor")
    period = args.get("period")
    filters = dict(args.get("filters") or {})

    parsed = _period_or_error(period)
    if isinstance(parsed, dict):
        return parsed
    start_month, end_month = parsed

    filter_err = validate_filters(filters)
    if filter_err:
        return filter_err

    vendor_id = None
    vendor_name = None
    if vendor_input:
        resolved = resolve_vendor(vendor_input)
        if resolved["status"] != "ok":
            return resolved
        vendor_id = resolved["vendor_id"]
        vendor_name = resolved["vendor_name"]

        auth_err = _check_spend_auth(caller_context, vendor_id, vendor_name)
        if auth_err:
            return auth_err

    docs = _load_spend_docs(start_month, end_month, vendor_id=vendor_id)

    if not vendor_input:
        docs = _filter_allowed_spend_docs(docs, caller_context)

    if filters:
        docs = [d for d in docs if _apply_filters(d, filters)]

    if vendor_id:
        docs.sort(key=lambda r: r.get("month", ""))
        months = [
            {"month": d.get("month"), "totalAmount": d.get("totalAmount", 0), "billCount": d.get("billCount", 0)}
            for d in docs
        ]
        total = round(sum(m["totalAmount"] for m in months), 2)
        return {
            "status": "ok",
            "vendor_id": vendor_id,
            "vendor_name": vendor_name,
            "data": {
                "months": months,
                "totalAmount": total,
                "period": {"start": start_month, "end": end_month},
            },
        }

    # All vendors — group by vendor
    by_vendor: dict[str, dict] = {}
    for d in docs:
        vid = d.get("vendorId", "")
        if vid not in by_vendor:
            by_vendor[vid] = {"vendor_id": vid, "vendor_name": d.get("vendorName", vid), "totalAmount": 0.0, "billCount": 0}
        by_vendor[vid]["totalAmount"] = round(by_vendor[vid]["totalAmount"] + d.get("totalAmount", 0), 2)
        by_vendor[vid]["billCount"] += d.get("billCount", 0)

    results = sorted(by_vendor.values(), key=lambda v: v["totalAmount"], reverse=True)
    grand_total = round(sum(v["totalAmount"] for v in results), 2)

    return {
        "status": "ok",
        "data": {
            "vendors": results[:50],
            "totalVendors": len(results),
            "grandTotal": grand_total,
            "period": {"start": start_month, "end": end_month},
        },
    }


def handle_spend_by_dimension(args: dict, caller_context: CallerContext = None) -> dict:
    """Spend grouped by a single dimension (e.g. paymentMethod, department)."""
    dimension = args.get("dimension")
    if not dimension:
        return {"status": "invalid_filter", "field": "dimension", "provided": None, "valid_values": _groupable_fields()}

    period = args.get("period")
    filters = dict(args.get("filters") or {})

    parsed = _period_or_error(period)
    if isinstance(parsed, dict):
        return parsed
    start_month, end_month = parsed

    filter_err = validate_filters(filters)
    if filter_err:
        return filter_err

    docs = _load_spend_docs(start_month, end_month)
    docs = _filter_allowed_spend_docs(docs, caller_context)

    if filters:
        docs = [d for d in docs if _apply_filters(d, filters)]

    groups: dict[str, dict] = {}
    for d in docs:
        raw = d.get(dimension)
        key = "Unknown" if raw is None else str(raw)
        if key not in groups:
            groups[key] = {"totalAmount": 0.0, "billCount": 0, "vendorCount": 0}
        groups[key]["totalAmount"] = round(groups[key]["totalAmount"] + d.get("totalAmount", 0), 2)
        groups[key]["billCount"] += d.get("billCount", 0)
        groups[key]["vendorCount"] += 1

    grand_total = round(sum(g["totalAmount"] for g in groups.values()), 2)
    sorted_groups = dict(sorted(groups.items(), key=lambda kv: kv[1]["totalAmount"], reverse=True))

    return {
        "status": "ok",
        "data": {
            "dimension": dimension,
            "groups": sorted_groups,
            "grandTotal": grand_total,
            "period": {"start": start_month, "end": end_month},
        },
    }


def handle_top_vendors(args: dict, caller_context: CallerContext = None) -> dict:
    """Top N vendors by spend in a period."""
    n = args.get("n", 10)
    period = args.get("period")
    filters = dict(args.get("filters") or {})

    parsed = _period_or_error(period)
    if isinstance(parsed, dict):
        return parsed
    start_month, end_month = parsed

    filter_err = validate_filters(filters)
    if filter_err:
        return filter_err

    docs = _load_spend_docs(start_month, end_month)
    docs = _filter_allowed_spend_docs(docs, caller_context)

    if filters:
        docs = [d for d in docs if _apply_filters(d, filters)]

    by_vendor: dict[str, dict] = {}
    for d in docs:
        vid = d.get("vendorId", "")
        if vid not in by_vendor:
            by_vendor[vid] = {"vendor_id": vid, "vendor_name": d.get("vendorName", vid), "totalAmount": 0.0, "billCount": 0}
        by_vendor[vid]["totalAmount"] = round(by_vendor[vid]["totalAmount"] + d.get("totalAmount", 0), 2)
        by_vendor[vid]["billCount"] += d.get("billCount", 0)

    ranked = sorted(by_vendor.values(), key=lambda v: v["totalAmount"], reverse=True)[:n]
    grand_total = round(sum(v["totalAmount"] for v in ranked), 2)

    return {
        "status": "ok",
        "data": {
            "vendors": ranked,
            "grandTotal": grand_total,
            "n": n,
            "period": {"start": start_month, "end": end_month},
        },
    }


def _groupable_fields() -> list[str]:
    return [
        "paymentMethod", "accountType", "track1099", "billingFrequency",
        "toolCall", "department", "owner", "vendorName",
    ]


# ── Handler registry ─────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "vendor_lookup": handle_vendor_lookup,
    "vendor_count": handle_vendor_count,
    "spend_total": handle_spend_total,
    "spend_by_vendor": handle_spend_by_vendor,
    "spend_by_dimension": handle_spend_by_dimension,
    "top_vendors": handle_top_vendors,
}
