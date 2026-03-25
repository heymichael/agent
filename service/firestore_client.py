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


SEARCH_RETURN_FIELDS = [
    "name", "billcomId", "toolCall", "paymentMethod", "accountType",
    "track1099", "hide", "owner", "secondaryOwner", "department", "purpose",
    "spendType", "billingFrequency", "contractStartDate", "contractEndDate",
    "contractLengthMonths", "autoRenew", "renewalRate", "renewalNoticeDays",
    "terminationTerms", "lastSyncedAt",
]

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
        return {"groups": groups, "grandTotal": grand_total, "totalVendors": len(matched)}

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
