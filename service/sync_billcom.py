"""Nightly sync: Bill.com vendors → Firestore vendors collection.

Paginates the Bill.com v3 /vendors endpoint and merge-writes each vendor
into Firestore. Only synced fields are touched — app-managed and contract
fields are preserved via merge=True.

Usage:
    python -m service.sync_billcom
"""

import logging
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from google.cloud import firestore

load_dotenv(interpolate=False)

from .billcom_auth import billcom_login

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

VENDORS_COLLECTION = "vendors"

APP_MANAGED_FIELDS = [
    "owner",
    "secondaryOwner",
    "department",
    "purpose",
    "spendType",
    "aliases",
]

CONTRACT_FIELDS = [
    "contractStartDate",
    "contractEndDate",
    "contractLengthMonths",
    "autoRenew",
    "renewalRate",
    "renewalNoticeDays",
    "billingFrequency",
    "terminationTerms",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paginate_vendors(base: str, headers: dict) -> list[dict]:
    """Fetch all vendors from Bill.com, handling pagination correctly."""
    vendors: list[dict] = []
    next_page = None

    while True:
        params: dict = {"max": 100}
        if next_page:
            params = {"page": next_page}

        resp = requests.get(
            f"{base}/v3/vendors",
            headers=headers,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        vendors.extend(data["results"])
        next_page = data.get("nextPage")
        if not next_page:
            break

    return vendors


def _extract_synced_fields(vendor: dict) -> dict:
    """Extract the fields we sync from a Bill.com vendor object."""
    return {
        "id": vendor["id"],
        "name": vendor.get("name", ""),
        "billcomId": vendor["id"],
        "nameLower": vendor.get("name", "").lower(),
        "paymentMethod": vendor.get("paymentInformation", {}).get("payByType"),
        "accountType": vendor.get("accountType"),
        "track1099": vendor.get("additionalInfo", {}).get("track1099", False),
        "toolCall": "billcom",
        "lastSyncedAt": _now_iso(),
    }


def sync():
    """Run the full Bill.com → Firestore vendor sync."""
    start = time.time()
    logger.info("Starting Bill.com vendor sync")

    base, _, headers = billcom_login()
    logger.info("Logged into Bill.com")

    vendors = _paginate_vendors(base, headers)
    logger.info("Fetched %d vendors from Bill.com in %.1fs", len(vendors), time.time() - start)

    db = firestore.Client()
    written = 0
    batch = db.batch()
    batch_size = 0

    for vendor in vendors:
        doc_id = vendor["id"]
        ref = db.collection(VENDORS_COLLECTION).document(doc_id)
        synced = _extract_synced_fields(vendor)
        batch.set(ref, synced, merge=True)
        batch_size += 1
        written += 1

        if batch_size >= 400:
            batch.commit()
            logger.info("Committed batch of %d (total %d/%d)", batch_size, written, len(vendors))
            batch = db.batch()
            batch_size = 0

    if batch_size > 0:
        batch.commit()
        logger.info("Committed final batch of %d", batch_size)

    elapsed = time.time() - start
    logger.info("Sync complete: %d vendors written in %.1fs", written, elapsed)


if __name__ == "__main__":
    sync()
