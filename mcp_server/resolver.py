"""Vendor resolution pipeline and filter validation.

resolve_vendor() is the SINGLE entry point for mapping a user-supplied
identifier (name, ID, alias, partial match) to a canonical vendor_id.
Every MCP tool that accepts a ``vendor`` parameter calls this function —
no tool implements its own matching logic.

resolve_filter() validates dynamic filter fields (department, owner) against
distinct values currently in Postgres.
"""

from __future__ import annotations

import re
from typing import Any

from service.pg_client import get_pool


# ── Vendor resolution ────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _token_match(query_tokens: list[str], name_lower: str) -> bool:
    """Return True if every query token appears in *name_lower*."""
    return all(t in name_lower for t in query_tokens)


def _load_all_vendors() -> list[dict]:
    """Load all vendors from Postgres. Returns list of dicts with id, name, aliases."""
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id::text, name, aliases FROM vendors ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_vendor(identifier: str) -> dict:
    """Resolve a user-supplied vendor identifier to a canonical vendor.

    Resolution steps (in order):
        1. Exact UUID or slug match
        2. Exact name match (case-insensitive)
        3. Alias match (``aliases`` array field on vendor rows)
        4. Normalised match (strip punctuation/whitespace, compare)
        5. Token / fuzzy match (all query tokens present in vendor name)

    Returns one of::

        {"status": "ok", "vendor_id": "...", "vendor_name": "..."}
        {"status": "ambiguous", "candidates": [{"vendor_id": "...", "vendor_name": "..."}, ...]}
        {"status": "not_found", "message": "..."}
    """
    identifier = identifier.strip()
    if not identifier:
        return {"status": "not_found", "message": "Empty vendor identifier."}

    pool = get_pool()

    # Step 1: exact UUID or source_system_id match
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id::text, name FROM vendors WHERE id::text = %s OR source_system_id = %s LIMIT 1",
            (identifier, identifier),
        ).fetchone()
    if row:
        return _ok(row["id"], row["name"])

    all_vendors = _load_all_vendors()

    # Step 2: exact name match (case-insensitive)
    id_lower = identifier.lower()
    exact = [v for v in all_vendors if (v.get("name") or "").lower() == id_lower]
    if len(exact) == 1:
        return _ok(exact[0]["id"], exact[0]["name"])
    if len(exact) > 1:
        return _ambiguous(exact)

    # Step 3: alias match
    alias_matches = []
    for v in all_vendors:
        aliases = v.get("aliases") or []
        if any(a.lower() == id_lower for a in aliases):
            alias_matches.append(v)
    if len(alias_matches) == 1:
        return _ok(alias_matches[0]["id"], alias_matches[0]["name"])
    if len(alias_matches) > 1:
        return _ambiguous(alias_matches)

    # Step 4: normalised match
    norm_query = _normalise(identifier)
    if norm_query:
        norm_matches = [v for v in all_vendors if _normalise(v.get("name", "")) == norm_query]
        if len(norm_matches) == 1:
            return _ok(norm_matches[0]["id"], norm_matches[0]["name"])
        if len(norm_matches) > 1:
            return _ambiguous(norm_matches)

    # Step 5: token / fuzzy match
    query_tokens = norm_query.split() if norm_query else []
    if query_tokens:
        token_matches = [
            v for v in all_vendors
            if _token_match(query_tokens, (v.get("name") or "").lower())
        ]
        if len(token_matches) == 1:
            return _ok(token_matches[0]["id"], token_matches[0]["name"])
        if len(token_matches) > 1:
            return _ambiguous(token_matches[:10])

    return {
        "status": "not_found",
        "message": f"No vendor matched '{identifier}'.",
    }


def _ok(vendor_id: str, vendor_name: str) -> dict:
    return {"status": "ok", "vendor_id": vendor_id, "vendor_name": vendor_name}


def _ambiguous(vendors: list[dict]) -> dict:
    return {
        "status": "ambiguous",
        "candidates": [
            {"vendor_id": v["id"], "vendor_name": v.get("name", v["id"])}
            for v in vendors
        ],
    }


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
