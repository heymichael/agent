"""Nightly sync: Bill.com bills → Postgres vendor_monthly_spend table.

Paginates the Bill.com v3 /bills endpoint, aggregates by vendor + month,
resolves Bill.com vendor IDs to internal UUIDs via the vendors table, and
upserts monthly spend summaries.  Each step is tracked in sync_job_log /
sync_job_step.

No denormalization — vendor metadata lives only on the vendors table and
is JOINed at query time.

Idempotent: each run overwrites spend for all vendor-month pairs found
in the bill data via ON CONFLICT ... DO UPDATE.

Usage:
    python -m service.sync_billcom_spend
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv(interpolate=False)

from .billcom_auth import billcom_login
from .pg_client import get_pool
from .sync_tracker import SyncTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

JOB_NAME = "billcom-spend-sync"


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    """Group bills by (Bill.com vendorId, YYYY-MM) and compute totals.

    Returns a dict keyed by (billcom_vendor_id, month_str) with values:
        {"totalAmount": float, "billCount": int}
    """
    buckets: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"totalAmount": 0.0, "billCount": 0}
    )

    skipped = 0
    for bill in bills:
        vendor_id = bill.get("vendorId")
        invoice = bill.get("invoice") or {}
        invoice_date = invoice.get("invoiceDate")
        if not vendor_id or not invoice_date:
            skipped += 1
            continue

        month = invoice_date[:7]  # "YYYY-MM"
        amount = float(bill.get("amount", 0) or 0)

        key = (vendor_id, month)
        buckets[key]["totalAmount"] = round(buckets[key]["totalAmount"] + amount, 2)
        buckets[key]["billCount"] += 1

    if skipped:
        logger.warning("Skipped %d bills missing vendorId or invoiceDate", skipped)

    return dict(buckets)


def _resolve_vendor_uuids(conn, billcom_ids: set[str]) -> dict[str, str]:
    """Map Bill.com vendor IDs → internal UUIDs. Returns {billcom_id: uuid}."""
    if not billcom_ids:
        return {}

    rows = conn.execute(
        """SELECT source_system_id, id::text
           FROM vendors
           WHERE source_system = 'billcom'
             AND source_system_id = ANY(%s)""",
        (list(billcom_ids),),
    ).fetchall()
    return {r["source_system_id"]: r["id"] for r in rows}


_UPSERT_SPEND_SQL = """
    INSERT INTO vendor_monthly_spend (vendor_id, date, total_amount, bill_count, synced_at)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (vendor_id, date)
    DO UPDATE SET total_amount = EXCLUDED.total_amount,
                  bill_count   = EXCLUDED.bill_count,
                  synced_at    = EXCLUDED.synced_at
"""


def _month_to_date(month_str: str) -> date:
    """Convert 'YYYY-MM' to a date on the first of that month."""
    parts = month_str.split("-")
    return date(int(parts[0]), int(parts[1]), 1)


def sync():
    """Run the full Bill.com bills → Postgres vendor_monthly_spend sync."""
    pool = get_pool()
    tracker = SyncTracker(JOB_NAME, pool)
    tracker.start()

    try:
        with tracker.step("api_fetch") as s:
            base, _, headers = billcom_login()
            bills = _paginate_bills(base, headers)
            s.row_count = len(bills)

        aggregated = _aggregate_bills(bills)
        billcom_ids = {vid for vid, _ in aggregated.keys()}
        now = _now()

        with tracker.step("summary_upsert") as s:
            written = 0
            skipped_unmapped = 0

            with pool.connection() as conn:
                uuid_map = _resolve_vendor_uuids(conn, billcom_ids)
                unmapped = billcom_ids - set(uuid_map.keys())
                if unmapped:
                    logger.warning(
                        "%d Bill.com vendor IDs have no match in vendors table (run sync_billcom first): %s",
                        len(unmapped),
                        list(unmapped)[:10],
                    )

                with conn.cursor() as cur:
                    for (billcom_id, month_str), totals in aggregated.items():
                        internal_id = uuid_map.get(billcom_id)
                        if not internal_id:
                            skipped_unmapped += 1
                            continue

                        cur.execute(_UPSERT_SPEND_SQL, (
                            internal_id,
                            _month_to_date(month_str),
                            totals["totalAmount"],
                            totals["billCount"],
                            now,
                        ))
                        written += 1

            s.row_count = written
            if skipped_unmapped:
                s.metadata["skipped_unmapped"] = skipped_unmapped

        tracker.finish()

    except Exception as exc:
        tracker.fail(str(exc))
        raise


if __name__ == "__main__":
    sync()
