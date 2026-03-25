import re
from datetime import datetime, timezone
from google.cloud import firestore

_db: firestore.Client | None = None

VENDORS_COLLECTION = "vendors"


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


def list_users_by_role(role: str) -> list[dict]:
    """Return users whose roles array contains the given role."""
    db = get_db()
    docs = (
        db.collection("users")
        .where("roles", "array_contains", role)
        .stream()
    )
    results = []
    for d in docs:
        data = d.to_dict()
        results.append({
            "email": d.id,
            "firstName": data.get("first_name", ""),
            "lastName": data.get("last_name", ""),
        })
    return results


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


SEARCH_RETURN_FIELDS = [
    "name", "billcomId", "toolCall", "paymentMethod", "accountType",
    "track1099", "owner", "secondaryOwner", "department", "purpose",
    "spendType", "billingFrequency", "contractStartDate", "contractEndDate",
    "contractLengthMonths", "autoRenew", "renewalRate", "renewalNoticeDays",
    "terminationTerms", "lastSyncedAt",
]


def search_vendors(
    query: str | None = None,
    filters: dict | None = None,
    group_by: str | None = None,
    limit: int = 50,
) -> dict:
    """Search the vendors collection with optional name prefix, filters, and aggregation.

    - query: prefix match on nameLower (case-insensitive)
    - filters: exact-match field/value pairs (e.g. {"track1099": True})
    - group_by: return counts grouped by this field instead of individual records
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
        results.append(record)

    return {"results": results, "total": len(matched)}
