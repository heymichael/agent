#!/usr/bin/env python3
"""Export all vendors as a CSV for contractor classification.

The finance_admin fills in the `is_contractor` column (true/false) and
runs import_contractor_csv.py to apply the classification.

Usage:
    cd agent
    source .venv/bin/activate
    DATABASE_URL="postgresql://..." python scripts/export_contractor_csv.py [output.csv]
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(interpolate=False)

from service.pg_client import get_pool


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "vendors-contractor-classification.csv"

    pool = get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT v.id::text, v.name, v.source_system, v.is_contractor,
                      d.name AS department
               FROM vendors v
               LEFT JOIN departments d ON d.id = v.department_id
               ORDER BY LOWER(v.name)"""
        ).fetchall()

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "source_system", "department", "is_contractor"])
        for r in rows:
            writer.writerow([
                r["id"], r["name"], r["source_system"],
                r["department"] or "", r["is_contractor"],
            ])

    print(f"Exported {len(rows)} vendors to {out_path}")


if __name__ == "__main__":
    main()
