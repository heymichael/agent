"""Nightly sync: GCP BigQuery billing export → Postgres vendor_spend_detail + vendor_monthly_spend.

Queries the BigQuery billing export table in arcade-ai-prod to fetch monthly
spend aggregated by service (category), SKU (subcategory), and project, then
upserts granular detail rows into vendor_spend_detail and rolls up to
vendor_monthly_spend.  Each step is tracked in sync_job_log / sync_job_step.

Field mapping (BigQuery → canonical columns):
    invoice.month       → date       (first of invoice month)
    service.description → category   (e.g. "Compute Engine")
    sku.description     → subcategory (e.g. "N1 Predefined Instance Core running in Americas")
    project.id          → project    (e.g. "arcade-ai-prod")

Idempotent: each run overwrites detail and summary rows for all months via
ON CONFLICT ... DO UPDATE.

Usage:
    python -m service.sync_gcp_spend                # full backfill (all history)
    python -m service.sync_gcp_spend --months 3     # rolling 3-month re-sync
"""

import json
import logging
import os
from datetime import date, datetime, timezone

from dotenv import load_dotenv

load_dotenv(interpolate=False)

from google.cloud import bigquery
from google.oauth2 import service_account

from .pg_client import get_pool
from .sync_tracker import SyncTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

JOB_NAME = "gcp-spend-sync"
GCP_SOURCE_SYSTEM = "gcp-billing"
GCP_SOURCE_SYSTEM_ID = "gcp-billing"
GCP_VENDOR_NAME = "Google Cloud"

BILLING_TABLE = "arcade-ai-prod.arcade_gcp_billing_export.gcp_billing_export_resource_v1_*"

_BILLING_QUERY_TEMPLATE = """
SELECT
  CONCAT(SUBSTR(invoice.month, 1, 4), '-', SUBSTR(invoice.month, 5, 2), '-01') AS month,
  service.description AS category,
  sku.description AS subcategory,
  project.id AS project_id,
  ROUND(SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 2) AS net_cost
FROM `{table}`
WHERE invoice.month >= @start_month
GROUP BY month, category, subcategory, project_id
HAVING net_cost != 0
ORDER BY month, net_cost DESC
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_bq_client() -> bigquery.Client:
    creds_json = json.loads(os.environ["VENDOR_GCP_BILLING_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(creds_json)
    return bigquery.Client(credentials=creds, project=creds.project_id)


def _fetch_monthly_costs(months: int | None = None) -> list[dict]:
    """Fetch monthly costs from BigQuery billing export grouped by service, SKU, and project.

    Args:
        months: Number of months to look back. None means full history.

    Returns a list of dicts:
        [{"date": date, "category": str, "subcategory": str, "project": str,
          "amount": float, "metadata": dict}, ...]
    """
    client = _build_bq_client()

    if months is not None:
        today = datetime.now(timezone.utc).date()
        start = date(today.year, today.month, 1)
        for _ in range(months):
            if start.month == 1:
                start = date(start.year - 1, 12, 1)
            else:
                start = date(start.year, start.month - 1, 1)
    else:
        start = date(2020, 1, 1)

    query = _BILLING_QUERY_TEMPLATE.replace("{table}", BILLING_TABLE)
    start_month = f"{start.year}{start.month:02d}"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_month", "STRING", start_month),
        ]
    )

    rows = client.query(query, job_config=job_config)

    results = []
    for row in rows:
        parts = row.month.split("-")
        month_date = date(int(parts[0]), int(parts[1]), 1)
        results.append({
            "date": month_date,
            "category": row.category,
            "subcategory": row.subcategory,
            "project": row.project_id,
            "amount": float(row.net_cost),
            "metadata": None,
        })

    return results


_ENSURE_VENDOR_SQL = """
    INSERT INTO vendors (source_system, source_system_id, name, synced_at, created_at, modified_at)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_system_id)
    DO UPDATE SET synced_at = EXCLUDED.synced_at
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
    """Run the full GCP BigQuery billing → detail → summary sync.

    Args:
        months: Number of months to look back. None for full backfill.
    """
    pool = get_pool()
    tracker = SyncTracker(JOB_NAME, pool)
    tracker.start(metadata={"months": months or "all"})

    try:
        with tracker.step("api_fetch") as s:
            detail_rows = _fetch_monthly_costs(months=months)
            s.row_count = len(detail_rows)
            s.metadata["months_covered"] = len({r["date"] for r in detail_rows})

        now = _now()

        with pool.connection() as conn:
            row = conn.execute(_ENSURE_VENDOR_SQL, (
                GCP_SOURCE_SYSTEM,
                GCP_SOURCE_SYSTEM_ID,
                GCP_VENDOR_NAME,
                now, now, now,
            )).fetchone()
            vendor_uuid = str(row["id"])

        with tracker.step("detail_upsert") as s:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    for detail in detail_rows:
                        cur.execute(_UPSERT_DETAIL_SQL, (
                            vendor_uuid,
                            detail["date"],
                            detail["amount"],
                            detail["category"],
                            detail["subcategory"],
                            detail["project"],
                            None,
                            detail["metadata"],
                            now,
                        ))
            s.row_count = len(detail_rows)

        with tracker.step("summary_upsert") as s:
            with pool.connection() as conn:
                result = conn.execute(_ROLLUP_SUMMARY_SQL, (now, vendor_uuid))
                s.row_count = result.rowcount

        with tracker.step("reconcile") as s:
            with pool.connection() as conn:
                mismatches = conn.execute(
                    _RECONCILE_SQL, (vendor_uuid, vendor_uuid)
                ).fetchall()

            if mismatches:
                diffs = [
                    {"month": str(m["date"]), "detail": float(m["detail_total"]),
                     "summary": float(m["summary_total"]), "diff": float(m["diff"])}
                    for m in mismatches
                ]
                s.metadata["mismatches"] = diffs
                raise ValueError(f"Reconciliation failed: {len(mismatches)} month(s) with mismatched totals")
            s.row_count = 0
            s.metadata["result"] = "all months match"

        tracker.finish()

    except Exception as exc:
        tracker.fail(str(exc))
        raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync GCP billing data from BigQuery")
    parser.add_argument("--months", type=int, default=None,
                        help="Months to look back (default: full history)")
    args = parser.parse_args()
    sync(months=args.months)
