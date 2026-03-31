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
            min_size=1,
            max_size=10,
            max_idle=300,
            reconnect_timeout=60,
            check=ConnectionPool.check_connection,
            kwargs={"row_factory": dict_row},
        )
    return _pool


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


def list_vendors() -> list[dict]:
    """Return all vendors with joined department/owner names."""
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            f"{_VENDOR_LIST_SQL} ORDER BY LOWER(v.name)"
        ).fetchall()
    return [_vendor_row_to_dict(r) for r in rows]


def get_vendor(vendor_id: str) -> dict | None:
    """Fetch a vendor by UUID id."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            f"{_VENDOR_LIST_SQL} WHERE v.id = %s", (vendor_id,)
        ).fetchone()
    return _vendor_row_to_dict(row) if row else None


def get_vendor_by_source(source_system: str, source_system_id: str) -> dict | None:
    """Fetch a vendor by its upstream identity."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            f"{_VENDOR_LIST_SQL} WHERE v.source_system = %s AND v.source_system_id = %s",
            (source_system, source_system_id),
        ).fetchone()
    return _vendor_row_to_dict(row) if row else None


def find_vendor_by_name(name: str, include_hidden: bool = False) -> dict | None:
    """Find a vendor by exact name (case-insensitive)."""
    hidden_clause = "" if include_hidden else " AND v.hidden_from_agent = false"
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            f"{_VENDOR_LIST_SQL} WHERE LOWER(v.name) = LOWER(%s){hidden_clause} LIMIT 1", (name,)
        ).fetchone()
    return _vendor_row_to_dict(row) if row else None


FUZZY_AUTO_ACCEPT = 0.6
FUZZY_SUGGEST = 0.25
SIMILAR_THRESHOLD = 0.4


def find_vendors_similar(name: str, threshold: float = FUZZY_SUGGEST,
                         limit: int = 5,
                         include_hidden: bool = False) -> list[tuple[dict, float]]:
    """Find vendors by trigram similarity (requires pg_trgm).

    Returns a list of (vendor_dict, similarity_score) tuples sorted by
    descending score, filtered to scores above *threshold*.
    """
    hidden_clause = "" if include_hidden else " AND sub.hidden_from_agent = false"
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT sub.*, similarity(LOWER(sub.name), LOWER(%s)) AS sim_score
                FROM ({_VENDOR_LIST_SQL}) sub
                WHERE similarity(LOWER(sub.name), LOWER(%s)) > %s{hidden_clause}
                ORDER BY sim_score DESC
                LIMIT %s""",
            (name, name, threshold, limit),
        ).fetchall()
    results = []
    for row in rows:
        score = float(row.pop("sim_score"))
        results.append((_vendor_row_to_dict(row), score))
    return results


def add_vendor(data: dict) -> dict:
    """Create a new vendor. Returns the created record."""
    pool = get_pool()
    now = _now()
    with pool.connection() as conn:
        row = conn.execute(
            """INSERT INTO vendors (name, source_system, source_system_id, created_at, modified_at)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING *""",
            (data["name"], data.get("source_system", "manual"), data.get("source_system_id", _slugify(data["name"])), now, now),
        ).fetchone()
    return get_vendor(str(row["id"]))


def update_vendor(vendor_id: str, updates: dict) -> dict:
    """Partial-update a vendor. Returns the full record after update."""
    pool = get_pool()
    allowed = {
        "name", "department_id", "owner_id", "secondary_owner_id",
        "payment_method", "billing_frequency", "account_type", "track_1099",
        "purpose", "spend_type", "aliases", "contract_start", "contract_end",
        "contract_months", "auto_renew", "renewal_rate", "renewal_notice",
        "termination_terms",
    }
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        result = get_vendor(vendor_id)
        if not result:
            raise ValueError(f"Vendor '{vendor_id}' not found")
        return result

    fields["modified_at"] = _now()
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [vendor_id]

    with pool.connection() as conn:
        cur = conn.execute(
            f"UPDATE vendors SET {set_clause} WHERE id = %s RETURNING id",
            values,
        )
        if cur.fetchone() is None:
            raise ValueError(f"Vendor '{vendor_id}' not found")

    return get_vendor(vendor_id)


def delete_vendor(vendor_id: str) -> bool:
    """Delete a vendor. Returns True if deleted."""
    pool = get_pool()
    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM vendors WHERE id = %s RETURNING id", (vendor_id,))
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


def _load_all_vendors_light(include_hidden: bool = False) -> list[dict]:
    """Load id, name, aliases for all vendors (used by alias/normalized matching)."""
    hidden_clause = "" if include_hidden else " WHERE hidden_from_agent = false"
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT id::text, name, aliases FROM vendors{hidden_clause} ORDER BY name"
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


def resolve_vendor_by_identifier(identifier: str) -> VendorMatch | None:
    """Resolve a vendor by UUID, name, alias, normalized name, or fuzzy match.

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
        vendor = get_vendor(identifier)
        if vendor:
            return VendorMatch(vendor, "exact")

    # Step 2: exact name
    vendor = find_vendor_by_name(identifier)
    if vendor:
        match_type = "exact"
    else:
        id_lower = identifier.lower()
        norm_query = _normalise(identifier)
        all_vendors = _load_all_vendors_light()

        # Step 3: alias match
        alias_matches = [
            v for v in all_vendors
            if any(a.lower() == id_lower for a in (v.get("aliases") or []))
        ]
        if len(alias_matches) == 1:
            vendor = get_vendor(alias_matches[0]["id"])
            match_type = "exact"
        elif len(alias_matches) > 1:
            vendor = get_vendor(alias_matches[0]["id"])
            match_type = "disambiguate"
            alternatives = [get_vendor(v["id"]) for v in alias_matches[1:]]
            return VendorMatch(vendor, match_type, [a for a in alternatives if a])

        # Step 4: normalized match
        if not vendor and norm_query:
            norm_matches = [
                v for v in all_vendors
                if _normalise(v.get("name", "")) == norm_query
            ]
            if len(norm_matches) == 1:
                vendor = get_vendor(norm_matches[0]["id"])
                match_type = "exact"
            elif len(norm_matches) > 1:
                vendor = get_vendor(norm_matches[0]["id"])
                match_type = "disambiguate"
                alternatives = [get_vendor(v["id"]) for v in norm_matches[1:]]
                return VendorMatch(vendor, match_type, [a for a in alternatives if a])

        # Step 5: pg_trgm fuzzy
        if not vendor:
            similar = find_vendors_similar(identifier)
            if not similar:
                return None
            vendor, score = similar[0]
            match_type = "close" if score >= FUZZY_AUTO_ACCEPT else "fuzzy"

    # --- Pass 2: check for ambiguity (skip for exact name matches) ---
    if match_type == "exact":
        return VendorMatch(vendor, match_type)

    similar = find_vendors_similar(identifier, threshold=SIMILAR_THRESHOLD)
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
) -> list[dict]:
    """Query spend for specific vendors within a month range.

    Accepts YYYY-MM strings for start/end and converts to DATE internally.
    Returns rows shaped for the frontend: {vendor, vendorId, month, amount}.
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
               JOIN vendors v ON v.id = s.vendor_id
               WHERE s.vendor_id = ANY(%s::uuid[])
                 AND s.date >= %s AND s.date <= %s
               ORDER BY s.date, v.name""",
            (vendor_ids, start_date, end_date),
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


def get_vendor_spend(vendor_id: str, months: int = 6) -> list[dict]:
    """Return recent monthly spend for a single vendor."""
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT TO_CHAR(date, 'YYYY-MM') AS month,
                      total_amount AS "totalAmount",
                      bill_count AS "billCount"
               FROM vendor_monthly_spend
               WHERE vendor_id = %s
               ORDER BY date DESC
               LIMIT %s""",
            (vendor_id, months),
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
    category: str | None = None,
    subcategory: str | None = None,
    project: str | None = None,
    group_by: str | None = None,
) -> list[dict]:
    """Query vendor_spend_detail with optional filters and grouping.

    When group_by is set (one of 'category', 'subcategory', 'project'),
    returns rows grouped and summed by that dimension.  Otherwise returns
    individual detail rows.
    """
    pool = get_pool()
    start_date = _month_to_date(start_month)
    end_date = _month_to_date(end_month)

    params: list[Any] = [vendor_id, start_date, end_date]
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

    if group_by and group_by in valid_group_by:
        sql = f"""
            SELECT d.{group_by} AS dimension_value,
                   TO_CHAR(d.date, 'YYYY-MM') AS month,
                   SUM(d.amount) AS amount
            FROM vendor_spend_detail d
            WHERE d.vendor_id = %s AND d.date >= %s AND d.date <= %s
            {filters}
            GROUP BY d.{group_by}, d.date
            ORDER BY d.date, amount DESC
        """
    else:
        sql = f"""
            SELECT d.category, d.subcategory, d.project, d.user_email,
                   TO_CHAR(d.date, 'YYYY-MM') AS month,
                   d.amount, d.metadata
            FROM vendor_spend_detail d
            WHERE d.vendor_id = %s AND d.date >= %s AND d.date <= %s
            {filters}
            ORDER BY d.date, d.amount DESC
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
    dimension: str | None = None,
) -> dict:
    """Return distinct dimension values for a vendor's detail rows.

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
                f"""SELECT DISTINCT {dim} AS val
                    FROM vendor_spend_detail
                    WHERE vendor_id = %s AND {dim} IS NOT NULL
                    ORDER BY val""",
                (vendor_id,),
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


def _load_user_roles(conn, user_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = %s",
        (user_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def _load_user_allowed_departments(conn, user_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT d.name FROM user_allowed_departments uad JOIN departments d ON d.id = uad.department_id WHERE uad.user_id = %s",
        (user_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def _load_user_allowed_vendors(conn, user_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT vendor_id FROM user_allowed_vendors WHERE user_id = %s",
        (user_id,),
    ).fetchall()
    return [str(r["vendor_id"]) for r in rows]


def _load_user_denied_vendors(conn, user_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT vendor_id FROM user_denied_vendors WHERE user_id = %s",
        (user_id,),
    ).fetchall()
    return [str(r["vendor_id"]) for r in rows]


def _get_full_user(conn, user_id: str, row: dict) -> dict:
    """Build full user summary with all relationship data."""
    roles = _load_user_roles(conn, user_id)
    allowed_depts = _load_user_allowed_departments(conn, user_id)
    allowed_vendors = _load_user_allowed_vendors(conn, user_id)
    denied_vendors = _load_user_denied_vendors(conn, user_id)
    user = _user_summary(row, roles, allowed_depts, allowed_vendors, denied_vendors)

    if allowed_vendors:
        resolved = []
        for vid in allowed_vendors:
            v = get_vendor(vid)
            resolved.append({"id": vid, "name": v["name"] if v else vid})
        user["allowedVendors"] = resolved
    else:
        user["allowedVendors"] = []

    return user


def list_users(roles: list[str] | None = None) -> list[dict]:
    """Return users, optionally filtered to those holding any of the given roles."""
    pool = get_pool()
    with pool.connection() as conn:
        if not roles:
            rows = conn.execute("SELECT * FROM users ORDER BY email").fetchall()
        else:
            rows = conn.execute(
                """SELECT DISTINCT u.* FROM users u
                   JOIN user_roles ur ON ur.user_id = u.id
                   JOIN roles r ON r.id = ur.role_id
                   WHERE r.name = ANY(%s)
                   ORDER BY u.email""",
                (roles,),
            ).fetchall()

        results = []
        for row in rows:
            user_id = str(row["id"])
            user_roles = _load_user_roles(conn, user_id)
            allowed_depts = _load_user_allowed_departments(conn, user_id)
            allowed_vendors = _load_user_allowed_vendors(conn, user_id)
            denied_vendors = _load_user_denied_vendors(conn, user_id)
            results.append(_user_summary(row, user_roles, allowed_depts, allowed_vendors, denied_vendors))
        return results


def get_user(email: str) -> dict | None:
    """Fetch a single user by email."""
    pool = get_pool()
    normalized = email.strip().lower()
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (normalized,)).fetchone()
        if not row:
            return None
        return _get_full_user(conn, str(row["id"]), row)


def create_user(email: str, first_name: str, last_name: str, roles: list[str]) -> dict:
    """Create a new user with roles. Raises ValueError if user already exists."""
    pool = get_pool()
    normalized = email.strip().lower()
    with pool.connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = %s", (normalized,)).fetchone()
        if existing:
            raise ValueError(f"User '{normalized}' already exists")

        row = conn.execute(
            "INSERT INTO users (email, first_name, last_name) VALUES (%s, %s, %s) RETURNING *",
            (normalized, first_name, last_name),
        ).fetchone()
        user_id = str(row["id"])

        if roles:
            for role_name in roles:
                conn.execute(
                    """INSERT INTO user_roles (user_id, role_id)
                       SELECT %s, r.id FROM roles r WHERE r.name = %s
                       ON CONFLICT DO NOTHING""",
                    (user_id, role_name),
                )

        user_roles = _load_user_roles(conn, user_id)
        return _user_summary(row, user_roles)


def update_user(
    email: str,
    roles: list[str] | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    allowed_departments: list[str] | None = None,
    allowed_vendor_ids: list[str] | None = None,
    denied_vendor_ids: list[str] | None = None,
) -> dict:
    """Update a user's fields. Only non-None arguments are changed."""
    pool = get_pool()
    normalized = email.strip().lower()
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (normalized,)).fetchone()
        if not row:
            raise ValueError(f"User '{normalized}' not found")
        user_id = str(row["id"])

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

        updated_row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        return _get_full_user(conn, user_id, updated_row)


def delete_user(email: str) -> bool:
    """Delete a user. Returns True if deleted."""
    pool = get_pool()
    normalized = email.strip().lower()
    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM users WHERE email = %s RETURNING id", (normalized,))
        return cur.fetchone() is not None


def get_user_access_context(email: str) -> dict | None:
    """Load a user's access control fields for spend filtering.

    Returns {roles, allowed_departments, allowed_vendor_ids, denied_vendor_ids}.
    """
    pool = get_pool()
    normalized = email.strip().lower()
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (normalized,)).fetchone()
        if not row:
            return None
        user_id = str(row["id"])
        return {
            "roles": _load_user_roles(conn, user_id),
            "allowed_departments": _load_user_allowed_departments(conn, user_id),
            "allowed_vendor_ids": _load_user_allowed_vendors(conn, user_id),
            "denied_vendor_ids": _load_user_denied_vendors(conn, user_id),
        }


def resolve_effective_vendor_ids(
    allowed_departments: list[str],
    allowed_vendor_ids: list[str],
    denied_vendor_ids: list[str],
) -> list[str]:
    """Compute the effective set of vendor IDs a user can access.

    (vendors in allowed_departments UNION allowed_vendor_ids) MINUS denied_vendor_ids.
    """
    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT id::text FROM vendors
               WHERE department_id IN (SELECT id FROM departments WHERE name = ANY(%s))
               UNION
               SELECT unnest(%s::text[])
               EXCEPT
               SELECT unnest(%s::text[])""",
            (
                allowed_departments or [],
                allowed_vendor_ids or [],
                denied_vendor_ids or [],
            ),
        ).fetchall()
    return sorted(r["id"] for r in rows)


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
