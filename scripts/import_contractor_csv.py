#!/usr/bin/env python3
"""Import a classified contractor CSV and set is_contractor on vendors.

Reads a CSV with `id` and `is_contractor` columns (as output by
export_contractor_csv.py, after manual classification by a finance_admin).

Usage:
    cd agent
    source .venv/bin/activate
    DATABASE_URL="postgresql://..." python scripts/import_contractor_csv.py vendors-classified.csv
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(interpolate=False)

from service.pg_client import get_pool


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in ("true", "yes", "1", "t")


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_contractor_csv.py <csv_file>")
        sys.exit(1)

    csv_path = sys.argv[1]

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        if "id" not in reader.fieldnames or "is_contractor" not in reader.fieldnames:
            print(f"ERROR: CSV must have 'id' and 'is_contractor' columns. Found: {reader.fieldnames}")
            sys.exit(1)
        rows = [
            (r["id"].strip(), _parse_bool(r["is_contractor"]))
            for r in reader
            if r["id"].strip()
        ]

    print(f"Loaded {len(rows)} vendor classifications from {csv_path}")

    pool = get_pool()
    updated = 0
    not_found = 0

    with pool.connection() as conn:
        for vendor_id, is_contractor in rows:
            cur = conn.execute(
                "UPDATE vendors SET is_contractor = %s, modified_at = now() WHERE id = %s RETURNING id",
                (is_contractor, vendor_id),
            )
            if cur.fetchone():
                updated += 1
            else:
                print(f"  SKIP (not found): {vendor_id}")
                not_found += 1

    contractors = sum(1 for _, ic in rows if ic)
    non_contractors = len(rows) - contractors
    print(f"\nDone: {updated} updated ({contractors} contractors, {non_contractors} non-contractors), {not_found} not found")


if __name__ == "__main__":
    main()
