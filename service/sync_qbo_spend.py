"""Nightly sync: QuickBooks Online bills → Postgres vendor_spend_detail + vendor_monthly_spend.

Queries the QBO API for vendors and bills, maps bill line items to canonical
detail columns, upserts granular detail rows into vendor_spend_detail, and
rolls up to vendor_monthly_spend.  Each step is tracked in sync_job_log /
sync_job_step.

Field mapping (QBO Bill → canonical columns):
    TxnDate                          → date       (first of transaction month)
    Line[].AccountBasedExpenseLineDetail.AccountRef.name → category
    Line[].Description               → subcategory
    (not mapped)                     → project    (QBO has no project concept on bills)
    (not mapped)                     → user_email

The sync job also refreshes the OAuth access token on each run and persists
the rotated refresh token.  Token persistence to Secret Manager is handled
separately in production; locally the new token is logged for manual update.

Idempotent: each run overwrites detail and summary rows for all months via
ON CONFLICT ... DO UPDATE.

Usage:
    python -m service.sync_qbo_spend                # full backfill (all history)
    python -m service.sync_qbo_spend --months 3     # rolling 3-month re-sync
"""

import json
import logging
import os
from datetime import date, datetime, timezone

from dotenv import load_dotenv

load_dotenv(interpolate=False)

import requests

from .credentials import load_json_credential
from .pg_client import get_pool
from .qbo_auth import refresh_access_token
from .sync_tracker import SyncTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

JOB_NAME = "qbo-spend-sync"
QBO_SOURCE_SYSTEM = "quickbooks"
QBO_SOURCE_SYSTEM_ID = "quickbooks"

_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
_PROD_BASE = "https://quickbooks.api.intuit.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_base_url() -> str:
    return os.getenv("QBO_API_BASE_URL", _SANDBOX_BASE)


def _qbo_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


def _qbo_query(realm_id: str, access_token: str, entity: str,
               where: str = "", start: int = 1, max_results: int = 1000) -> list[dict]:
    """Execute a paginated QBO query and return all results."""
    base = _get_base_url()
    all_results: list[dict] = []
    page = 0

    while True:
        page += 1
        query = f"SELECT * FROM {entity}"
        if where:
            query += f" WHERE {where}"
        query += f" STARTPOSITION {start} MAXRESULTS {max_results}"

        resp = requests.get(
            f"{base}/v3/company/{realm_id}/query",
            headers=_qbo_headers(access_token),
            params={"query": query},
            timeout=60,
        )
        intuit_tid = resp.headers.get("intuit_tid", "")
        logger.info("QBO query %s page=%d intuit_tid=%s status=%d",
                     entity, page, intuit_tid, resp.status_code)
        resp.raise_for_status()
        data = resp.json()

        query_response = data.get("QueryResponse", {})
        rows = query_response.get(entity, [])
        if not rows:
            break

        all_results.extend(rows)

        if len(rows) < max_results:
            break
        start += max_results

    return all_results


def _fetch_vendors(realm_id: str, access_token: str) -> list[dict]:
    return _qbo_query(realm_id, access_token, "Vendor")


def _fetch_bills(realm_id: str, access_token: str, months: int | None = None) -> list[dict]:
    where = ""
    if months is not None:
        today = datetime.now(timezone.utc).date()
        start = date(today.year, today.month, 1)
        for _ in range(months):
            if start.month == 1:
                start = date(start.year - 1, 12, 1)
            else:
                start = date(start.year, start.month - 1, 1)
        where = f"TxnDate >= '{start.isoformat()}'"
    return _qbo_query(realm_id, access_token, "Bill", where=where)


def _bills_to_detail_rows(bills: list[dict], vendor_name_map: dict[str, str]) -> list[dict]:
    """Map QBO bills to canonical detail rows.

    Each bill line item becomes a separate detail row. Bills without line
    detail (or with only descriptive lines) produce a single row from the
    bill total.
    """
    rows: list[dict] = []
    skipped = 0

    for bill in bills:
        vendor_ref = bill.get("VendorRef", {})
        vendor_id = vendor_ref.get("value")
        vendor_name = vendor_ref.get("name") or vendor_name_map.get(vendor_id, "")
        txn_date = bill.get("TxnDate")

        if not vendor_id or not txn_date:
            skipped += 1
            continue

        parts = txn_date.split("-")
        month_date = date(int(parts[0]), int(parts[1]), 1)

        lines = bill.get("Line", [])
        detail_lines = [l for l in lines if l.get("DetailType") in (
            "AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail",
        )]

        if detail_lines:
            for line in detail_lines:
                amount = float(line.get("Amount", 0))
                detail_type = line.get("DetailType", "")
                category = None
                subcategory = line.get("Description")

                if detail_type == "AccountBasedExpenseLineDetail":
                    acct = line.get("AccountBasedExpenseLineDetail", {})
                    acct_ref = acct.get("AccountRef", {})
                    category = acct_ref.get("name")
                elif detail_type == "ItemBasedExpenseLineDetail":
                    item = line.get("ItemBasedExpenseLineDetail", {})
                    item_ref = item.get("ItemRef", {})
                    category = item_ref.get("name")

                rows.append({
                    "qbo_vendor_id": vendor_id,
                    "vendor_name": vendor_name,
                    "date": month_date,
                    "amount": amount,
                    "category": category,
                    "subcategory": subcategory,
                    "project": None,
                    "metadata": {"bill_id": bill.get("Id"), "detail_type": detail_type},
                })
        else:
            rows.append({
                "qbo_vendor_id": vendor_id,
                "vendor_name": vendor_name,
                "date": month_date,
                "amount": float(bill.get("TotalAmt", 0)),
                "category": None,
                "subcategory": None,
                "project": None,
                "metadata": {"bill_id": bill.get("Id")},
            })

    if skipped:
        logger.warning("Skipped %d bills missing VendorRef or TxnDate", skipped)

    return rows


_ENSURE_VENDOR_SQL = """
    INSERT INTO vendors (source_system, source_system_id, name, synced_at, created_at, modified_at)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_system_id)
    DO UPDATE SET name      = EXCLUDED.name,
                  synced_at = EXCLUDED.synced_at
    RETURNING id
"""

_UPSERT_DETAIL_SQL = """
    INSERT INTO vendor_spend_detail
        (vendor_id, date, amount, category, subcategory, project, user_email, metadata, synced_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (vendor_id, date,
                 COALESCE(category, ''),
                 COALESCE(subcategory, ''),
                 COALESCE(project, ''),
                 COALESCE(user_email, ''))
    DO UPDATE SET amount   = EXCLUDED.amount,
                  metadata = EXCLUDED.metadata,
                  synced_at = EXCLUDED.synced_at
"""

_ROLLUP_SUMMARY_SQL = """
    INSERT INTO vendor_monthly_spend (vendor_id, date, total_amount, bill_count, synced_at)
    SELECT vendor_id, date, SUM(amount), COUNT(*), %s
    FROM vendor_spend_detail
    WHERE vendor_id = %s
    GROUP BY vendor_id, date
    ON CONFLICT (vendor_id, date)
    DO UPDATE SET total_amount = EXCLUDED.total_amount,
                  bill_count   = EXCLUDED.bill_count,
                  synced_at    = EXCLUDED.synced_at
"""

_RECONCILE_SQL = """
    SELECT
        d.date,
        d.detail_total,
        s.total_amount AS summary_total,
        ABS(d.detail_total - s.total_amount) AS diff
    FROM (
        SELECT date, SUM(amount) AS detail_total
        FROM vendor_spend_detail
        WHERE vendor_id = %s
        GROUP BY date
    ) d
    JOIN vendor_monthly_spend s ON s.vendor_id = %s AND s.date = d.date
    WHERE ABS(d.detail_total - s.total_amount) > 0.01
"""


def sync(months: int | None = None):
    """Run the full QBO bills → detail → summary sync.

    Args:
        months: Number of months to look back. None for full backfill.
    """
    pool = get_pool()
    tracker = SyncTracker(JOB_NAME, pool)
    tracker.start(metadata={"months": months or "all"})

    try:
        creds = load_json_credential("VENDOR_QBO_CREDENTIALS")
        realm_id = creds["realm_id"]

        with tracker.step("api_fetch") as s:
            tokens = refresh_access_token()
            access_token = tokens["access_token"]
            new_refresh_token = tokens["refresh_token"]
            logger.info(
                "QBO token refreshed. NEW refresh_token (first 20): %s...",
                new_refresh_token[:20],
            )
            s.metadata["refresh_token_rotated"] = True

            qbo_vendors = _fetch_vendors(realm_id, access_token)
            s.metadata["vendor_count"] = len(qbo_vendors)

            bills = _fetch_bills(realm_id, access_token, months=months)
            s.row_count = len(bills)
            s.metadata["months_param"] = months or "all"

        vendor_name_map = {v["Id"]: v.get("DisplayName", "") for v in qbo_vendors}
        detail_rows = _bills_to_detail_rows(bills, vendor_name_map)
        now = _now()

        with tracker.step("vendor_sync") as s:
            written = 0
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    for v in qbo_vendors:
                        cur.execute(_ENSURE_VENDOR_SQL, (
                            QBO_SOURCE_SYSTEM,
                            v["Id"],
                            v.get("DisplayName", v.get("CompanyName", "")),
                            now, now, now,
                        ))
                        written += 1
            s.row_count = written

        qbo_id_to_uuid: dict[str, str] = {}
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT source_system_id, id::text FROM vendors WHERE source_system = %s",
                (QBO_SOURCE_SYSTEM,),
            ).fetchall()
            qbo_id_to_uuid = {r["source_system_id"]: r["id"] for r in rows}

        with tracker.step("detail_upsert") as s:
            written = 0
            skipped = 0
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    for detail in detail_rows:
                        vendor_uuid = qbo_id_to_uuid.get(detail["qbo_vendor_id"])
                        if not vendor_uuid:
                            skipped += 1
                            continue
                        cur.execute(_UPSERT_DETAIL_SQL, (
                            vendor_uuid,
                            detail["date"],
                            detail["amount"],
                            detail["category"],
                            detail["subcategory"],
                            detail["project"],
                            None,
                            json.dumps(detail["metadata"]) if detail["metadata"] else None,
                            now,
                        ))
                        written += 1
            s.row_count = written
            if skipped:
                s.metadata["skipped_unmapped"] = skipped

        vendor_uuids = set(qbo_id_to_uuid.values())

        with tracker.step("summary_upsert") as s:
            total_rows = 0
            with pool.connection() as conn:
                for vuuid in vendor_uuids:
                    result = conn.execute(_ROLLUP_SUMMARY_SQL, (now, vuuid))
                    total_rows += result.rowcount
            s.row_count = total_rows

        with tracker.step("reconcile") as s:
            all_mismatches: list[dict] = []
            with pool.connection() as conn:
                for vuuid in vendor_uuids:
                    mismatches = conn.execute(
                        _RECONCILE_SQL, (vuuid, vuuid)
                    ).fetchall()
                    for m in mismatches:
                        all_mismatches.append({
                            "vendor_id": vuuid,
                            "month": str(m["date"]),
                            "detail": float(m["detail_total"]),
                            "summary": float(m["summary_total"]),
                            "diff": float(m["diff"]),
                        })

            if all_mismatches:
                s.metadata["mismatches"] = all_mismatches
                raise ValueError(
                    f"Reconciliation failed: {len(all_mismatches)} vendor-month(s) with mismatched totals"
                )
            s.row_count = 0
            s.metadata["result"] = "all vendor-months match"

        tracker.finish()

    except Exception as exc:
        tracker.fail(str(exc))
        raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync QBO billing data")
    parser.add_argument("--months", type=int, default=None,
                        help="Months to look back (default: full history)")
    args = parser.parse_args()
    sync(months=args.months)
