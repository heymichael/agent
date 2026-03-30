"""Filter validation for MCP tool handlers.

Vendor resolution has been consolidated into
``service.pg_client.resolve_vendor_by_identifier``.

resolve_filter() validates dynamic filter fields (department, owner) against
distinct values currently in Postgres.
"""

from __future__ import annotations

from typing import Any

from service.pg_client import get_pool


# ── Filter validation ────────────────────────────────────────────────────

ENUM_FIELDS: dict[str, list[Any]] = {
    "paymentMethod": ["Check", "ACH", "CreditCard", "Wire", "PayPal"],
    "accountType": ["Business", "Individual"],
    "track1099": [True, False],
    "billingFrequency": ["monthly", "annual", "usage-based"],
    "sourceSystem": ["billcom", "aws-ce", "manual"],
}

RESOLVE_FIELDS = {"department", "owner"}

# Maps camelCase filter/dimension names to SQL column expressions
FIELD_TO_SQL = {
    "paymentMethod": "v.payment_method",
    "accountType": "v.account_type",
    "track1099": "v.track_1099",
    "billingFrequency": "v.billing_frequency",
    "sourceSystem": "v.source_system",
    "department": "d.name",
    "owner": "uo.email",
    "vendorName": "v.name",
}


def resolve_filter(field: str, value: Any) -> dict:
    """Validate a single filter field+value.

    For enum fields, checks against the hardcoded valid set.
    For resolve fields (department, owner), checks against distinct values
    currently in Postgres.

    Returns one of::

        {"status": "ok", "field": "...", "value": <canonical>}
        {"status": "invalid_filter", "field": "...", "provided": "...", "valid_values": [...]}
    """
    if field in ENUM_FIELDS:
        valid = ENUM_FIELDS[field]
        if value in valid:
            return {"status": "ok", "field": field, "value": value}
        if isinstance(value, str):
            for v in valid:
                if isinstance(v, str) and v.lower() == value.lower():
                    return {"status": "ok", "field": field, "value": v}
        return {
            "status": "invalid_filter",
            "field": field,
            "provided": value,
            "valid_values": valid,
        }

    if field in RESOLVE_FIELDS:
        return _resolve_dynamic_field(field, value)

    return {
        "status": "invalid_filter",
        "field": field,
        "provided": value,
        "valid_values": [],
        "message": f"Unknown filter field: '{field}'. "
                   f"Valid fields: {sorted(list(ENUM_FIELDS.keys()) + list(RESOLVE_FIELDS))}",
    }


def _resolve_dynamic_field(field: str, value: Any) -> dict:
    """Validate a dynamic field value against distinct values in Postgres."""
    pool = get_pool()
    sql_col = FIELD_TO_SQL.get(field)
    if not sql_col:
        return {"status": "invalid_filter", "field": field, "provided": value, "valid_values": []}

    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {sql_col} AS val FROM vendors v "
            "LEFT JOIN departments d ON d.id = v.department_id "
            "LEFT JOIN users uo ON uo.id = v.owner_id "
            f"WHERE {sql_col} IS NOT NULL"
        ).fetchall()
    distinct = {str(r["val"]) for r in rows}

    value_str = str(value)

    if value_str in distinct:
        return {"status": "ok", "field": field, "value": value_str}

    for dv in distinct:
        if dv.lower() == value_str.lower():
            return {"status": "ok", "field": field, "value": dv}

    return {
        "status": "invalid_filter",
        "field": field,
        "provided": value,
        "valid_values": sorted(distinct),
    }


def validate_filters(filters: dict | None) -> dict | None:
    """Validate all filters in a dict. Returns None if all valid, or the
    first invalid_filter response."""
    if not filters:
        return None
    for field, value in filters.items():
        result = resolve_filter(field, value)
        if result["status"] != "ok":
            return result
        filters[field] = result["value"]
    return None
