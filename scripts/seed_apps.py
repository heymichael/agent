#!/usr/bin/env python3
"""Seed the Firestore `apps` collection from the current hardcoded catalog.

Usage:
    cd agent
    source .venv/bin/activate
    python scripts/seed_apps.py [--dry-run]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(interpolate=False)

from google.cloud import firestore

APPS = [
    {
        "id": "card",
        "label": "Card",
        "path": "/card/",
        "type": "app",
        "granting_roles": ["haderach_user"],
        "sort_order": 1,
    },
    {
        "id": "stocks",
        "label": "Commodities",
        "path": "/stocks/",
        "type": "app",
        "granting_roles": ["user", "admin"],
        "sort_order": 2,
    },
    {
        "id": "vendors",
        "label": "Vendors",
        "path": "/vendors/",
        "type": "app",
        "granting_roles": ["user", "admin"],
        "sort_order": 3,
    },
    {
        "id": "system_administration",
        "label": "System",
        "path": "/admin/system/",
        "type": "admin",
        "granting_roles": ["admin"],
        "sort_order": 1,
    },
    {
        "id": "vendor_administration",
        "label": "Vendors",
        "path": "/admin/vendors/",
        "type": "admin",
        "granting_roles": ["finance_admin"],
        "sort_order": 2,
    },
]


def main():
    parser = argparse.ArgumentParser(description="Seed Firestore apps collection")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    args = parser.parse_args()

    db = firestore.Client()
    collection = db.collection("apps")

    for app in APPS:
        app_id = app["id"]
        doc_data = {k: v for k, v in app.items() if k != "id"}

        if args.dry_run:
            print(f"[dry-run] Would write apps/{app_id}: {doc_data}")
            continue

        ref = collection.document(app_id)
        existing = ref.get()
        if existing.exists:
            print(f"  exists  apps/{app_id} — skipping")
        else:
            ref.set(doc_data)
            print(f"  created apps/{app_id}")

    print("\nDone." if not args.dry_run else "\nDry run complete — no writes made.")


if __name__ == "__main__":
    main()
