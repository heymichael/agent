"""Nightly sync: AWS Cost Explorer → Postgres vendor_monthly_spend table.

Calls the AWS Cost Explorer API to fetch monthly spend for the last 12
months, ensures the 'aws' vendor row exists, and upserts monthly spend
summaries into vendor_monthly_spend.

No denormalization — vendor metadata lives only on the vendors table.

Idempotent: each run overwrites spend for all months returned by the API
via ON CONFLICT ... DO UPDATE.

Usage:
    python -m service.sync_aws_spend
"""

import json
import logging
import os
import time
from datetime import date, datetime, timezone

import boto3
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

load_dotenv(interpolate=False)

from .pg_client import get_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

AWS_SOURCE_SYSTEM = "aws-ce"
AWS_SOURCE_SYSTEM_ID = "aws"
AWS_VENDOR_NAME = "AWS"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_monthly_costs(months: int = 12) -> list[dict]:
    """Fetch monthly unblended costs from AWS Cost Explorer.

    Returns a list of dicts: [{"date": date, "totalAmount": float}, ...]
    """
    creds = json.loads(os.environ["VENDOR_AWS_BILLING_CREDENTIALS"])
    ce = boto3.client(
        "ce",
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        region_name=creds.get("region", "us-east-1"),
    )

    today = datetime.now(timezone.utc).date()
    start = (today - relativedelta(months=months)).replace(day=1)
    end = today.replace(day=1) + relativedelta(months=1)

    response = ce.get_cost_and_usage(
        TimePeriod={
            "Start": start.strftime("%Y-%m-%d"),
            "End": end.strftime("%Y-%m-%d"),
        },
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
    )

    results = []
    for period in response["ResultsByTime"]:
        period_start = period["TimePeriod"]["Start"]
        parts = period_start.split("-")
        month_date = date(int(parts[0]), int(parts[1]), 1)
        amount = round(float(period["Total"]["UnblendedCost"]["Amount"]), 2)
        results.append({"date": month_date, "totalAmount": amount})

    return results


_ENSURE_VENDOR_SQL = """
    INSERT INTO vendors (source_system, source_system_id, name, synced_at, created_at, modified_at)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_system_id)
    DO UPDATE SET synced_at = EXCLUDED.synced_at
    RETURNING id
"""

_UPSERT_SPEND_SQL = """
    INSERT INTO vendor_monthly_spend (vendor_id, date, total_amount, bill_count, synced_at)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (vendor_id, date)
    DO UPDATE SET total_amount = EXCLUDED.total_amount,
                  bill_count   = EXCLUDED.bill_count,
                  synced_at    = EXCLUDED.synced_at
"""


def sync():
    """Run the full AWS Cost Explorer → Postgres vendor_monthly_spend sync."""
    start = time.time()
    logger.info("Starting AWS spend sync")

    monthly_costs = _fetch_monthly_costs(months=12)
    logger.info("Fetched %d months of cost data from AWS", len(monthly_costs))

    pool = get_pool()
    now = _now()

    with pool.connection() as conn:
        row = conn.execute(_ENSURE_VENDOR_SQL, (
            AWS_SOURCE_SYSTEM,
            AWS_SOURCE_SYSTEM_ID,
            AWS_VENDOR_NAME,
            now, now, now,
        )).fetchone()
        vendor_uuid = str(row["id"])
        logger.info("AWS vendor row: %s", vendor_uuid)

        with conn.cursor() as cur:
            for cost in monthly_costs:
                cur.execute(_UPSERT_SPEND_SQL, (
                    vendor_uuid,
                    cost["date"],
                    cost["totalAmount"],
                    1,
                    now,
                ))

    elapsed = time.time() - start
    logger.info("AWS spend sync complete: %d rows upserted in %.1fs", len(monthly_costs), elapsed)


if __name__ == "__main__":
    sync()
