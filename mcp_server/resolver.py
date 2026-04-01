"""Filter validation for MCP tool handlers.

Vendor resolution has been consolidated into
``service.pg_client.resolve_vendor_by_identifier``.

resolve_filter() validates dynamic filter fields (department, owner) against
distinct values currently in Postgres, with fuzzy matching via
``service.resolve.resolve_canonical_value``.
"""

from __future__ import annotations

from typing import Any

from service.pg_client import get_pool
from service.resolve import resolve_canonical_value


# ── Filter validation ────────────────────────────────────────────────────

ENUM_FIELDS: dict[str, list[Any]] = {
    "paymentMethod": ["Check", "ACH", "CreditCard", "Wire", "PayPal"],
    "accountType": ["Business", "Individual"],
    "track1099": [True, False],
    "billingFrequency": ["monthly", "annual", "usage-based"],
    "sourceSystem": ["billcom", "aws-ce", "manual"],
    "autoRenew": [True, False],
}

RESOLVE_FIELDS = {"department", "owner", "secondaryOwner",
                  "purpose", "spendType", "renewalRate", "terminationTerms"}

OWNER_FIELDS = {"owner", "secondaryOwner"}

RANGE_FIELDS: dict[str, str] = {
    "contractStart": "v.contract_start",
    "contractEnd": "v.contract_end",
    "contractMonths": "v.contract_months",
    "renewalNotice": "v.renewal_notice",
}

# Maps camelCase filter/dimension names to SQL column expressions
FIELD_TO_SQL = {
    "paymentMethod": "v.payment_method",
    "accountType": "v.account_type",
    "track1099": "v.track_1099",
    "billingFrequency": "v.billing_frequency",
    "sourceSystem": "v.source_system",
    "department": "d.name",
    "owner": "uo.email",
    "secondaryOwner": "uso.email",
    "vendorName": "v.name",
    "autoRenew": "v.auto_renew",
    "purpose": "v.purpose",
    "spendType": "v.spend_type",
    "renewalRate": "v.renewal_rate",
    "terminationTerms": "v.termination_terms",
    "contractMonths": "v.contract_months",
    "renewalNotice": "v.renewal_notice",
    "contractStart": "v.contract_start",
    "contractEnd": "v.contract_end",
}


def _resolve_with_candidates(field: str, value: Any, candidates: list[str]) -> dict:
    """Resolve a value against a list of canonical candidates using fuzzy matching."""
    result = resolve_canonical_value(str(value), candidates)
    if not result:
        return {
            "status": "invalid_filter",
            "field": field,
            "provided": value,
            "valid_values": sorted(candidates),
        }
    if result.match in ("exact", "close"):
        return {"status": "ok", "field": field, "value": result.value}
    return {
        "status": "did_you_mean",
        "field": field,
        "provided": value,
        "suggestion": result.value,
        "alternatives": result.alternatives,
        "valid_values": sorted(candidates),
    }


def resolve_filter(field: str, value: Any) -> dict:
    """Validate a single filter field+value.

    For enum fields, checks against the hardcoded valid set.
    For resolve fields (department, owner), checks against distinct values
    currently in Postgres.
    For range fields (dates, integers), validates the range dict structure.

    Returns one of::

        {"status": "ok", "field": "...", "value": <canonical>}
        {"status": "did_you_mean", "field": "...", "suggestion": "...", ...}
        {"status": "invalid_filter", "field": "...", "provided": "...", "valid_values": [...]}
    """
    if field in ENUM_FIELDS:
        valid = ENUM_FIELDS[field]
        if value in valid:
            return {"status": "ok", "field": field, "value": value}
        str_candidates = [v for v in valid if isinstance(v, str)]
        if isinstance(value, str) and str_candidates:
            return _resolve_with_candidates(field, value, str_candidates)
        return {
            "status": "invalid_filter",
            "field": field,
            "provided": value,
            "valid_values": valid,
        }

    if field in RESOLVE_FIELDS:
        return _resolve_dynamic_field(field, value)

    if field in RANGE_FIELDS:
        return _validate_range_filter(field, value)

    all_fields = sorted(
        list(ENUM_FIELDS.keys()) + list(RESOLVE_FIELDS) + list(RANGE_FIELDS.keys())
    )
    return {
        "status": "invalid_filter",
        "field": field,
        "provided": value,
        "valid_values": [],
        "message": f"Unknown filter field: '{field}'. Valid fields: {all_fields}",
    }


def _validate_range_filter(field: str, value: Any) -> dict:
    """Validate a range filter value. Accepts a scalar or {"from"/"to"} /
    {"min"/"max"} dict for range queries."""
    if isinstance(value, dict):
        range_keys = {"from", "to", "min", "max"}
        if not set(value.keys()) & range_keys:
            return {
                "status": "invalid_filter",
                "field": field,
                "provided": value,
                "valid_values": [],
                "message": f"Range filter must use keys: from/to (dates) or min/max (numbers).",
            }
        return {"status": "ok", "field": field, "value": value}
    return {"status": "ok", "field": field, "value": value}


def _resolve_dynamic_field(field: str, value: Any) -> dict:
    """Validate a dynamic field value against distinct values in Postgres.

    For owner/secondaryOwner fields, resolves against user full names and
    returns matching user IDs so the filter builder can use an IN clause.
    """
    pool = get_pool()

    if field in OWNER_FIELDS:
        return _resolve_owner_field(field, value, pool)

    sql_col = FIELD_TO_SQL.get(field)
    if not sql_col:
        return {"status": "invalid_filter", "field": field, "provided": value, "valid_values": []}

    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {sql_col} AS val FROM vendors v "
            "LEFT JOIN departments d ON d.id = v.department_id "
            "LEFT JOIN users uo ON uo.id = v.owner_id "
            "LEFT JOIN users uso ON uso.id = v.secondary_owner_id "
            f"WHERE {sql_col} IS NOT NULL"
        ).fetchall()
    candidates = [str(r["val"]) for r in rows]

    return _resolve_with_candidates(field, value, candidates)


def _resolve_owner_field(field: str, value: Any, pool) -> dict:
    """Resolve an owner/secondaryOwner filter against user full names."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT CONCAT(first_name, ' ', last_name) AS val "
            "FROM users WHERE first_name IS NOT NULL"
        ).fetchall()
    candidates = [r["val"] for r in rows if r["val"] and r["val"].strip()]

    result = resolve_canonical_value(str(value), candidates)
    if not result:
        return {
            "status": "invalid_filter",
            "field": field,
            "provided": value,
            "valid_values": sorted(candidates),
        }
    if result.match not in ("exact", "close"):
        return {
            "status": "did_you_mean",
            "field": field,
            "provided": value,
            "suggestion": result.value,
            "alternatives": result.alternatives,
            "valid_values": sorted(candidates),
        }

    resolved_name = result.value
    with pool.connection() as conn:
        id_rows = conn.execute(
            "SELECT id FROM users "
            "WHERE CONCAT(first_name, ' ', last_name) = %s",
            [resolved_name],
        ).fetchall()
    user_ids = [str(r["id"]) for r in id_rows]

    return {"status": "ok", "field": field, "value": resolved_name, "user_ids": user_ids}


def validate_filters(filters: dict | None) -> dict | None:
    """Validate all filters in a dict. Returns None if all valid, or the
    first invalid_filter response.

    For owner/secondaryOwner fields, stashes resolved user IDs into
    ``filters["_owner_ids"]`` / ``filters["_secondary_owner_ids"]``.
    """
    if not filters:
        return None
    for field in list(filters.keys()):
        if field.startswith("_"):
            continue
        value = filters[field]
        result = resolve_filter(field, value)
        if result["status"] != "ok":
            return result
        filters[field] = result["value"]
        if field == "owner" and "user_ids" in result:
            filters["_owner_ids"] = result["user_ids"]
        elif field == "secondaryOwner" and "user_ids" in result:
            filters["_secondary_owner_ids"] = result["user_ids"]
    return None
