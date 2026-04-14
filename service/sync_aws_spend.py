"""Nightly sync: AWS Cost Explorer → Postgres vendor_spend_detail + vendor_monthly_spend.

Calls the AWS Cost Explorer API to fetch monthly spend for the last 12
months grouped by Service and UsageType, ensures the 'aws' vendor row
exists, upserts granular detail rows into vendor_spend_detail, then rolls
up to vendor_monthly_spend.  Each step is tracked in sync_job_log /
sync_job_step for observability.

Field mapping:
    Service   → category   (e.g. "Amazon Elastic Compute Cloud")
    UsageType → subcategory (e.g. "USW2-BoxUsage:t3.medium")

Idempotent: each run overwrites detail and summary rows for all months
returned by the API via ON CONFLICT ... DO UPDATE.

Usage:
    python -m service.sync_aws_spend
"""

import json
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal

import boto3
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

load_dotenv(interpolate=False)

from .credentials import load_json_credential
from .pg_client import get_pool
from .sync_tracker import SyncTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

JOB_NAME = "aws-spend-sync"
AWS_SOURCE_SYSTEM = "aws-ce"
AWS_SOURCE_SYSTEM_ID = "aws"
AWS_VENDOR_NAME = "AWS"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_monthly_costs_grouped(months: int = 12) -> list[dict]:
    """Fetch monthly costs from AWS Cost Explorer grouped by Service and UsageType.

    Returns a list of dicts:
        [{"date": date, "category": str, "subcategory": str, "amount": float}, ...]
    """
    creds = load_json_credential("VENDOR_AWS_BILLING_CREDENTIALS")
    ce = boto3.client(
        "ce",
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        region_name=creds.get("region", "us-east-1"),
    )

    today = datetime.now(timezone.utc).date()
    start = (today - relativedelta(months=months)).replace(day=1)
    end = today.replace(day=1) + relativedelta(months=1)

    results = []
    next_token = None

    while True:
        kwargs = dict(
            TimePeriod={
                "Start": start.strftime("%Y-%m-%d"),
                "End": end.strftime("%Y-%m-%d"),
            },
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
        )
        if next_token:
            kwargs["NextPageToken"] = next_token

        response = ce.get_cost_and_usage(**kwargs)

        for period in response["ResultsByTime"]:
            period_start = period["TimePeriod"]["Start"]
            parts = period_start.split("-")
            month_date = date(int(parts[0]), int(parts[1]), 1)

            for group in period.get("Groups", []):
                amount = round(float(group["Metrics"]["UnblendedCost"]["Amount"]), 2)
                if amount == 0:
                    continue
                results.append({
                    "date": month_date,
                    "category": group["Keys"][0],
                    "subcategory": group["Keys"][1],
                    "amount": amount,
                })

        next_token = response.get("NextPageToken")
        if not next_token:
            break

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


def sync():
    """Run the full AWS Cost Explorer → detail → summary sync."""
    pool = get_pool()
    tracker = SyncTracker(JOB_NAME, pool)
    tracker.start(metadata={"months": 12})

    try:
        # --- api_fetch ---
        with tracker.step("api_fetch") as s:
            detail_rows = _fetch_monthly_costs_grouped(months=12)
            s.row_count = len(detail_rows)
            s.metadata["months_covered"] = len({r["date"] for r in detail_rows})

        now = _now()

        with pool.connection() as conn:
            row = conn.execute(_ENSURE_VENDOR_SQL, (
                AWS_SOURCE_SYSTEM,
                AWS_SOURCE_SYSTEM_ID,
                AWS_VENDOR_NAME,
                now, now, now,
            )).fetchone()
            vendor_uuid = str(row["id"])

        # --- detail_upsert ---
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
                            None,
                            None,
                            "{}",
                            now,
                        ))
            s.row_count = len(detail_rows)

        # --- summary_upsert ---
        with tracker.step("summary_upsert") as s:
            with pool.connection() as conn:
                result = conn.execute(_ROLLUP_SUMMARY_SQL, (now, vendor_uuid))
                s.row_count = result.rowcount

        # --- reconcile ---
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
    sync()
