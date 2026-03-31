"""Nightly sync: Bill.com vendors → Postgres vendors table.

Paginates the Bill.com v3 /vendors endpoint and upserts each vendor
into Postgres. Only synced fields are touched — app-managed and contract
fields are preserved via ON CONFLICT ... DO UPDATE on synced columns only.
Each step is tracked in sync_job_log / sync_job_step.

Usage:
    python -m service.sync_billcom
"""

import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv(interpolate=False)

from .billcom_auth import billcom_login
from .pg_client import get_pool
from .sync_tracker import SyncTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

JOB_NAME = "billcom-vendor-sync"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _paginate_vendors(base: str, headers: dict) -> list[dict]:
    """Fetch all vendors from Bill.com, handling pagination."""
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


_UPSERT_SQL = """
    INSERT INTO vendors (source_system, source_system_id, name, payment_method,
                         account_type, track_1099, synced_at, created_at, modified_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_system_id)
    DO UPDATE SET name           = EXCLUDED.name,
                  payment_method = EXCLUDED.payment_method,
                  account_type   = EXCLUDED.account_type,
                  track_1099     = EXCLUDED.track_1099,
                  synced_at      = EXCLUDED.synced_at,
                  modified_at    = EXCLUDED.modified_at
"""


def sync():
    """Run the full Bill.com → Postgres vendor sync."""
    pool = get_pool()
    tracker = SyncTracker(JOB_NAME, pool)
    tracker.start()

    try:
        with tracker.step("api_fetch") as s:
            base, _, headers = billcom_login()
            vendors = _paginate_vendors(base, headers)
            s.row_count = len(vendors)

        now = _now()

        with tracker.step("vendor_sync") as s:
            written = 0
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    for vendor in vendors:
                        payment_info = vendor.get("paymentInformation") or {}
                        cur.execute(_UPSERT_SQL, (
                            "billcom",
                            vendor["id"],
                            vendor.get("name", ""),
                            payment_info.get("payByType"),
                            vendor.get("accountType"),
                            vendor.get("additionalInfo", {}).get("track1099", False),
                            now,
                            now,
                            now,
                        ))
                        written += 1
            s.row_count = written

        tracker.finish()

    except Exception as exc:
        tracker.fail(str(exc))
        raise


if __name__ == "__main__":
    sync()
