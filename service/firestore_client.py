import re
from datetime import datetime, timezone

from google.cloud import firestore

_db: firestore.Client | None = None

VENDORS_COLLECTION = "vendors"
VENDOR_SPEND_COLLECTION = "vendor_spend"


def get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def slugify(name: str) -> str:
    """Convert a vendor name to a kebab-case document ID."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_vendor(data: dict) -> dict:
    """Create a new vendor document. Returns the written data."""
    db = get_db()
    doc_id = data.get("id") or slugify(data["name"])
    data["id"] = doc_id

    ref = db.collection(VENDORS_COLLECTION).document(doc_id)
    if ref.get().exists:
        raise ValueError(f"Vendor '{doc_id}' already exists")

    now = _now_iso()
    data["created_at"] = now
    data["modified_at"] = now
    ref.set(data)
    return data


def update_vendor(vendor_id: str, updates: dict) -> dict:
    """Partial-update an existing vendor. Returns the full document after update."""
    db = get_db()
    ref = db.collection(VENDORS_COLLECTION).document(vendor_id)
    snap = ref.get()
    if not snap.exists:
        raise ValueError(f"Vendor '{vendor_id}' not found")

    updates["modified_at"] = _now_iso()
    ref.update(updates)
    return ref.get().to_dict()


def get_vendor(vendor_id: str) -> dict | None:
    """Fetch a vendor by document ID."""
    db = get_db()
    snap = db.collection(VENDORS_COLLECTION).document(vendor_id).get()
    return snap.to_dict() if snap.exists else None


def find_vendor_by_name(name: str) -> dict | None:
    """Query vendors collection by name (case-insensitive match)."""
    db = get_db()
    docs = (
        db.collection(VENDORS_COLLECTION)
        .where("name", "==", name)
        .limit(1)
        .stream()
    )
    for doc in docs:
        return doc.to_dict()
    return None


def delete_vendor(vendor_id: str) -> bool:
    """Delete a vendor document. Returns True if deleted."""
    db = get_db()
    ref = db.collection(VENDORS_COLLECTION).document(vendor_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def _user_summary(doc_id: str, data: dict) -> dict:
    return {
        "email": doc_id,
        "firstName": data.get("first_name", ""),
        "lastName": data.get("last_name", ""),
        "roles": data.get("roles", []),
        "allowedDepartments": data.get("allowed_departments", []),
        "allowedVendorIds": data.get("allowed_vendor_ids", []),
        "deniedVendorIds": data.get("denied_vendor_ids", []),
    }


def list_users(roles: list[str] | None = None) -> list[dict]:
    """Return users, optionally filtered to those holding any of the given roles.

    Firestore ``array_contains`` supports only a single value per query, so
    we run one query per role and merge the results.
    """
    db = get_db()

    if not roles:
        docs = db.collection("users").stream()
        return [_user_summary(d.id, d.to_dict()) for d in docs]

    seen: set[str] = set()
    results: list[dict] = []
    for role in roles:
        docs = (
            db.collection("users")
            .where("roles", "array_contains", role)
            .stream()
        )
        for d in docs:
            if d.id not in seen:
                seen.add(d.id)
                results.append(_user_summary(d.id, d.to_dict()))
    return results


def get_user(email: str) -> dict | None:
    """Fetch a single user doc by email. Returns None if not found."""
    db = get_db()
    snap = db.collection("users").document(email).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    user = _user_summary(snap.id, data)
    allowed_ids = data.get("allowed_vendor_ids", [])
    if allowed_ids:
        resolved = []
        for vid in allowed_ids:
            vendor = get_vendor(vid)
            resolved.append({"id": vid, "name": vendor.get("name", vid) if vendor else vid})
        user["allowedVendors"] = resolved
    else:
        user["allowedVendors"] = []
    return user


def create_user(email: str, first_name: str, last_name: str, roles: list[str]) -> dict:
    """Create a new user doc. Raises ValueError if the user already exists."""
    db = get_db()
    normalized = email.strip().lower()
    ref = db.collection("users").document(normalized)
    if ref.get().exists:
        raise ValueError(f"User '{normalized}' already exists")
    data = {
        "first_name": first_name,
        "last_name": last_name,
        "roles": roles,
        "createdAt": _now_iso(),
    }
    ref.set(data)
    return _user_summary(normalized, data)


def update_user(
    email: str,
    roles: list[str] | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    allowed_departments: list[str] | None = None,
    allowed_vendor_ids: list[str] | None = None,
    denied_vendor_ids: list[str] | None = None,
) -> dict:
    """Update a user's fields. Only non-None fields are written.

    Raises ValueError if the user does not exist.
    """
    db = get_db()
    normalized = email.strip().lower()
    ref = db.collection("users").document(normalized)
    snap = ref.get()
    if not snap.exists:
        raise ValueError(f"User '{normalized}' not found")
    updates: dict = {}
    if roles is not None:
        updates["roles"] = roles
    if first_name is not None:
        updates["first_name"] = first_name
    if last_name is not None:
        updates["last_name"] = last_name
    if allowed_departments is not None:
        updates["allowed_departments"] = allowed_departments
    if allowed_vendor_ids is not None:
        updates["allowed_vendor_ids"] = allowed_vendor_ids
    if denied_vendor_ids is not None:
        updates["denied_vendor_ids"] = denied_vendor_ids
    if updates:
        ref.update(updates)
    updated = ref.get().to_dict()
    return _user_summary(normalized, updated)


def delete_user(email: str) -> bool:
    """Delete a user doc. Returns True if deleted, False if not found."""
    db = get_db()
    normalized = email.strip().lower()
    ref = db.collection("users").document(normalized)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def set_vendor_hidden(vendor_id: str, hide: bool) -> dict:
    """Set or clear the hide flag on a vendor. Returns the updated doc."""
    db = get_db()
    ref = db.collection(VENDORS_COLLECTION).document(vendor_id)
    snap = ref.get()
    if not snap.exists:
        raise ValueError(f"Vendor '{vendor_id}' not found")
    ref.update({"hide": hide, "modified_at": _now_iso()})
    return ref.get().to_dict()


def get_hidden_vendor_ids() -> set[str]:
    """Return the set of vendorIds that are hidden from spend analysis."""
    db = get_db()
    docs = db.collection(VENDORS_COLLECTION).where("hide", "==", True).stream()
    hidden = set()
    for doc in docs:
        data = doc.to_dict()
        hidden.add(data.get("billcomId") or doc.id)
    return hidden


def resolve_vendor(identifier: str) -> dict | None:
    """Find a vendor by ID or by name."""
    result = get_vendor(identifier)
    if result:
        return result
    result = find_vendor_by_name(identifier)
    if result:
        return result
    slug = slugify(identifier)
    if slug != identifier:
        return get_vendor(slug)
    return None


def get_feature_flag(flag_name: str, default: bool = False) -> bool:
    """Read a boolean feature flag from the config/feature_flags Firestore doc."""
    db = get_db()
    snap = db.collection("config").document("feature_flags").get()
    if not snap.exists:
        return default
    return bool(snap.to_dict().get(flag_name, default))


def resolve_effective_vendor_ids(
    allowed_departments: list[str],
    allowed_vendor_ids: list[str],
    denied_vendor_ids: list[str],
) -> list[str]:
    """Compute the effective set of vendor IDs a user can access.

    Resolution: (vendors in allowed_departments UNION allowed_vendor_ids)
                MINUS denied_vendor_ids.
    Deny always wins.
    Vendors with no department are invisible unless explicitly in allowed_vendor_ids.
    """
    db = get_db()
    dept_set = set(allowed_departments) if allowed_departments else set()

    vendor_ids: set[str] = set()
    if dept_set:
        for doc in db.collection(VENDORS_COLLECTION).stream():
            data = doc.to_dict()
            if data.get("department") in dept_set:
                vendor_ids.add(doc.id)

    vendor_ids.update(allowed_vendor_ids or [])
    vendor_ids -= set(denied_vendor_ids or [])

    return sorted(vendor_ids)


def get_user_access_context(email: str) -> dict | None:
    """Load a user's raw access control fields for spend filtering.

    Returns None if the user doc doesn't exist. Does not resolve vendor
    names -- returns raw Firestore field values for use by _build_caller_context.
    """
    db = get_db()
    snap = db.collection("users").document(email.strip().lower()).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    return {
        "roles": data.get("roles", []),
        "allowed_departments": data.get("allowed_departments", []),
        "allowed_vendor_ids": data.get("allowed_vendor_ids", []),
        "denied_vendor_ids": data.get("denied_vendor_ids", []),
    }


SEARCH_RETURN_FIELDS = [
    "name", "billcomId", "toolCall", "paymentMethod", "accountType",
    "track1099", "hide", "owner", "secondaryOwner", "department", "purpose",
    "spendType", "billingFrequency", "contractStartDate", "contractEndDate",
    "contractLengthMonths", "autoRenew", "renewalRate", "renewalNoticeDays",
    "terminationTerms", "lastSyncedAt", "aliases",
]


def list_vendors() -> list[dict]:
    """Return all vendors with full field set for API responses."""
    db = get_db()
    results = []
    for doc in db.collection(VENDORS_COLLECTION).stream():
        data = doc.to_dict()
        record = {"id": doc.id}
        for f in SEARCH_RETURN_FIELDS:
            if f in data:
                record[f] = data[f]
        results.append(record)
    return results

SPEND_RETURN_FIELDS = ["month", "totalAmount", "billCount"]


def get_vendor_spend(vendor_id: str, months: int = 6) -> list[dict]:
    """Return recent monthly spend docs for a vendor from the vendor_spend collection.

    Queries by vendorId only (single-field index), then filters and sorts
    in Python to avoid requiring a composite Firestore index.
    """
    db = get_db()

    docs = (
        db.collection(VENDOR_SPEND_COLLECTION)
        .where("vendorId", "==", vendor_id)
        .stream()
    )

    all_records = []
    for doc in docs:
        data = doc.to_dict()
        record = {}
        for f in SPEND_RETURN_FIELDS:
            if f in data:
                record[f] = data[f]
        all_records.append(record)

    all_records.sort(key=lambda r: r.get("month", ""), reverse=True)
    return all_records[:months]


def query_spend(
    month: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
    vendor_name: str | None = None,
    group_by: str | None = None,
    limit: int = 50,
) -> dict:
    """Query the vendor_spend collection for cross-vendor spend aggregations.

    Supports filtering by month (exact or range), optional vendor name
    substring match, and grouping with sum aggregation on totalAmount.
    Hidden vendors are excluded via a live lookup against the vendors collection.
    """
    db = get_db()
    hidden_ids = get_hidden_vendor_ids()

    ref = db.collection(VENDOR_SPEND_COLLECTION)

    if month:
        ref = ref.where("month", "==", month)
    else:
        if start_month:
            ref = ref.where("month", ">=", start_month)
        if end_month:
            ref = ref.where("month", "<=", end_month)

    docs = ref.stream()

    name_tokens = vendor_name.lower().split() if vendor_name else []

    matched: list[dict] = []
    for doc in docs:
        data = doc.to_dict()
        if data.get("vendorId") in hidden_ids:
            continue
        if name_tokens:
            vname = (data.get("vendorName") or "").lower()
            if not all(t in vname for t in name_tokens):
                continue
        matched.append(data)

    if group_by:
        groups: dict[str, dict] = {}
        for data in matched:
            raw = data.get(group_by)
            key = "Unknown" if raw is None else str(raw)
            if key not in groups:
                groups[key] = {"totalAmount": 0.0, "billCount": 0, "vendorCount": 0}
            groups[key]["totalAmount"] = round(
                groups[key]["totalAmount"] + float(data.get("totalAmount", 0)), 2
            )
            groups[key]["billCount"] += data.get("billCount", 0)
            groups[key]["vendorCount"] += 1
        grand_total = round(sum(g["totalAmount"] for g in groups.values()), 2)
        total_groups = len(groups)
        sorted_groups = dict(
            sorted(groups.items(), key=lambda kv: kv[1]["totalAmount"], reverse=True)[:limit]
        )
        return {
            "groups": sorted_groups,
            "grandTotal": grand_total,
            "totalGroups": total_groups,
            "limitApplied": limit if total_groups > limit else None,
        }

    matched.sort(key=lambda r: r.get("totalAmount", 0), reverse=True)
    results = []
    for data in matched[:limit]:
        results.append({
            "vendorId": data.get("vendorId", ""),
            "vendorName": data.get("vendorName", ""),
            "month": data.get("month", ""),
            "totalAmount": data.get("totalAmount", 0),
            "billCount": data.get("billCount", 0),
        })
    grand_total = round(sum(r["totalAmount"] for r in results), 2)
    return {"results": results, "total": len(matched), "grandTotal": grand_total}


def search_vendors(
    query: str | None = None,
    filters: dict | None = None,
    group_by: str | None = None,
    include_spend: bool = False,
    spend_months: int = 6,
    include_hidden: bool = False,
    limit: int = 50,
) -> dict:
    """Search the vendors collection with optional name prefix, filters, and aggregation.

    - query: prefix match on nameLower (case-insensitive)
    - filters: exact-match field/value pairs (e.g. {"track1099": True})
    - group_by: return counts grouped by this field instead of individual records
    - include_spend: attach recent monthly spend data from vendor_spend collection
    - spend_months: how many months of spend history to include (default 6)
    - include_hidden: if False (default), exclude vendors with hide=True
    """
    db = get_db()
    ref = db.collection(VENDORS_COLLECTION)

    if filters and not query:
        for field, value in filters.items():
            ref = ref.where(field, "==", value)

    docs = ref.stream()

    query_lower = query.lower() if query else None
    query_tokens = query_lower.split() if query_lower else []

    matched: list[dict] = []
    for doc in docs:
        data = doc.to_dict()

        if not include_hidden and data.get("hide"):
            continue

        if query_tokens:
            name_lower = data.get("nameLower", "")
            if not all(token in name_lower for token in query_tokens):
                continue

        if filters and query:
            if not all(data.get(k) == v for k, v in filters.items()):
                continue

        matched.append(data)

    if group_by:
        counts: dict[str, int] = {}
        for data in matched:
            key = str(data.get(group_by, "unknown"))
            counts[key] = counts.get(key, 0) + 1
        return {"counts": counts, "total": len(matched)}

    results = []
    for data in matched[:limit]:
        record = {"id": data.get("id", "")}
        for f in SEARCH_RETURN_FIELDS:
            if f in data:
                record[f] = data[f]
        if include_spend:
            spend_id = data.get("billcomId") or data.get("id", "")
            record["spend"] = get_vendor_spend(spend_id, months=spend_months) if spend_id else []
        results.append(record)

    return {"results": results, "total": len(matched)}
