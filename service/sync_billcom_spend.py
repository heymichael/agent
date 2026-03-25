"""Nightly sync: Bill.com bills → Firestore vendor_spend collection.

Paginates the Bill.com v3 /bills endpoint, aggregates by vendor + month,
denormalizes key vendor metadata from the vendors collection, and writes
monthly spend summaries to the top-level vendor_spend collection.

Idempotent: each run overwrites the spend docs for all months found in
the bill data. Doc IDs are {vendorId}_{YYYY-MM}.

Usage:
    python -m service.sync_billcom_spend
"""

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from google.cloud import firestore

load_dotenv(interpolate=False)

from .billcom_auth import billcom_login

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

VENDOR_SPEND_COLLECTION = "vendor_spend"
VENDORS_COLLECTION = "vendors"

DENORMALIZED_FIELDS = [
    "paymentMethod",
    "billingFrequency",
    "department",
    "owner",
    "track1099",
    "accountType",
    "purpose",
    "spendType",
    "hide",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paginate_bills(base: str, headers: dict) -> list[dict]:
    """Fetch all bills from Bill.com, handling pagination."""
    bills: list[dict] = []
    next_page = None
    page_num = 0

    while True:
        params: dict = {"max": 100}
        if next_page:
            params = {"page": next_page}

        resp = requests.get(
            f"{base}/v3/bills",
            headers=headers,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        bills.extend(results)
        page_num += 1

        if page_num % 10 == 0:
            logger.info("  fetched %d bills so far (%d pages)", len(bills), page_num)

        next_page = data.get("nextPage")
        if not next_page:
            break

    return bills


def _aggregate_bills(bills: list[dict]) -> dict[tuple[str, str], dict]:
    """Group bills by (vendorId, YYYY-MM) and compute totals.

    Returns a dict keyed by (vendorId, month) with values:
        {"totalAmount": float, "billCount": int, "vendorName": str}
    """
    buckets: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"totalAmount": 0.0, "billCount": 0, "vendorName": ""}
    )

    skipped = 0
    for bill in bills:
        vendor_id = bill.get("vendorId")
        due_date = bill.get("dueDate")
        if not vendor_id or not due_date:
            skipped += 1
            continue

        month = due_date[:7]  # "YYYY-MM"
        amount = float(bill.get("amount", 0) or 0)

        key = (vendor_id, month)
        buckets[key]["totalAmount"] = round(buckets[key]["totalAmount"] + amount, 2)
        buckets[key]["billCount"] += 1
        buckets[key]["vendorName"] = bill.get("vendorName", "")

    if skipped:
        logger.warning("Skipped %d bills missing vendorId or dueDate", skipped)

    return dict(buckets)


def _load_vendor_metadata(db: firestore.Client, vendor_ids: set[str]) -> dict[str, dict]:
    """Batch-read vendor docs to grab denormalized fields."""
    metadata: dict[str, dict] = {}

    vendor_id_list = list(vendor_ids)
    for i in range(0, len(vendor_id_list), 100):
        chunk = vendor_id_list[i : i + 100]
        refs = [db.collection(VENDORS_COLLECTION).document(vid) for vid in chunk]
        snapshots = db.get_all(refs)
        for snap in snapshots:
            if snap.exists:
                data = snap.to_dict()
                extracted = {}
                for field in DENORMALIZED_FIELDS:
                    if field in data:
                        extracted[field] = data[field]
                metadata[snap.id] = extracted

    return metadata


def sync():
    """Run the full Bill.com bills → Firestore vendor_spend sync."""
    start = time.time()
    logger.info("Starting Bill.com spend sync")

    base, _, headers = billcom_login()
    logger.info("Logged into Bill.com")

    bills = _paginate_bills(base, headers)
    fetch_elapsed = time.time() - start
    logger.info("Fetched %d bills from Bill.com in %.1fs", len(bills), fetch_elapsed)

    aggregated = _aggregate_bills(bills)
    logger.info("Aggregated into %d vendor-month buckets", len(aggregated))

    vendor_ids = {vid for vid, _ in aggregated.keys()}
    db = firestore.Client()
    vendor_meta = _load_vendor_metadata(db, vendor_ids)
    logger.info("Loaded metadata for %d/%d vendors", len(vendor_meta), len(vendor_ids))

    now = _now_iso()
    written = 0
    batch = db.batch()
    batch_size = 0

    for (vendor_id, month), totals in aggregated.items():
        doc_id = f"{vendor_id}_{month}"
        ref = db.collection(VENDOR_SPEND_COLLECTION).document(doc_id)

        doc = {
            "vendorId": vendor_id,
            "vendorName": totals["vendorName"],
            "month": month,
            "totalAmount": totals["totalAmount"],
            "billCount": totals["billCount"],
            "toolCall": "billcom",
            "lastSyncedAt": now,
        }

        meta = vendor_meta.get(vendor_id, {})
        for field in DENORMALIZED_FIELDS:
            doc[field] = meta.get(field)

        batch.set(ref, doc)
        batch_size += 1
        written += 1

        if batch_size >= 400:
            batch.commit()
            logger.info("Committed batch of %d (total %d/%d)", batch_size, written, len(aggregated))
            batch = db.batch()
            batch_size = 0

    if batch_size > 0:
        batch.commit()
        logger.info("Committed final batch of %d", batch_size)

    elapsed = time.time() - start
    logger.info("Spend sync complete: %d docs written in %.1fs", written, elapsed)


if __name__ == "__main__":
    sync()
