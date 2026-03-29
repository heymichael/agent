#!/usr/bin/env python3
"""Bulk-update vendor department fields from a CSV file.

Usage:
    python scripts/seed_departments.py path/to/departments.csv

CSV format (tab or comma separated, with header row):
    id,department
    00902ABC...,Engineering
    00902DEF...,Marketing

Requires GCP Application Default Credentials for Firestore access.
"""
import csv
import sys
from datetime import datetime, timezone

from google.cloud import firestore

VENDORS_COLLECTION = "vendors"


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/seed_departments.py <csv_file>")
        sys.exit(1)

    csv_path = sys.argv[1]
    db = firestore.Client()

    with open(csv_path, newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        reader = csv.DictReader(f, dialect=dialect)

        if "id" not in reader.fieldnames or "department" not in reader.fieldnames:
            print(f"ERROR: CSV must have 'id' and 'department' columns. Found: {reader.fieldnames}")
            sys.exit(1)

        rows = [(r["id"].strip(), r["department"].strip()) for r in reader if r["id"].strip() and r["department"].strip()]

    print(f"Loaded {len(rows)} vendor-department mappings from {csv_path}")

    batch = db.batch()
    batch_count = 0
    updated = 0
    skipped = 0
    not_found = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for vendor_id, department in rows:
        ref = db.collection(VENDORS_COLLECTION).document(vendor_id)
        snap = ref.get()
        if not snap.exists:
            print(f"  SKIP (not found): {vendor_id}")
            not_found += 1
            continue

        current = snap.to_dict().get("department")
        if current == department:
            skipped += 1
            continue

        batch.update(ref, {"department": department, "modified_at": now})
        batch_count += 1
        updated += 1

        if batch_count >= 400:
            batch.commit()
            print(f"  Committed batch of {batch_count}")
            batch = db.batch()
            batch_count = 0

    if batch_count > 0:
        batch.commit()
        print(f"  Committed final batch of {batch_count}")

    print(f"\nDone: {updated} updated, {skipped} already correct, {not_found} not found")


if __name__ == "__main__":
    main()
