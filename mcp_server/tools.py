"""Intent-aligned vendor analytics tool handlers — SQL-backed.

Each handler accepts a dict of parameters (from LLM tool call) and an optional
``caller_context`` for spend-level access control.  All handlers return a dict
with a ``status`` field — one of ``ok``, ``ambiguous``, ``not_found``,
``not_authorized``, or ``invalid_filter``.

All aggregation is pushed down to Postgres via SQL queries — no Python-side
streaming or in-memory aggregation.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from service.pg_client import (
    get_pool,
    get_vendor,
    resolve_vendor_by_identifier,
    query_spend_detail as pg_query_spend_detail,
    get_spend_detail_dimensions as pg_get_spend_detail_dimensions,
)
from .resolver import validate_filters, FIELD_TO_SQL
from .period_parser import parse_period, PeriodParseError


# ── Helpers ──────────────────────────────────────────────────────────────

CallerContext = dict[str, Any] | None


def _resolve_or_ambiguous(vendor_input: str) -> dict | None:
    """Resolve a vendor input, returning an ambiguous/not_found response dict
    if resolution fails, or None on success (caller reads result from the
    returned VendorMatch).

    Returns a tuple of (error_response | None, VendorMatch | None).
    """
    from service.pg_client import VendorMatch  # avoid circular at module level
    result = resolve_vendor_by_identifier(vendor_input)
    if not result:
        return {"status": "not_found", "message": f"No vendor matched '{vendor_input}'."}, None
    if result.match == "disambiguate" or (result.match == "fuzzy" and result.alternatives):
        return {
            "status": "ambiguous",
            "candidates": [
                {"vendor_id": result.vendor["id"], "vendor_name": result.vendor.get("name", result.vendor["id"])}
            ] + [
                {"vendor_id": a["id"], "vendor_name": a.get("name", a["id"])}
                for a in result.alternatives
            ],
        }, None
    return None, result

_SPEND_BASE_JOIN = """
    FROM vendor_monthly_spend s
    JOIN vendors v ON v.id = s.vendor_id
    LEFT JOIN departments d ON d.id = v.department_id
    LEFT JOIN users uo ON uo.id = v.owner_id
"""

_VENDOR_BASE_JOIN = """
    FROM vendors v
    LEFT JOIN departments d ON d.id = v.department_id
    LEFT JOIN users uo ON uo.id = v.owner_id
"""


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


def _month_to_date(month_str: str) -> str:
    """Convert 'YYYY-MM' to 'YYYY-MM-01' for SQL date comparison."""
    return f"{month_str}-01"


def _build_where(
    filters: dict,
    start_month: str | None,
    end_month: str | None,
    vendor_id: str | None = None,
    caller_context: CallerContext = None,
) -> tuple[str, list]:
    """Build a WHERE clause from filters, period, vendor, and access control.

    Returns (where_sql, params) — the SQL starts with ' WHERE 1=1'.
    """
    clauses = ["1=1", "v.hidden_from_agent = false"]
    params: list = []

    if start_month:
        clauses.append("s.date >= %s")
        params.append(_month_to_date(start_month))
    if end_month:
        clauses.append("s.date <= %s")
        params.append(_month_to_date(end_month))

    if vendor_id:
        clauses.append("s.vendor_id = %s")
        params.append(vendor_id)

    for field, value in filters.items():
        sql_col = FIELD_TO_SQL.get(field)
        if sql_col:
            clauses.append(f"{sql_col} = %s")
            params.append(value)

    if caller_context and not caller_context.get("is_finance_admin"):
        allowed = caller_context.get("allowed_vendor_ids") or []
        if not allowed:
            clauses.append("FALSE")
        else:
            clauses.append("s.vendor_id = ANY(%s::uuid[])")
            params.append(allowed)

    return " WHERE " + " AND ".join(clauses), params


def _build_vendor_where(
    filters: dict,
    caller_context: CallerContext = None,
) -> tuple[str, list]:
    """Build a WHERE clause for vendor-only queries (no spend table)."""
    clauses = ["1=1", "v.hidden_from_agent = false"]
    params: list = []

    for field, value in filters.items():
        sql_col = FIELD_TO_SQL.get(field)
        if sql_col:
            clauses.append(f"{sql_col} = %s")
            params.append(value)

    return " WHERE " + " AND ".join(clauses), params


# ── Tool handlers ────────────────────────────────────────────────────────

def handle_vendor_lookup(args: dict, caller_context: CallerContext = None) -> dict:
    """Look up a single vendor by name, ID, or alias."""
    vendor_input = args.get("vendor", "")
    if not vendor_input:
        return {"status": "not_found", "message": "No vendor specified."}

    err, result = _resolve_or_ambiguous(vendor_input)
    if err:
        return err

    return {
        "status": "ok",
        "vendor_id": result.vendor["id"],
        "vendor_name": result.vendor.get("name", result.vendor["id"]),
        "match": result.match,
        "data": result.vendor,
    }


def handle_vendor_count(args: dict, caller_context: CallerContext = None) -> dict:
    """Count vendors, optionally filtered and/or grouped by a dimension."""
    filters = dict(args.get("filters") or {})
    group_by = args.get("group_by")

    filter_err = validate_filters(filters)
    if filter_err:
        return filter_err

    where_sql, params = _build_vendor_where(filters, caller_context)
    pool = get_pool()

    if group_by:
        group_col = FIELD_TO_SQL.get(group_by)
        if not group_col:
            return {
                "status": "invalid_filter",
                "field": "group_by",
                "provided": group_by,
                "valid_values": list(FIELD_TO_SQL.keys()),
            }

        with pool.connection() as conn:
            rows = conn.execute(
                f"SELECT COALESCE({group_col}::text, 'Unknown') AS grp, COUNT(*) AS cnt"
                f" {_VENDOR_BASE_JOIN} {where_sql} GROUP BY grp ORDER BY cnt DESC",
                params,
            ).fetchall()

        counts = {r["grp"]: r["cnt"] for r in rows}
        total = sum(counts.values())
        return {"status": "ok", "data": {"counts": counts, "total": total}}

    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt {_VENDOR_BASE_JOIN} {where_sql}",
            params,
        ).fetchone()

    return {"status": "ok", "data": {"count": row["cnt"]}}


_VENDOR_LIST_SELECT = """
    SELECT v.id, v.name, v.account_type, v.track_1099,
           v.payment_method, v.billing_frequency, v.source_system,
           d.name AS department, uo.email AS owner
"""

_VENDOR_LIST_DEFAULT_LIMIT = 50


def handle_vendor_list(args: dict, caller_context: CallerContext = None) -> dict:
    """List vendors matching filter criteria with key fields."""
    filters = dict(args.get("filters") or {})
    limit = args.get("limit", _VENDOR_LIST_DEFAULT_LIMIT)
    if not isinstance(limit, int) or limit < 1:
        limit = _VENDOR_LIST_DEFAULT_LIMIT
    limit = min(limit, 200)

    filter_err = validate_filters(filters)
    if filter_err:
        return filter_err

    where_sql, params = _build_vendor_where(filters, caller_context)
    pool = get_pool()

    with pool.connection() as conn:
        count_row = conn.execute(
            f"SELECT COUNT(*) AS cnt {_VENDOR_BASE_JOIN} {where_sql}",
            params,
        ).fetchone()
        total = count_row["cnt"]

        rows = conn.execute(
            f"{_VENDOR_LIST_SELECT} {_VENDOR_BASE_JOIN} {where_sql}"
            f" ORDER BY v.name LIMIT %s",
            params + [limit],
        ).fetchall()

    vendors = [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "accountType": r["account_type"],
            "track1099": r["track_1099"],
            "paymentMethod": r["payment_method"],
            "billingFrequency": r["billing_frequency"],
            "sourceSystem": r["source_system"],
            "department": r["department"],
            "owner": r["owner"],
        }
        for r in rows
    ]

    _CSV_THRESHOLD = 10
    has_csv = total >= _CSV_THRESHOLD

    if has_csv:
        if total > limit:
            with pool.connection() as conn:
                all_rows = conn.execute(
                    f"{_VENDOR_LIST_SELECT} {_VENDOR_BASE_JOIN} {where_sql}"
                    f" ORDER BY v.name",
                    params,
                ).fetchall()
            all_vendors = [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "accountType": r["account_type"],
                    "track1099": r["track_1099"],
                    "paymentMethod": r["payment_method"],
                    "billingFrequency": r["billing_frequency"],
                    "sourceSystem": r["source_system"],
                    "department": r["department"],
                    "owner": r["owner"],
                }
                for r in all_rows
            ]
        else:
            all_vendors = vendors

    result: dict = {"status": "ok", "data": {"total": total}}

    if has_csv:
        result["data"]["csv_attached"] = True
        result["csv"] = _vendors_to_csv(all_vendors)
        result["csv_filename"] = _vendor_csv_filename(filters)
    else:
        result["data"]["vendors"] = vendors

    return result


_CSV_HEADERS = ["name", "accountType", "track1099", "paymentMethod",
                "billingFrequency", "sourceSystem", "department", "owner"]


def _vendors_to_csv(vendors: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_HEADERS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(vendors)
    return buf.getvalue()


def _vendor_csv_filename(filters: dict) -> str:
    parts = ["vendors"]
    for key in sorted(filters):
        val = str(filters[key]).lower().replace(" ", "-")
        parts.append(f"{key}-{val}")
    return "-".join(parts) + ".csv"


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

    where_sql, params = _build_where(filters, start_month, end_month, caller_context=caller_context)
    pool = get_pool()

    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(s.total_amount), 0) AS total,"
            f" COALESCE(SUM(s.bill_count), 0) AS bills,"
            f" COUNT(DISTINCT s.vendor_id) AS vendors"
            f" {_SPEND_BASE_JOIN} {where_sql}",
            params,
        ).fetchone()

    return {
        "status": "ok",
        "data": {
            "totalAmount": round(float(row["total"]), 2),
            "billCount": int(row["bills"]),
            "vendorCount": int(row["vendors"]),
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
        err, result = _resolve_or_ambiguous(vendor_input)
        if err:
            return err
        vendor_id = result.vendor["id"]
        vendor_name = result.vendor.get("name", result.vendor["id"])

        auth_err = _check_spend_auth(caller_context, vendor_id, vendor_name)
        if auth_err:
            return auth_err

    where_sql, params = _build_where(
        filters, start_month, end_month,
        vendor_id=vendor_id, caller_context=caller_context if not vendor_input else None,
    )
    pool = get_pool()

    if vendor_id:
        with pool.connection() as conn:
            rows = conn.execute(
                f"SELECT TO_CHAR(s.date, 'YYYY-MM') AS month,"
                f" s.total_amount, s.bill_count"
                f" {_SPEND_BASE_JOIN} {where_sql} ORDER BY s.date",
                params,
            ).fetchall()

        months = [
            {"month": r["month"], "totalAmount": float(r["total_amount"]), "billCount": int(r["bill_count"])}
            for r in rows
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

    # All vendors — ranked by total
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT v.id::text AS vendor_id, v.name AS vendor_name,"
            f" SUM(s.total_amount) AS total, SUM(s.bill_count) AS bills"
            f" {_SPEND_BASE_JOIN} {where_sql}"
            f" GROUP BY v.id, v.name ORDER BY total DESC LIMIT 50",
            params,
        ).fetchall()

    results = [
        {"vendor_id": r["vendor_id"], "vendor_name": r["vendor_name"],
         "totalAmount": round(float(r["total"]), 2), "billCount": int(r["bills"])}
        for r in rows
    ]
    grand_total = round(sum(v["totalAmount"] for v in results), 2)

    return {
        "status": "ok",
        "data": {
            "vendors": results,
            "totalVendors": len(results),
            "grandTotal": grand_total,
            "period": {"start": start_month, "end": end_month},
        },
    }


def handle_spend_by_dimension(args: dict, caller_context: CallerContext = None) -> dict:
    """Spend grouped by a single dimension (e.g. paymentMethod, department)."""
    dimension = args.get("dimension")
    if not dimension:
        return {"status": "invalid_filter", "field": "dimension", "provided": None, "valid_values": list(FIELD_TO_SQL.keys())}

    dim_col = FIELD_TO_SQL.get(dimension)
    if not dim_col:
        return {"status": "invalid_filter", "field": "dimension", "provided": dimension, "valid_values": list(FIELD_TO_SQL.keys())}

    period = args.get("period")
    filters = dict(args.get("filters") or {})

    parsed = _period_or_error(period)
    if isinstance(parsed, dict):
        return parsed
    start_month, end_month = parsed

    filter_err = validate_filters(filters)
    if filter_err:
        return filter_err

    where_sql, params = _build_where(filters, start_month, end_month, caller_context=caller_context)
    pool = get_pool()

    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT COALESCE({dim_col}::text, 'Unknown') AS grp,"
            f" SUM(s.total_amount) AS total, SUM(s.bill_count) AS bills,"
            f" COUNT(DISTINCT s.vendor_id) AS vendors"
            f" {_SPEND_BASE_JOIN} {where_sql}"
            f" GROUP BY grp ORDER BY total DESC",
            params,
        ).fetchall()

    groups = {
        r["grp"]: {
            "totalAmount": round(float(r["total"]), 2),
            "billCount": int(r["bills"]),
            "vendorCount": int(r["vendors"]),
        }
        for r in rows
    }
    grand_total = round(sum(g["totalAmount"] for g in groups.values()), 2)

    return {
        "status": "ok",
        "data": {
            "dimension": dimension,
            "groups": groups,
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

    where_sql, params = _build_where(filters, start_month, end_month, caller_context=caller_context)
    pool = get_pool()

    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT v.id::text AS vendor_id, v.name AS vendor_name,"
            f" SUM(s.total_amount) AS total, SUM(s.bill_count) AS bills"
            f" {_SPEND_BASE_JOIN} {where_sql}"
            f" GROUP BY v.id, v.name ORDER BY total DESC LIMIT %s",
            params + [n],
        ).fetchall()

    ranked = [
        {"vendor_id": r["vendor_id"], "vendor_name": r["vendor_name"],
         "totalAmount": round(float(r["total"]), 2), "billCount": int(r["bills"])}
        for r in rows
    ]
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


# ── Spend detail handlers ────────────────────────────────────────────────

def handle_spend_detail(args: dict, caller_context: CallerContext = None) -> dict:
    """Granular spend detail for a single vendor, with optional filters and grouping."""
    vendor_input = args.get("vendor")
    period = args.get("period")
    group_by = args.get("group_by")
    category = args.get("category")
    project = args.get("project")

    if not vendor_input:
        return {"status": "invalid_filter", "field": "vendor", "provided": None, "valid_values": []}

    err, match = _resolve_or_ambiguous(vendor_input)
    if err:
        return err

    vendor = match.vendor
    vendor_id = vendor["id"]

    auth_err = _check_spend_auth(caller_context, vendor_id=vendor_id, vendor_name=vendor.get("name"))
    if auth_err:
        return auth_err

    parsed = _period_or_error(period)
    if isinstance(parsed, dict):
        return parsed
    start_month, end_month = parsed

    rows = pg_query_spend_detail(
        vendor_id=vendor_id,
        start_month=start_month,
        end_month=end_month,
        category=category,
        project=project,
        group_by=group_by,
    )

    return {
        "status": "ok",
        "data": {
            "vendor": vendor.get("name", vendor_id),
            "vendor_id": vendor_id,
            "period": {"start": start_month, "end": end_month},
            "group_by": group_by,
            "rows": rows,
            "row_count": len(rows),
        },
    }


def handle_spend_detail_dimensions(args: dict, caller_context: CallerContext = None) -> dict:
    """Discover available dimension values for a vendor's spend detail."""
    vendor_input = args.get("vendor")
    dimension = args.get("dimension")

    if not vendor_input:
        return {"status": "invalid_filter", "field": "vendor", "provided": None, "valid_values": []}

    err, match = _resolve_or_ambiguous(vendor_input)
    if err:
        return err

    vendor = match.vendor
    vendor_id = vendor["id"]

    auth_err = _check_spend_auth(caller_context, vendor_id=vendor_id, vendor_name=vendor.get("name"))
    if auth_err:
        return auth_err

    dimensions = pg_get_spend_detail_dimensions(vendor_id=vendor_id, dimension=dimension)

    return {
        "status": "ok",
        "data": {
            "vendor": vendor.get("name", vendor_id),
            "vendor_id": vendor_id,
            "dimensions": dimensions,
        },
    }


# ── Handler registry ─────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "vendor_lookup": handle_vendor_lookup,
    "vendor_count": handle_vendor_count,
    "vendor_list": handle_vendor_list,
    "spend_total": handle_spend_total,
    "spend_by_vendor": handle_spend_by_vendor,
    "spend_by_dimension": handle_spend_by_dimension,
    "top_vendors": handle_top_vendors,
    "spend_detail": handle_spend_detail,
    "spend_detail_dimensions": handle_spend_detail_dimensions,
}
