"""Nightly sync: AWS Cost Explorer → Firestore vendor_spend collection.

Calls the AWS Cost Explorer API to fetch monthly spend for the last 12
months, denormalizes vendor metadata from the vendors collection, and
writes monthly spend summaries to the vendor_spend collection.

Idempotent: each run overwrites the spend docs for all months returned
by the API. Doc IDs are aws_{YYYY-MM}.

Usage:
    python -m service.sync_aws_spend
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta

import boto3
from dotenv import load_dotenv
from google.cloud import firestore

load_dotenv(interpolate=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

VENDOR_SPEND_COLLECTION = "vendor_spend"
VENDORS_COLLECTION = "vendors"
AWS_VENDOR_DOC_ID = "aws"

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


def _fetch_monthly_costs(months: int = 12) -> list[dict]:
    """Fetch monthly unblended costs from AWS Cost Explorer.

    Returns a list of dicts: [{"month": "YYYY-MM", "totalAmount": float}, ...]
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
        month = period["TimePeriod"]["Start"][:7]
        amount = round(float(period["Total"]["UnblendedCost"]["Amount"]), 2)
        results.append({"month": month, "totalAmount": amount})

    return results


def _load_vendor_metadata(db: firestore.Client) -> dict:
    """Read denormalized fields from the AWS vendor doc."""
    snap = db.collection(VENDORS_COLLECTION).document(AWS_VENDOR_DOC_ID).get()
    if not snap.exists:
        logger.warning("Vendor doc '%s' not found, skipping metadata", AWS_VENDOR_DOC_ID)
        return {}

    data = snap.to_dict()
    return {field: data.get(field) for field in DENORMALIZED_FIELDS}


def sync():
    """Run the full AWS Cost Explorer → Firestore vendor_spend sync."""
    start = time.time()
    logger.info("Starting AWS spend sync")

    monthly_costs = _fetch_monthly_costs(months=12)
    logger.info("Fetched %d months of cost data from AWS", len(monthly_costs))

    db = firestore.Client()

    vendor_ref = db.collection(VENDORS_COLLECTION).document(AWS_VENDOR_DOC_ID)
    vendor_ref.set({"nameLower": "aws-api"}, merge=True)

    meta = _load_vendor_metadata(db)

    now = _now_iso()
    batch = db.batch()

    for cost in monthly_costs:
        doc_id = f"{AWS_VENDOR_DOC_ID}_{cost['month']}"
        ref = db.collection(VENDOR_SPEND_COLLECTION).document(doc_id)

        doc = {
            "vendorId": AWS_VENDOR_DOC_ID,
            "vendorName": "AWS-API",
            "month": cost["month"],
            "totalAmount": cost["totalAmount"],
            "billCount": 1,
            "toolCall": "aws-ce",
            "lastSyncedAt": now,
        }

        for field in DENORMALIZED_FIELDS:
            doc[field] = meta.get(field)

        batch.set(ref, doc)

    batch.commit()

    elapsed = time.time() - start
    logger.info("AWS spend sync complete: %d docs written in %.1fs", len(monthly_costs), elapsed)


if __name__ == "__main__":
    sync()
