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
