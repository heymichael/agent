#!/usr/bin/env python3
"""Delete legacy Firestore collections after Postgres migration.

This is a one-time cleanup script. All data has been migrated to Postgres:

    Firestore collection    ->  Postgres table
    ----------------------      --------------
    allowlists              ->  app_granting_roles + user_roles
    apps                    ->  apps
    users                   ->  users
    vendors                 ->  vendors
    vendor_spend            ->  vendor_spend

After running this script, Firestore will be empty. Only Firebase Auth
remains in use (for token verification).

Usage:
    # Dry run — show what would be deleted
    python scripts/cleanup_firestore_legacy.py

    # Actually delete
    python scripts/cleanup_firestore_legacy.py --confirm

Prerequisites:
    pip install firebase-admin
    gcloud auth application-default login

Run date: 2026-04-23 (Task 257 — Retire card app)
"""

import argparse
import sys

import firebase_admin
from firebase_admin import credentials, firestore

PROJECT_ID = "haderach-ai"

LEGACY_COLLECTIONS = [
    "allowlists",
    "apps",
    "users",
    "vendors",
    "vendor_spend",
]


def delete_collection(db, collection_name: str, batch_size: int = 100) -> int:
    """Delete all documents in a collection. Returns count deleted."""
    coll_ref = db.collection(collection_name)
    deleted = 0

    while True:
        docs = list(coll_ref.limit(batch_size).stream())
        if not docs:
            break

        batch = db.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()
        deleted += len(docs)
        print(f"  ... deleted {deleted} documents from {collection_name}", file=sys.stderr)

    return deleted


def main():
    parser = argparse.ArgumentParser(description="Delete legacy Firestore collections")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete (without this flag, only shows what would be deleted)",
    )
    args = parser.parse_args()

    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {"projectId": PROJECT_ID})
    db = firestore.client()

    print(f"\nLegacy Firestore cleanup for project: {PROJECT_ID}\n", file=sys.stderr)

    # Inventory
    totals = {}
    for coll_name in LEGACY_COLLECTIONS:
        docs = list(db.collection(coll_name).stream())
        totals[coll_name] = len(docs)
        print(f"  {coll_name}: {len(docs)} documents", file=sys.stderr)

    total_docs = sum(totals.values())
    print(f"\nTotal: {total_docs} documents across {len(LEGACY_COLLECTIONS)} collections\n", file=sys.stderr)

    if total_docs == 0:
        print("Nothing to delete — Firestore is already clean.", file=sys.stderr)
        return

    if not args.confirm:
        print("Dry run complete. Pass --confirm to delete.\n", file=sys.stderr)
        return

    # Delete
    print("Deleting...\n", file=sys.stderr)
    deleted_total = 0
    for coll_name in LEGACY_COLLECTIONS:
        if totals[coll_name] == 0:
            continue
        deleted = delete_collection(db, coll_name)
        deleted_total += deleted
        print(f"  {coll_name}: deleted {deleted} documents", file=sys.stderr)

    print(f"\nDone. Deleted {deleted_total} documents total.\n", file=sys.stderr)


if __name__ == "__main__":
    main()
