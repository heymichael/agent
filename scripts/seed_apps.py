#!/usr/bin/env python3
"""Seed the Postgres `apps` and `app_granting_roles` tables.

Usage:
    cd agent
    source .venv/bin/activate
    DATABASE_URL="postgresql://..." python scripts/seed_apps.py [--dry-run]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(interpolate=False)

from service.pg_client import get_pool

APPS = [
    {
        "slug": "site",
        "label": "CMS",
        "path": "/site/",
        "type": "app",
        "granting_roles": ["user", "admin"],
        "sort_order": 0,
        "icon": "layout-template",
    },
    {
        "slug": "expenses",
        "label": "Expenses",
        "path": "/expenses/",
        "type": "app",
        "granting_roles": ["user", "admin"],
        "sort_order": 1,
    },
    {
        "slug": "card",
        "label": "Card",
        "path": "/card/",
        "type": "app",
        "granting_roles": ["haderach_user"],
        "sort_order": 2,
    },
    {
        "slug": "stocks",
        "label": "Commodities",
        "path": "/stocks/",
        "type": "app",
        "granting_roles": ["user", "admin"],
        "sort_order": 3,
    },
    {
        "slug": "vendors",
        "label": "Vendors",
        "path": "/vendors/",
        "type": "app",
        "granting_roles": ["user", "admin"],
        "sort_order": 4,
    },
    {
        "slug": "system_administration",
        "label": "System",
        "path": "/admin/system/",
        "type": "admin",
        "granting_roles": ["admin"],
        "sort_order": 1,
    },
    {
        "slug": "vendor_administration",
        "label": "Vendors",
        "path": "/admin/vendors/",
        "type": "admin",
        "granting_roles": ["finance_admin"],
        "sort_order": 2,
    },
]


def main():
    parser = argparse.ArgumentParser(description="Seed Postgres apps table")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    args = parser.parse_args()

    if args.dry_run:
        for app in APPS:
            print(f"[dry-run] Would upsert app '{app['slug']}': {app}")
        print("\nDry run complete — no writes made.")
        return

    pool = get_pool()
    with pool.connection() as conn:
        for app in APPS:
            row = conn.execute(
                """INSERT INTO apps (slug, label, path, type, sort_order, icon)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (slug) DO UPDATE
                     SET label = EXCLUDED.label,
                         path = EXCLUDED.path,
                         type = EXCLUDED.type,
                         sort_order = EXCLUDED.sort_order,
                         icon = EXCLUDED.icon
                   RETURNING id""",
                (app["slug"], app["label"], app["path"], app["type"], app["sort_order"], app.get("icon")),
            ).fetchone()
            app_id = str(row["id"])

            conn.execute("DELETE FROM app_granting_roles WHERE app_id = %s", (app_id,))
            for role_name in app.get("granting_roles", []):
                conn.execute(
                    """INSERT INTO app_granting_roles (app_id, role_id)
                       SELECT %s, r.id FROM roles r WHERE r.name = %s
                       ON CONFLICT DO NOTHING""",
                    (app_id, role_name),
                )

            print(f"  upserted apps/{app['slug']} ({app_id}) roles={app.get('granting_roles', [])}")

    print("\nDone.")


if __name__ == "__main__":
    main()
