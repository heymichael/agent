"""Postgres database client for all data access.

Postgres database client. Uses psycopg3 with a connection pool.
All functions preserve the return shapes expected by app.py, service/tools.py,
and mcp_server/tools.py so callers require only an import swap.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        conninfo = os.environ["DATABASE_URL"]
        _pool = ConnectionPool(
            conninfo,
            min_size=2,
            max_size=10,
            max_idle=900,
            reconnect_timeout=60,
            check=ConnectionPool.check_connection,
            kwargs={"row_factory": dict_row},
        )
    return _pool


def warm_connection_pool() -> None:
    """Block until the shared Postgres pool has established its warm connections."""
    get_pool().wait()


def close_pool() -> None:
    """Close the shared Postgres pool during application shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------

_VENDOR_COLUMNS = (
    "id", "source_system", "source_system_id", "name", "department_id",
    "owner_id", "secondary_owner_id", "payment_method", "billing_frequency",
    "account_type", "track_1099", "purpose", "spend_type", "aliases",
    "contract_start", "contract_end", "contract_months", "auto_renew",
    "renewal_rate", "renewal_notice", "termination_terms",
    "created_at", "modified_at", "synced_at",
)

VENDOR_EDITABLE_COLUMNS = frozenset({
    "name", "department_id", "owner_id", "secondary_owner_id",
    "payment_method", "billing_frequency", "account_type", "track_1099",
    "purpose", "spend_type", "aliases", "contract_start", "contract_end",
    "contract_months", "auto_renew", "renewal_rate", "renewal_notice",
    "termination_terms",
})


def _vendor_row_to_dict(row: dict) -> dict:
    """Map a Postgres vendor row to the API response shape."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "sourceSystem": row["source_system"],
        "sourceSystemId": row["source_system_id"],
        "department": row.get("department_name"),
        "departmentId": str(row["department_id"]) if row.get("department_id") else None,
        "owner": row.get("owner_email"),
        "ownerId": str(row["owner_id"]) if row.get("owner_id") else None,
        "secondaryOwner": row.get("secondary_owner_email"),
        "secondaryOwnerId": str(row["secondary_owner_id"]) if row.get("secondary_owner_id") else None,
        "paymentMethod": row.get("payment_method"),
        "billingFrequency": row.get("billing_frequency"),
        "accountType": row.get("account_type"),
        "track1099": row.get("track_1099", False),
        "purpose": row.get("purpose"),
        "spendType": row.get("spend_type"),
        "aliases": row.get("aliases") or [],
        "contractStartDate": _date_str(row.get("contract_start")),
        "contractEndDate": _date_str(row.get("contract_end")),
        "contractLengthMonths": row.get("contract_months"),
        "autoRenew": row.get("auto_renew"),
        "renewalRate": row.get("renewal_rate"),
        "renewalNoticeDays": row.get("renewal_notice"),
        "terminationTerms": row.get("termination_terms"),
        "isContractor": row.get("is_contractor", False),
        "lastSyncedAt": _ts_str(row.get("synced_at")),
    }


def _date_str(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _ts_str(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


_VENDOR_LIST_SQL = """
    SELECT v.*,
           d.name AS department_name,
           uo.email AS owner_email,
           us.email AS secondary_owner_email
    FROM vendors v
    LEFT JOIN departments d ON d.id = v.department_id
    LEFT JOIN users uo ON uo.id = v.owner_id
    LEFT JOIN users us ON us.id = v.secondary_owner_id
"""

# Phase 3 of multi-org tenancy (task 254). Every vendor query helper takes
# `org_slug` as a required positional argument. Defaults are intentionally
# omitted so a missed call site fails at type-check / import time rather
# than silently returning cross-tenant rows. See strategy 197-r2 risk
# section ("Missing an `org_slug` filter on a query").


def list_vendors(org_slug: str) -> list[dict]:
    """Return all vendors in the given org with joined department/owner names."""
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            f"{_VENDOR_LIST_SQL} WHERE v.org_slug = %s ORDER BY LOWER(v.name)",
            (org_slug,),
        ).fetchall()
    return [_vendor_row_to_dict(r) for r in rows]


def get_vendor(vendor_id: str, org_slug: str) -> dict | None:
    """Fetch a vendor by UUID id, scoped to the active org."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            f"{_VENDOR_LIST_SQL} WHERE v.id = %s AND v.org_slug = %s",
            (vendor_id, org_slug),
        ).fetchone()
    return _vendor_row_to_dict(row) if row else None


def get_vendor_by_source(source_system: str, source_system_id: str, org_slug: str) -> dict | None:
    """Fetch a vendor by its upstream identity, scoped to the active org."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            f"{_VENDOR_LIST_SQL} WHERE v.source_system = %s AND v.source_system_id = %s AND v.org_slug = %s",
            (source_system, source_system_id, org_slug),
        ).fetchone()
    return _vendor_row_to_dict(row) if row else None


def find_vendor_by_name(name: str, org_slug: str, include_hidden: bool = False) -> dict | None:
    """Find a vendor by exact name (case-insensitive), scoped to the active org."""
    hidden_clause = "" if include_hidden else " AND v.hidden_from_agent = false"
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            f"{_VENDOR_LIST_SQL} WHERE LOWER(v.name) = LOWER(%s) AND v.org_slug = %s{hidden_clause} LIMIT 1",
            (name, org_slug),
        ).fetchone()
    return _vendor_row_to_dict(row) if row else None


FUZZY_AUTO_ACCEPT = 0.6
FUZZY_SUGGEST = 0.25
SIMILAR_THRESHOLD = 0.4


def find_vendors_similar(name: str, org_slug: str,
                         threshold: float = FUZZY_SUGGEST,
                         limit: int = 5,
                         include_hidden: bool = False) -> list[tuple[dict, float]]:
    """Find vendors by trigram similarity, scoped to the active org.

    Returns a list of (vendor_dict, similarity_score) tuples sorted by
    descending score, filtered to scores above *threshold*. Requires
    pg_trgm.
    """
    hidden_clause = "" if include_hidden else " AND sub.hidden_from_agent = false"
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT sub.*, similarity(LOWER(sub.name), LOWER(%s)) AS sim_score
                FROM ({_VENDOR_LIST_SQL}) sub
                WHERE similarity(LOWER(sub.name), LOWER(%s)) > %s
                  AND sub.org_slug = %s{hidden_clause}
                ORDER BY sim_score DESC
                LIMIT %s""",
            (name, name, threshold, org_slug, limit),
        ).fetchall()
    results = []
    for row in rows:
        score = float(row.pop("sim_score"))
        results.append((_vendor_row_to_dict(row), score))
    return results


def add_vendor(data: dict, org_slug: str) -> dict:
    """Create a new vendor in the active org. Returns the created record."""
    pool = get_pool()
    now = _now()
    with pool.connection() as conn:
        row = conn.execute(
            """INSERT INTO vendors (name, source_system, source_system_id, org_slug, created_at, modified_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING *""",
            (
                data["name"],
                data.get("source_system", "manual"),
                data.get("source_system_id", _slugify(data["name"])),
                org_slug,
                now,
                now,
            ),
        ).fetchone()
    return get_vendor(str(row["id"]), org_slug)


def update_vendor(vendor_id: str, updates: dict, org_slug: str) -> dict:
    """Partial-update a vendor scoped to the active org. Returns the full record after update."""
    pool = get_pool()
    fields = {k: v for k, v in updates.items() if k in VENDOR_EDITABLE_COLUMNS}
    if not fields:
        result = get_vendor(vendor_id, org_slug)
        if not result:
            raise ValueError(f"Vendor '{vendor_id}' not found")
        return result

    fields["modified_at"] = _now()
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [vendor_id, org_slug]

    with pool.connection() as conn:
        cur = conn.execute(
            f"UPDATE vendors SET {set_clause} WHERE id = %s AND org_slug = %s RETURNING id",
            values,
        )
        if cur.fetchone() is None:
            raise ValueError(f"Vendor '{vendor_id}' not found")

    return get_vendor(vendor_id, org_slug)


def delete_vendor(vendor_id: str, org_slug: str) -> bool:
    """Delete a vendor scoped to the active org. Returns True if deleted."""
    pool = get_pool()
    with pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM vendors WHERE id = %s AND org_slug = %s RETURNING id",
            (vendor_id, org_slug),
        )
        return cur.fetchone() is not None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _normalise(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_all_vendors_light(org_slug: str, include_hidden: bool = False) -> list[dict]:
    """Load id, name, aliases for all vendors in an org (used by alias/normalized matching)."""
    hidden_clause = "" if include_hidden else " AND hidden_from_agent = false"
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT id::text, name, aliases FROM vendors WHERE org_slug = %s{hidden_clause} ORDER BY name",
            (org_slug,),
        ).fetchall()
    return [dict(r) for r in rows]


class VendorMatch:
    """Result of resolve_vendor_by_identifier."""
    __slots__ = ("vendor", "match", "alternatives")

    def __init__(self, vendor: dict, match: str,
                 alternatives: list[dict] | None = None):
        self.vendor = vendor
        self.match = match
        self.alternatives = alternatives or []


def resolve_vendor_by_identifier(identifier: str, org_slug: str) -> VendorMatch | None:
    """Resolve a vendor by UUID, name, alias, normalized name, or fuzzy match within an org.

    Two passes:
      1. Find the best match through a cascade of strategies.
      2. Check for similar alternatives that could cause ambiguity.

    Resolution cascade:
      1. Exact UUID
      2. Exact name (case-insensitive)
      3. Alias match (aliases array on vendor rows)
      4. Normalized match (strip punctuation/whitespace, compare)
      5. pg_trgm fuzzy (two-tier: close >= 0.6, fuzzy >= 0.25)

    Returns a VendorMatch with:
      vendor       — best-matching vendor dict
      match        — "exact", "close", "fuzzy", or "disambiguate"
      alternatives — other similar vendors the user might have meant
    """
    identifier = identifier.strip()
    if not identifier:
        return None

    # --- Pass 1: find best match ---

    # Step 1: UUID
    if _is_uuid(identifier):
        vendor = get_vendor(identifier, org_slug)
        if vendor:
            return VendorMatch(vendor, "exact")

    # Step 2: exact name
    vendor = find_vendor_by_name(identifier, org_slug)
    if vendor:
        match_type = "exact"
    else:
        id_lower = identifier.lower()
        norm_query = _normalise(identifier)
        all_vendors = _load_all_vendors_light(org_slug)

        # Step 3: alias match
        alias_matches = [
            v for v in all_vendors
            if any(a.lower() == id_lower for a in (v.get("aliases") or []))
        ]
        if len(alias_matches) == 1:
            vendor = get_vendor(alias_matches[0]["id"], org_slug)
            match_type = "close"
        elif len(alias_matches) > 1:
            vendor = get_vendor(alias_matches[0]["id"], org_slug)
            match_type = "disambiguate"
            alternatives = [get_vendor(v["id"], org_slug) for v in alias_matches[1:]]
            return VendorMatch(vendor, match_type, [a for a in alternatives if a])

        # Step 4: normalized match
        if not vendor and norm_query:
            norm_matches = [
                v for v in all_vendors
                if _normalise(v.get("name", "")) == norm_query
            ]
            if len(norm_matches) == 1:
                vendor = get_vendor(norm_matches[0]["id"], org_slug)
                match_type = "exact"
            elif len(norm_matches) > 1:
                vendor = get_vendor(norm_matches[0]["id"], org_slug)
                match_type = "disambiguate"
                alternatives = [get_vendor(v["id"], org_slug) for v in norm_matches[1:]]
                return VendorMatch(vendor, match_type, [a for a in alternatives if a])

        # Step 5: pg_trgm fuzzy
        if not vendor:
            similar = find_vendors_similar(identifier, org_slug)
            if not similar:
                return None
            vendor, score = similar[0]
            match_type = "close" if score >= FUZZY_AUTO_ACCEPT else "fuzzy"

    # --- Pass 2: check for ambiguity (skip for exact name matches) ---
    if match_type == "exact":
        return VendorMatch(vendor, match_type)

    similar = find_vendors_similar(identifier, org_slug, threshold=SIMILAR_THRESHOLD)
    alternatives = [v for v, _ in similar if v["id"] != vendor["id"]]
    if alternatives:
        match_type = "disambiguate"

    return VendorMatch(vendor, match_type, alternatives)


def _slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ---------------------------------------------------------------------------
# Vendor spend
# ---------------------------------------------------------------------------

def query_spend_by_vendor_ids(
    vendor_ids: list[str],
    start_month: str,
    end_month: str,
    org_slug: str,
) -> list[dict]:
    """Query spend for specific vendors within a month range, scoped to the active org.

    Accepts YYYY-MM strings for start/end and converts to DATE internally.
    Returns rows shaped for the frontend: {vendor, vendorId, month, amount}.

    Org scoping is enforced via the JOIN on vendors.org_slug — even if the
    caller hands in a cross-org vendor_id, the row is dropped because the
    join predicate fails. Defense in depth on top of the
    `_resolve_caller_access` filter that already org-scopes the input list.
    """
    pool = get_pool()
    start_date = _month_to_date(start_month)
    end_date = _month_to_date(end_month)

    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT v.name AS vendor_name, v.id AS vendor_id,
                      TO_CHAR(s.date, 'YYYY-MM') AS month,
                      s.total_amount
               FROM vendor_monthly_spend s
               INNER JOIN vendors v ON v.id = s.vendor_id AND v.org_slug = %s
               WHERE s.vendor_id = ANY(%s::uuid[])
                 AND s.date >= %s AND s.date <= %s
               ORDER BY s.date, v.name""",
            (org_slug, vendor_ids, start_date, end_date),
        ).fetchall()

    return [
        {
            "vendor": r["vendor_name"],
            "vendorId": str(r["vendor_id"]),
            "month": r["month"],
            "amount": float(r["total_amount"]),
        }
        for r in rows
    ]


def get_vendor_spend(vendor_id: str, org_slug: str, months: int = 6) -> list[dict]:
    """Return recent monthly spend for a single vendor, scoped to the active org."""
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT TO_CHAR(s.date, 'YYYY-MM') AS month,
                      s.total_amount AS "totalAmount",
                      s.bill_count AS "billCount"
               FROM vendor_monthly_spend s
               INNER JOIN vendors v ON v.id = s.vendor_id AND v.org_slug = %s
               WHERE s.vendor_id = %s
               ORDER BY s.date DESC
               LIMIT %s""",
            (org_slug, vendor_id, months),
        ).fetchall()
    return [dict(r) for r in rows]


def _month_to_date(month_str: str) -> date:
    """Convert 'YYYY-MM' to a date on the first of that month."""
    parts = month_str.split("-")
    return date(int(parts[0]), int(parts[1]), 1)


# ---------------------------------------------------------------------------
# Spend detail
# ---------------------------------------------------------------------------

def query_spend_detail(
    vendor_id: str,
    start_month: str,
    end_month: str,
    org_slug: str,
    category: str | None = None,
    subcategory: str | None = None,
    project: str | None = None,
    group_by: str | None = None,
    secondary_group_by: str | None = None,
) -> list[dict]:
    """Query vendor_spend_detail with optional filters and grouping, scoped to the active org.

    When group_by is set (one of 'category', 'subcategory', 'project'),
    returns rows grouped and summed by that dimension with a month column.

    When secondary_group_by is also set, returns a 2D cross-tab: rows have
    both dimension_value (group_by) and secondary_value (secondary_group_by)
    with amounts summed across the entire period (no month column).

    Org scoping is enforced via INNER JOIN vendors on org_slug; a
    cross-org vendor_id silently returns no rows.
    """
    pool = get_pool()
    start_date = _month_to_date(start_month)
    end_date = _month_to_date(end_month)

    params: list[Any] = [org_slug, vendor_id, start_date, end_date]
    filters = ""

    if category:
        filters += " AND d.category = %s"
        params.append(category)
    if subcategory:
        filters += " AND d.subcategory = %s"
        params.append(subcategory)
    if project:
        filters += " AND d.project = %s"
        params.append(project)

    valid_group_by = {"category", "subcategory", "project"}

    if (group_by and group_by in valid_group_by
            and secondary_group_by and secondary_group_by in valid_group_by
            and group_by != secondary_group_by):
        sql = f"""
            SELECT d.{group_by} AS dimension_value,
                   d.{secondary_group_by} AS secondary_value,
                   SUM(d.amount) AS amount
            FROM vendor_spend_detail d
            INNER JOIN vendors v ON v.id = d.vendor_id AND v.org_slug = %s
            WHERE d.vendor_id = %s AND d.date >= %s AND d.date <= %s
            {filters}
            GROUP BY d.{group_by}, d.{secondary_group_by}
            ORDER BY amount DESC
        """
    elif group_by and group_by in valid_group_by:
        sql = f"""
            SELECT d.{group_by} AS dimension_value,
                   TO_CHAR(d.date, 'YYYY-MM') AS month,
                   SUM(d.amount) AS amount
            FROM vendor_spend_detail d
            INNER JOIN vendors v ON v.id = d.vendor_id AND v.org_slug = %s
            WHERE d.vendor_id = %s AND d.date >= %s AND d.date <= %s
            {filters}
            GROUP BY d.{group_by}, d.date
            ORDER BY d.date, amount DESC
        """
    else:
        sql = f"""
            SELECT TO_CHAR(d.date, 'YYYY-MM') AS month,
                   SUM(d.amount) AS amount
            FROM vendor_spend_detail d
            INNER JOIN vendors v ON v.id = d.vendor_id AND v.org_slug = %s
            WHERE d.vendor_id = %s AND d.date >= %s AND d.date <= %s
            {filters}
            GROUP BY d.date
            ORDER BY d.date
        """

    with pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    result = []
    for r in rows:
        row = dict(r)
        if "amount" in row:
            row["amount"] = float(row["amount"])
        if "metadata" in row and row["metadata"] is not None:
            pass  # already a dict from psycopg jsonb handling
        result.append(row)
    return result


def get_spend_detail_dimensions(
    vendor_id: str,
    org_slug: str,
    dimension: str | None = None,
) -> dict:
    """Return distinct dimension values for a vendor's detail rows, scoped to the active org.

    If dimension is specified ('category', 'subcategory', or 'project'),
    returns only that dimension's values. Otherwise returns all three.
    """
    pool = get_pool()
    valid = {"category", "subcategory", "project"}
    dims_to_query = [dimension] if dimension and dimension in valid else sorted(valid)

    result: dict[str, list[str]] = {}

    with pool.connection() as conn:
        for dim in dims_to_query:
            rows = conn.execute(
                f"""SELECT DISTINCT d.{dim} AS val
                    FROM vendor_spend_detail d
                    INNER JOIN vendors v ON v.id = d.vendor_id AND v.org_slug = %s
                    WHERE d.vendor_id = %s AND d.{dim} IS NOT NULL
                    ORDER BY val""",
                (org_slug, vendor_id),
            ).fetchall()
            key = f"{dim}s" if not dim.endswith("y") else f"{dim[:-1]}ies"
            result[key] = [r["val"] for r in rows]

    return result


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _user_summary(row: dict, roles: list[str] | None = None,
                  allowed_departments: list[str] | None = None,
                  allowed_vendor_ids: list[str] | None = None,
                  denied_vendor_ids: list[str] | None = None) -> dict:
    """Build a user summary dict matching the existing API response shape."""
    full = " ".join(filter(None, [row.get("first_name", ""), row.get("last_name", "")])).strip()
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "firstName": row.get("first_name", ""),
        "lastName": row.get("last_name", ""),
        "fullName": full or row["email"],
        "roles": roles if roles is not None else [],
        "allowedDepartments": allowed_departments if allowed_departments is not None else [],
        "allowedVendorIds": allowed_vendor_ids if allowed_vendor_ids is not None else [],
        "deniedVendorIds": denied_vendor_ids if denied_vendor_ids is not None else [],
    }


_USER_CONTEXT_SQL = "SELECT * FROM user_context WHERE email = %s"


def _get_user_context_row(conn, email: str) -> dict | None:
    return conn.execute(_USER_CONTEXT_SQL, (email,)).fetchone()


_USER_ORG_SLUGS_SQL = """
    SELECT uom.org_slug
    FROM users u
    JOIN user_org_memberships uom ON uom.user_id = u.id
    WHERE u.email = %s
    ORDER BY uom.org_slug
"""


def list_user_org_slugs(email: str) -> list[str]:
    """Return the list of org slugs the user is a member of.

    Cheap lookup intended for the auth dependency on every authenticated
    request (one indexed JOIN on the membership table). Avoids paying for
    the full `user_context` view, which materializes vendor/department
    allowlists this caller doesn't need.
    """
    pool = get_pool()
    normalized = email.strip().lower()
    with pool.connection() as conn:
        rows = conn.execute(_USER_ORG_SLUGS_SQL, (normalized,)).fetchall()
    return [r["org_slug"] for r in rows]


def _context_row_to_user(row: dict) -> dict:
    return {
        **_user_summary(
            row,
            list(row.get("role_names") or []),
            list(row.get("allowed_departments") or []),
            list(row.get("allowed_vendor_ids") or []),
            list(row.get("denied_vendor_ids") or []),
        ),
        "allowedVendors": row.get("allowed_vendors") or [],
        # `orgs` is a jsonb array of {slug, name, enabledApps} populated by the
        # `user_context` view (migration 021). Each element is already in the
        # camelCased shape the frontend expects, so it serves straight through.
        # Empty list when the user has no memberships yet — never null.
        "orgs": row.get("orgs") or [],
    }


def list_users(org_slug: str, roles: list[str] | None = None) -> list[dict]:
    """Return users in the active org, optionally filtered to those holding any of the given roles.

    Membership join (gate 5 of the request flow): only users who have a
    row in user_org_memberships for `org_slug` are returned. Today every
    user has exactly one membership, but the join handles the
    multi-membership future case correctly (returns the same user once).
    """
    pool = get_pool()
    with pool.connection() as conn:
        if not roles:
            rows = conn.execute(
                """SELECT uc.* FROM user_context uc
                   WHERE uc.id IN (
                       SELECT uom.user_id FROM user_org_memberships uom
                       WHERE uom.org_slug = %s
                   )
                   ORDER BY uc.email""",
                (org_slug,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT uc.* FROM user_context uc
                   WHERE uc.role_names && %s
                     AND uc.id IN (
                         SELECT uom.user_id FROM user_org_memberships uom
                         WHERE uom.org_slug = %s
                     )
                   ORDER BY uc.email""",
                (roles, org_slug),
            ).fetchall()
        return [_context_row_to_user(row) for row in rows]


def _is_member_of_org(conn, user_id: str, org_slug: str) -> bool:
    """Check whether `user_id` has a membership row in `org_slug`.

    Phase 3 of multi-org tenancy (task 254 / strategy 197-r2): user-record
    CRUD endpoints scope by membership so a caller in one org cannot
    read, update, or delete a user that lives only in another org. A
    `False` return on read paths surfaces as "not found" — never leak
    existence across tenants.
    """
    row = conn.execute(
        "SELECT 1 FROM user_org_memberships WHERE user_id = %s AND org_slug = %s",
        (user_id, org_slug),
    ).fetchone()
    return row is not None


def get_user(email: str) -> dict | None:
    """Fetch a single user by email, unscoped.

    Used by the caller-self lookup paths (`_get_caller_roles`, `/me`)
    that need the caller's own record before any org-scope decision is
    made. Other-user lookups should use `get_user_in_org` to enforce
    membership scoping.
    """
    pool = get_pool()
    normalized = email.strip()
    with pool.connection() as conn:
        row = _get_user_context_row(conn, normalized)
    return _context_row_to_user(row) if row else None


def get_user_in_org(email: str, org_slug: str) -> dict | None:
    """Fetch a single user by email, scoped to the active org.

    Returns `None` if the user doesn't exist OR exists but isn't a
    member of `org_slug`. Callers cannot distinguish the two cases by
    design — this prevents tenant-existence leaks across the
    `/users/{email}` GET path.
    """
    pool = get_pool()
    normalized = email.strip()
    with pool.connection() as conn:
        row = _get_user_context_row(conn, normalized)
        if not row:
            return None
        if not _is_member_of_org(conn, str(row["id"]), org_slug):
            return None
    return _context_row_to_user(row)


def create_user(
    email: str,
    first_name: str,
    last_name: str,
    roles: list[str],
    org_slug: str,
) -> dict:
    """Create a new user with roles in the active org.

    Adds a `user_org_memberships` row for `org_slug` in the same
    transaction so the new user is scoped to the caller's tenant.
    Raises `ValueError` if a user with this email already exists in
    any org — cross-org "invite an existing user" flows are out of
    scope for Phase 3 and will be revisited when the picker UX lands.
    """
    pool = get_pool()
    normalized = email.strip()
    with pool.connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = %s", (normalized,)).fetchone()
        if existing:
            raise ValueError(f"User '{normalized}' already exists")

        row = conn.execute(
            "INSERT INTO users (email, first_name, last_name) VALUES (%s, %s, %s) RETURNING *",
            (normalized, first_name, last_name),
        ).fetchone()
        user_id = str(row["id"])

        conn.execute(
            """INSERT INTO user_org_memberships (user_id, org_slug)
               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
            (user_id, org_slug),
        )

        if roles:
            for role_name in roles:
                conn.execute(
                    """INSERT INTO user_roles (user_id, role_id)
                       SELECT %s, r.id FROM roles r WHERE r.name = %s
                       ON CONFLICT DO NOTHING""",
                    (user_id, role_name),
                )

        created = _get_user_context_row(conn, normalized)
        return _context_row_to_user(created)


def update_user(
    email: str,
    org_slug: str,
    roles: list[str] | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    allowed_departments: list[str] | None = None,
    allowed_vendor_ids: list[str] | None = None,
    denied_vendor_ids: list[str] | None = None,
) -> dict:
    """Update a user's fields, scoped to the active org.

    Only non-None arguments are changed. Raises `ValueError` if the
    target user doesn't exist OR isn't a member of `org_slug` — the
    "not found" message is intentionally identical to the no-such-user
    case to avoid leaking cross-tenant existence.
    """
    pool = get_pool()
    normalized = email.strip()
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (normalized,)).fetchone()
        if not row:
            raise ValueError(f"User '{normalized}' not found")
        user_id = str(row["id"])
        if not _is_member_of_org(conn, user_id, org_slug):
            raise ValueError(f"User '{normalized}' not found")

        updates = {}
        if first_name is not None:
            updates["first_name"] = first_name
        if last_name is not None:
            updates["last_name"] = last_name
        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            conn.execute(
                f"UPDATE users SET {set_clause} WHERE id = %s",
                list(updates.values()) + [user_id],
            )

        if roles is not None:
            conn.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
            for role_name in roles:
                conn.execute(
                    """INSERT INTO user_roles (user_id, role_id)
                       SELECT %s, r.id FROM roles r WHERE r.name = %s
                       ON CONFLICT DO NOTHING""",
                    (user_id, role_name),
                )

        if allowed_departments is not None:
            conn.execute("DELETE FROM user_allowed_departments WHERE user_id = %s", (user_id,))
            for dept_name in allowed_departments:
                conn.execute(
                    """INSERT INTO user_allowed_departments (user_id, department_id)
                       SELECT %s, d.id FROM departments d WHERE d.name = %s
                       ON CONFLICT DO NOTHING""",
                    (user_id, dept_name),
                )

        if allowed_vendor_ids is not None:
            conn.execute("DELETE FROM user_allowed_vendors WHERE user_id = %s", (user_id,))
            for vid in allowed_vendor_ids:
                conn.execute(
                    "INSERT INTO user_allowed_vendors (user_id, vendor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (user_id, vid),
                )

        if denied_vendor_ids is not None:
            conn.execute("DELETE FROM user_denied_vendors WHERE user_id = %s", (user_id,))
            for vid in denied_vendor_ids:
                conn.execute(
                    "INSERT INTO user_denied_vendors (user_id, vendor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (user_id, vid),
                )

        updated = _get_user_context_row(conn, normalized)
        return _context_row_to_user(updated)


def delete_user(email: str, org_slug: str) -> bool:
    """Delete a user, scoped to the active org.

    Returns False (treated as "not found" by the caller) when the user
    doesn't exist OR isn't a member of `org_slug`. We never delete a
    user across an org boundary even if the caller is a global admin —
    the membership row is the authority gate. When the user IS a
    member of the active org, the row is removed entirely (including
    memberships in OTHER orgs); cross-org cleanup is deliberate
    because users are global-by-email today.
    """
    pool = get_pool()
    normalized = email.strip()
    with pool.connection() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = %s", (normalized,)).fetchone()
        if not row:
            return False
        user_id = str(row["id"])
        if not _is_member_of_org(conn, user_id, org_slug):
            return False
        cur = conn.execute("DELETE FROM users WHERE id = %s RETURNING id", (user_id,))
        return cur.fetchone() is not None


def get_user_access_context(email: str) -> dict | None:
    """Load a user's access control fields for spend filtering.

    Returns {user_id, roles, allowed_departments, allowed_vendor_ids,
    denied_vendor_ids}.
    """
    pool = get_pool()
    normalized = email.strip()
    with pool.connection() as conn:
        row = _get_user_context_row(conn, normalized)
        if not row:
            return None
        return {
            "user_id": str(row["id"]),
            "roles": list(row.get("role_names") or []),
            "allowed_departments": list(row.get("allowed_departments") or []),
            "allowed_vendor_ids": list(row.get("allowed_vendor_ids") or []),
            "denied_vendor_ids": list(row.get("denied_vendor_ids") or []),
        }


def resolve_effective_vendor_ids(
    allowed_departments: list[str],
    allowed_vendor_ids: list[str],
    denied_vendor_ids: list[str],
    org_slug: str,
    user_id: str | None = None,
) -> list[str]:
    """Compute the effective set of vendor IDs a user can access in the active org.

    Formula: ((dept vendors in org UNION allowed) MINUS denied) MINUS
    (contractors without a user_contractor_access grant), then
    intersected with the org's vendors.

    Cross-org IDs in the user's `allowed_vendor_ids` (defensive: shouldn't
    happen post-Phase-3 writes, but harmless if present in legacy data)
    are filtered out by the final org INTERSECT. When user_id is None the
    contractor filter is skipped (backward compat).
    """
    base_sql = """
        SELECT id FROM vendors
        WHERE department_id IN (
            SELECT id FROM departments WHERE name = ANY(%s)
        )
        UNION
        SELECT unnest(%s::text[])::uuid
        EXCEPT
        SELECT unnest(%s::text[])::uuid
        INTERSECT
        SELECT id FROM vendors WHERE org_slug = %s
    """
    base_params: list = [
        allowed_departments or [],
        allowed_vendor_ids or [],
        denied_vendor_ids or [],
        org_slug,
    ]

    if user_id is not None:
        sql = f"""SELECT id::text FROM ({base_sql}) AS base
                  WHERE base.id NOT IN (
                      SELECT v.id FROM vendors v
                      WHERE v.is_contractor = true
                        AND v.org_slug = %s
                        AND v.id NOT IN (
                            SELECT uca.vendor_id
                            FROM user_contractor_access uca
                            WHERE uca.user_id = %s
                        )
                  )"""
        base_params.extend([org_slug, user_id])
    else:
        sql = f"SELECT id::text FROM ({base_sql}) AS base"

    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(sql, base_params).fetchall()
    return sorted(r["id"] for r in rows)


# ---------------------------------------------------------------------------
# Contractor access
# ---------------------------------------------------------------------------


def set_vendor_is_contractor(
    vendor_id: str, is_contractor: bool, actor_user_id: str, org_slug: str,
) -> dict:
    """Update the is_contractor flag on a vendor scoped to the active org.

    Returns the updated vendor. Raises ValueError if the vendor does not
    belong to *org_slug* (prevents cross-tenant flag flips via guessed UUID).
    """
    pool = get_pool()
    with pool.connection() as conn:
        cur = conn.execute(
            "UPDATE vendors SET is_contractor = %s, modified_at = %s WHERE id = %s AND org_slug = %s RETURNING id",
            (is_contractor, _now(), vendor_id, org_slug),
        )
        if cur.fetchone() is None:
            raise ValueError(f"Vendor '{vendor_id}' not found")
    return get_vendor(vendor_id, org_slug)


def grant_contractor_access(
    user_id: str, vendor_id: str, granted_by_user_id: str,
) -> None:
    """Grant a user access to a contractor vendor."""
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO user_contractor_access (user_id, vendor_id, granted_by)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id, vendor_id) DO NOTHING""",
            (user_id, vendor_id, granted_by_user_id),
        )


def revoke_contractor_access(user_id: str, vendor_id: str) -> bool:
    """Revoke a user's access to a contractor vendor. Returns True if a row was deleted."""
    pool = get_pool()
    with pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM user_contractor_access WHERE user_id = %s AND vendor_id = %s RETURNING user_id",
            (user_id, vendor_id),
        )
        return cur.fetchone() is not None


def list_contractor_access(vendor_id: str) -> list[dict]:
    """List users with access to a specific contractor vendor."""
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT u.id::text AS user_id, u.email,
                      u.first_name, u.last_name,
                      uca.granted_by::text, uca.granted_at,
                      ug.email AS granted_by_email
               FROM user_contractor_access uca
               JOIN users u ON u.id = uca.user_id
               LEFT JOIN users ug ON ug.id = uca.granted_by
               WHERE uca.vendor_id = %s
               ORDER BY u.email""",
            (vendor_id,),
        ).fetchall()
    return [
        {
            "userId": r["user_id"],
            "email": r["email"],
            "firstName": r["first_name"],
            "lastName": r["last_name"],
            "grantedBy": r["granted_by_email"],
            "grantedAt": _ts_str(r["granted_at"]),
        }
        for r in rows
    ]


def list_contractor_vendors(org_slug: str) -> list[dict]:
    """List all contractor vendors in the active org."""
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            f"{_VENDOR_LIST_SQL} WHERE v.is_contractor = true AND v.org_slug = %s ORDER BY LOWER(v.name)",
            (org_slug,),
        ).fetchall()
    return [_vendor_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------

def list_apps() -> list[dict]:
    """Return all app definitions, sorted by type then sort_order."""
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT a.*,
                      COALESCE(
                          array_agg(r.name ORDER BY r.name) FILTER (WHERE r.name IS NOT NULL),
                          '{}'
                      ) AS granting_roles
               FROM apps a
               LEFT JOIN app_granting_roles agr ON agr.app_id = a.id
               LEFT JOIN roles r ON r.id = agr.role_id
               GROUP BY a.id
               ORDER BY (CASE WHEN a.type = 'app' THEN 0 ELSE 1 END), a.sort_order"""
        ).fetchall()
    return [_app_row_to_dict(r) for r in rows]


def get_app(app_id: str) -> dict | None:
    """Fetch a single app by UUID or slug."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT a.*,
                      COALESCE(
                          array_agg(r.name ORDER BY r.name) FILTER (WHERE r.name IS NOT NULL),
                          '{}'
                      ) AS granting_roles
               FROM apps a
               LEFT JOIN app_granting_roles agr ON agr.app_id = a.id
               LEFT JOIN roles r ON r.id = agr.role_id
               WHERE a.id::text = %s OR a.slug = %s
               GROUP BY a.id""",
            (app_id, app_id),
        ).fetchone()
    return _app_row_to_dict(row) if row else None


def update_app(
    app_id: str,
    label: str | None = None,
    granting_roles: list[str] | None = None,
    sort_order: int | None = None,
) -> dict:
    """Update an app definition. Raises ValueError if not found."""
    pool = get_pool()
    with pool.connection() as conn:
        existing = conn.execute(
            "SELECT id FROM apps WHERE id::text = %s OR slug = %s", (app_id, app_id)
        ).fetchone()
        if not existing:
            raise ValueError(f"App '{app_id}' not found")
        real_id = str(existing["id"])

        updates = {}
        if label is not None:
            updates["label"] = label
        if sort_order is not None:
            updates["sort_order"] = sort_order
        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            conn.execute(
                f"UPDATE apps SET {set_clause} WHERE id = %s",
                list(updates.values()) + [real_id],
            )

        if granting_roles is not None:
            conn.execute("DELETE FROM app_granting_roles WHERE app_id = %s", (real_id,))
            for role_name in granting_roles:
                conn.execute(
                    """INSERT INTO app_granting_roles (app_id, role_id)
                       SELECT %s, r.id FROM roles r WHERE r.name = %s
                       ON CONFLICT DO NOTHING""",
                    (real_id, role_name),
                )

    return get_app(real_id)


def _app_row_to_dict(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "label": row.get("label"),
        "type": row.get("type", "app"),
        "sort_order": row.get("sort_order", 99),
        "path": row.get("path"),
        "icon": row.get("icon"),
        "granting_roles": row.get("granting_roles", []),
    }


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

def get_branding() -> dict | None:
    """Return the singleton branding row, or None if no row exists."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT logo_svg, lockup_svg, show_lockup FROM branding WHERE id = 1"
        ).fetchone()
    if not row:
        return None
    return {
        "logoSvg": row["logo_svg"],
        "lockupSvg": row["lockup_svg"],
        "lockupMode": row["show_lockup"],
    }


# ---------------------------------------------------------------------------
# Departments (convenience helpers)
# ---------------------------------------------------------------------------

def list_departments() -> list[dict]:
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute("SELECT * FROM departments ORDER BY name").fetchall()
    return [{"id": str(r["id"]), "name": r["name"]} for r in rows]


def get_editable_schema(
    table: str,
    allowed_columns: frozenset[str],
) -> list[dict]:
    """Return column metadata for editable fields of any table.

    Queries information_schema.columns and filters to *allowed_columns*.
    Returns [{column, pg_type, nullable}] with snake_case column names.
    """
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT column_name, data_type, is_nullable
               FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = %s
               ORDER BY ordinal_position""",
            (table,),
        ).fetchall()
    return [
        {
            "column": r["column_name"],
            "pg_type": r["data_type"],
            "nullable": r["is_nullable"] == "YES",
        }
        for r in rows
        if r["column_name"] in allowed_columns
    ]


def ids_exist(table: str, ids: list[str], pk: str = "id") -> dict[str, bool]:
    """Check which primary-key values exist in *table*. Returns {id: bool}."""
    if not ids:
        return {}
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {pk}::text AS pk FROM {table} WHERE {pk} = ANY(%s::uuid[])",
            (ids,),
        ).fetchall()
    found = {r["pk"] for r in rows}
    return {v: v in found for v in ids}


def batch_update(
    table: str,
    allowed_columns: frozenset[str],
    updates: list[dict],
    pk: str = "id",
    pk_key: str = "vendor_id",
    ts_column: str | None = "modified_at",
    org_slug: str | None = None,
    org_column: str | None = None,
) -> int:
    """Apply multiple row updates in a single transaction.

    Each item in *updates* must have *pk_key* (the row identifier) and
    'changes' (snake_case field dict). Returns the count of rows updated.
    Raises ValueError if any row is not found.

    When *org_slug* and *org_column* are supplied, every UPDATE is scoped
    by `org_column = org_slug`, so a row in another tenant raises
    ValueError as if not found. Used by `batch_update_vendors` to enforce
    Phase 3 tenant isolation on bulk edits.
    """
    if not updates:
        return 0
    pool = get_pool()
    now = _now()
    count = 0
    org_clause = f" AND {org_column} = %s" if org_slug and org_column else ""
    with pool.connection() as conn:
        with conn.transaction():
            for item in updates:
                fields = {k: v for k, v in item["changes"].items() if k in allowed_columns}
                if not fields:
                    continue
                if ts_column:
                    fields[ts_column] = now
                set_clause = ", ".join(f"{k} = %s" for k in fields)
                values: list = list(fields.values()) + [item[pk_key]]
                if org_clause:
                    values.append(org_slug)
                cur = conn.execute(
                    f"UPDATE {table} SET {set_clause} WHERE {pk} = %s{org_clause} RETURNING {pk}",
                    values,
                )
                if cur.fetchone() is None:
                    raise ValueError(f"Row '{item[pk_key]}' not found in {table}")
                count += 1
    return count


def get_vendor_editable_schema() -> list[dict]:
    return get_editable_schema("vendors", VENDOR_EDITABLE_COLUMNS)


def vendor_ids_exist(vendor_ids: list[str], org_slug: str) -> dict[str, bool]:
    """Return {id: bool} indicating which vendor IDs exist in the active org.

    Cross-org IDs always read as False so CSV uploads referencing another
    tenant's vendor surface as "unknown vendor" rather than mutating it.
    """
    if not vendor_ids:
        return {}
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id::text AS pk FROM vendors WHERE id = ANY(%s::uuid[]) AND org_slug = %s",
            (vendor_ids, org_slug),
        ).fetchall()
    found = {r["pk"] for r in rows}
    return {v: v in found for v in vendor_ids}


def batch_update_vendors(updates: list[dict], org_slug: str) -> int:
    """Apply multiple vendor updates in a transaction, scoped to the active org.

    Each update dict must include 'vendor_id' and 'changes'. Rows in
    other orgs raise ValueError as if not found, preventing
    cross-tenant writes via guessed UUIDs.
    """
    return batch_update(
        "vendors", VENDOR_EDITABLE_COLUMNS, updates,
        org_slug=org_slug, org_column="org_slug",
    )


def get_or_create_department(name: str) -> str:
    """Return the UUID of a department, creating it if it doesn't exist."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO departments (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (name,),
        ).fetchone()
    return str(row["id"])


# ---------------------------------------------------------------------------
# Roles (convenience helpers)
# ---------------------------------------------------------------------------

def list_roles() -> list[dict]:
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute("SELECT * FROM roles ORDER BY name").fetchall()
    return [{"id": str(r["id"]), "name": r["name"]} for r in rows]


# ---------------------------------------------------------------------------
# Chat sessions & feedback
# ---------------------------------------------------------------------------

def get_user_id_by_email(email: str) -> str | None:
    """Resolve an email to a user UUID without loading relationships."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email = %s", (email.strip(),)
        ).fetchone()
    return str(row["id"]) if row else None


def upsert_chat_session(
    session_id: str,
    user_id: str,
    app_context: str,
    messages_json: list[dict],
) -> None:
    """Create or update a chat session with the latest messages."""
    import json as _json

    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO chat_sessions (id, user_id, app_context, messages)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE
                SET messages    = EXCLUDED.messages,
                    modified_at = now()
            """,
            (session_id, user_id, app_context, _json.dumps(messages_json)),
        )


def get_chat_session(session_id: str) -> dict | None:
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE id = %s", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def upsert_feedback(
    chat_session_id: str,
    message_seq: int,
    user_id: str,
    signal: bool,
    comment: str | None = None,
) -> None:
    """Create or update feedback for a specific message in a session."""
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO agent_feedback (chat_session_id, message_seq, user_id, signal, comment)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (chat_session_id, message_seq) DO UPDATE
                SET signal      = EXCLUDED.signal,
                    comment     = EXCLUDED.comment,
                    modified_at = now()
            """,
            (chat_session_id, message_seq, user_id, signal, comment),
        )


def insert_site_feedback(
    user_id: str,
    app_id: str,
    open_panes: dict | None,
    feedback_text: str,
) -> None:
    """Insert a general site feedback entry."""
    import json as _json

    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO site_feedback (user_id, app_id, open_panes, feedback_text)
            VALUES (%s, %s, %s::jsonb, %s)
            """,
            (
                user_id,
                app_id,
                _json.dumps(open_panes) if open_panes else None,
                feedback_text,
            ),
        )


# ---------------------------------------------------------------------------
# Feedback review queries (MCP feedback server)
# ---------------------------------------------------------------------------


def list_agent_feedback(
    *,
    uncollected_only: bool = True,
    limit: int = 20,
    since: str | None = None,
) -> list[dict]:
    """List recent agent feedback with user email resolved."""
    pool = get_pool()
    clauses: list[str] = []
    params: list = []
    if uncollected_only:
        clauses.append("af.collected = false")
    if since:
        clauses.append("af.created_at >= %s::timestamptz")
        params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT af.id, af.created_at, af.signal, af.comment,
                   af.chat_session_id, af.message_seq, af.collected,
                   u.email AS user_email
            FROM agent_feedback af
            LEFT JOIN users u ON u.id = af.user_id
            {where}
            ORDER BY af.created_at DESC
            LIMIT %s
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def list_site_feedback_entries(
    *,
    uncollected_only: bool = True,
    limit: int = 20,
    since: str | None = None,
    app_id: str | None = None,
) -> list[dict]:
    """List recent site feedback with user email resolved."""
    pool = get_pool()
    clauses: list[str] = []
    params: list = []
    if uncollected_only:
        clauses.append("sf.collected = false")
    if since:
        clauses.append("sf.created_at >= %s::timestamptz")
        params.append(since)
    if app_id:
        clauses.append("sf.app_id = %s")
        params.append(app_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT sf.id, sf.created_at, sf.app_id, sf.feedback_text,
                   sf.open_panes, sf.collected,
                   u.email AS user_email
            FROM site_feedback sf
            LEFT JOIN users u ON u.id = sf.user_id
            {where}
            ORDER BY sf.created_at DESC
            LIMIT %s
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_agent_feedback_by_id(feedback_id: str) -> dict | None:
    """Single agent feedback row with user email."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT af.id, af.created_at, af.signal, af.comment,
                   af.chat_session_id, af.message_seq, af.collected,
                   u.email AS user_email
            FROM agent_feedback af
            LEFT JOIN users u ON u.id = af.user_id
            WHERE af.id = %s
            """,
            (feedback_id,),
        ).fetchone()
    return dict(row) if row else None


def get_site_feedback_by_id(feedback_id: str) -> dict | None:
    """Single site feedback row with user email."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT sf.id, sf.created_at, sf.app_id, sf.feedback_text,
                   sf.open_panes, sf.collected,
                   u.email AS user_email
            FROM site_feedback sf
            LEFT JOIN users u ON u.id = sf.user_id
            WHERE sf.id = %s
            """,
            (feedback_id,),
        ).fetchone()
    return dict(row) if row else None


def get_chat_session_detail(session_id: str) -> dict | None:
    """Chat session with user email resolved (for MCP feedback server)."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT cs.id, cs.created_at, cs.modified_at, cs.app_context,
                   cs.messages, u.email AS user_email
            FROM chat_sessions cs
            LEFT JOIN users u ON u.id = cs.user_id
            WHERE cs.id = %s
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_chat_sessions_summary(
    *,
    limit: int = 20,
    since: str | None = None,
    app_context: str | None = None,
    user_email: str | None = None,
) -> list[dict]:
    """List recent chat sessions with message count and user email."""
    pool = get_pool()
    clauses: list[str] = []
    params: list = []
    if since:
        clauses.append("cs.created_at >= %s::timestamptz")
        params.append(since)
    if app_context:
        clauses.append("cs.app_context = %s")
        params.append(app_context)
    if user_email:
        clauses.append("u.email = %s")
        params.append(user_email.strip())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT cs.id, cs.created_at, cs.app_context,
                   u.email AS user_email,
                   jsonb_array_length(cs.messages) AS message_count
            FROM chat_sessions cs
            LEFT JOIN users u ON u.id = cs.user_id
            {where}
            ORDER BY cs.created_at DESC
            LIMIT %s
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def count_uncollected_feedback() -> dict:
    """Counts of uncollected agent and site feedback."""
    pool = get_pool()
    with pool.connection() as conn:
        agent = conn.execute(
            "SELECT count(*) AS cnt FROM agent_feedback WHERE collected = false"
        ).fetchone()["cnt"]
        site = conn.execute(
            "SELECT count(*) AS cnt FROM site_feedback WHERE collected = false"
        ).fetchone()["cnt"]
    return {"agent": agent, "site": site}


def mark_feedback_collected(feedback_type: str, ids: list[str]) -> int:
    """Set collected=true on the given feedback IDs. Returns rows updated."""
    table = "agent_feedback" if feedback_type == "agent" else "site_feedback"
    pool = get_pool()
    with pool.connection() as conn:
        cur = conn.execute(
            f"UPDATE {table} SET collected = true WHERE id = ANY(%s) AND collected = false",
            (ids,),
        )
        return cur.rowcount
