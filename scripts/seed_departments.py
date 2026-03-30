#!/usr/bin/env python3
"""Bulk-update vendor department assignments from a CSV file.

Creates departments on-the-fly and sets vendors.department_id via
source_system_id lookup.

Usage:
    cd agent
    source .venv/bin/activate
    DATABASE_URL="postgresql://..." python scripts/seed_departments.py path/to/departments.csv

CSV format (tab or comma separated, with header row):
    id,department
    00902ABC...,Engineering
    00902DEF...,Marketing

The `id` column is the Bill.com vendor ID (source_system_id).
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(interpolate=False)

from service.pg_client import get_pool


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/seed_departments.py <csv_file>")
        sys.exit(1)

    csv_path = sys.argv[1]

    with open(csv_path, newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        reader = csv.DictReader(f, dialect=dialect)

        if "id" not in reader.fieldnames or "department" not in reader.fieldnames:
            print(f"ERROR: CSV must have 'id' and 'department' columns. Found: {reader.fieldnames}")
            sys.exit(1)

        rows = [
            (r["id"].strip(), r["department"].strip())
            for r in reader
            if r["id"].strip() and r["department"].strip()
        ]

    print(f"Loaded {len(rows)} vendor-department mappings from {csv_path}")

    pool = get_pool()
    updated = 0
    not_found = 0

    with pool.connection() as conn:
        for source_system_id, dept_name in rows:
            dept_row = conn.execute(
                """INSERT INTO departments (name) VALUES (%s)
                   ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                   RETURNING id""",
                (dept_name,),
            ).fetchone()
            dept_id = str(dept_row["id"])

            cur = conn.execute(
                """UPDATE vendors SET department_id = %s, modified_at = now()
                   WHERE source_system = 'billcom' AND source_system_id = %s
                   RETURNING id""",
                (dept_id, source_system_id),
            )
            if cur.fetchone():
                updated += 1
            else:
                print(f"  SKIP (not found): {source_system_id}")
                not_found += 1

    print(f"\nDone: {updated} updated, {not_found} not found")


if __name__ == "__main__":
    main()
