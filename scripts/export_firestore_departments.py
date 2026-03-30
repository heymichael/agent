#!/usr/bin/env python3
"""Export vendor department mappings from Firestore to CSV.

Reads the Firestore `vendors` collection and writes a CSV of
(id, department) pairs where `id` is the Bill.com vendor ID
(billcomId) and `department` is the assigned department string.

The output CSV can be fed directly to seed_departments.py:

    python scripts/export_firestore_departments.py > departments.csv
    DATABASE_URL="postgresql://..." python scripts/seed_departments.py departments.csv

Prerequisites:
    pip install firebase-admin
    gcloud auth application-default login
"""

import csv
import sys

import firebase_admin
from firebase_admin import credentials, firestore

PROJECT_ID = "haderach-ai"


def main():
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {"projectId": PROJECT_ID})
    db = firestore.client()

    writer = csv.writer(sys.stdout)
    writer.writerow(["id", "department"])

    count = 0
    skipped = 0

    for doc in db.collection("vendors").stream():
        data = doc.to_dict()
        department = (data.get("department") or "").strip()
        billcom_id = (data.get("billcomId") or "").strip()

        if not department:
            skipped += 1
            continue

        if not billcom_id:
            name = data.get("name", doc.id)
            print(f"  WARN: vendor '{name}' has department '{department}' but no billcomId — skipping", file=sys.stderr)
            skipped += 1
            continue

        writer.writerow([billcom_id, department])
        count += 1

    print(f"\nExported {count} department mappings ({skipped} skipped)", file=sys.stderr)


if __name__ == "__main__":
    main()
