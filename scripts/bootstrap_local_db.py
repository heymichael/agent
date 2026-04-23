#!/usr/bin/env python3
"""Bootstrap a local Postgres database for agent development.

Usage:
    cd agent
    source .venv/bin/activate
    DATABASE_URL=postgresql://agent_local:localdev@localhost:5436/haderach_local \
      python scripts/bootstrap_local_db.py [--reset]

This is the local-only path for task 239: build the schema from migrations,
seed the minimum auth/app data, and let the service run without Cloud SQL
Proxy or production credentials.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

LOCAL_USERS: dict[str, list[str]] = {
    "michael@haderach.ai": ["admin", "finance_admin"],
    "michael@heretic.fund": ["admin", "finance_admin"],
    "huy@heretic.fund": ["admin", "finance_admin"],
    "mariam@heretic.fund": ["admin", "finance_admin"],
    "mariam@heretic.ventures": ["admin", "finance_admin"],
    "suman@heretic.fund": ["admin"],
    "michael.d.mader@gmail.com": ["user"],
}

LOCAL_MEMBERSHIPS: dict[str, str] = {
    "huy@heretic.fund": "arcade",
    "mariam@heretic.fund": "arcade",
    "mariam@heretic.ventures": "arcade",
    "michael@heretic.fund": "arcade",
    "suman@heretic.fund": "arcade",
    "michael.d.mader@gmail.com": "arcade",
    "michael@haderach.ai": "haderach",
}

LOCAL_APPS = [
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
        "icon": None,
    },
    {
        "slug": "stocks",
        "label": "Commodities",
        "path": "/stocks/",
        "type": "app",
        "granting_roles": ["user", "admin"],
        "sort_order": 3,
        "icon": None,
    },
    {
        "slug": "vendors",
        "label": "Vendors",
        "path": "/vendors/",
        "type": "app",
        "granting_roles": ["user", "admin"],
        "sort_order": 4,
        "icon": None,
    },
    {
        "slug": "system_administration",
        "label": "System",
        "path": "/admin/system/",
        "type": "admin",
        "granting_roles": ["admin"],
        "sort_order": 1,
        "icon": None,
    },
    {
        "slug": "vendor_administration",
        "label": "Vendors",
        "path": "/admin/vendors/",
        "type": "admin",
        "granting_roles": ["finance_admin"],
        "sort_order": 2,
        "icon": None,
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap the local agent Postgres database")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate the public schema before applying migrations",
    )
    return parser.parse_args()


def ensure_database_url() -> str:
    conninfo = os.environ.get("DATABASE_URL", "").strip()
    if not conninfo:
        raise SystemExit("DATABASE_URL is required")
    return conninfo


def migration_paths() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def ensure_empty_schema(conn: psycopg.Connection, *, reset: bool) -> None:
    table_count = conn.execute(
        """
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'public'
        """
    ).fetchone()[0]
    if table_count == 0:
        return
    if not reset:
        raise SystemExit(
            "public schema is not empty; rerun with --reset for a clean local rebuild"
        )
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")


def ensure_extensions(conn: psycopg.Connection) -> None:
    conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.execute("CREATE EXTENSION IF NOT EXISTS citext")


def apply_migrations(conn: psycopg.Connection) -> None:
    paths = migration_paths()
    if not paths:
        raise SystemExit("no migration files found")
    for path in paths:
        print(f"applying {path.name}")
        conn.execute(path.read_text())


def seed_users_and_memberships(conn: psycopg.Connection) -> None:
    for email, roles in LOCAL_USERS.items():
        normalized = email.strip().lower()
        user_row = conn.execute(
            """
            INSERT INTO users (email)
            VALUES (%s)
            ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
            RETURNING id
            """,
            (normalized,),
        ).fetchone()
        user_id = str(user_row[0])

        conn.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
        for role_name in roles:
            conn.execute(
                """
                INSERT INTO user_roles (user_id, role_id)
                SELECT %s, r.id FROM roles r WHERE r.name = %s
                ON CONFLICT DO NOTHING
                """,
                (user_id, role_name),
            )

        org_slug = LOCAL_MEMBERSHIPS.get(normalized, "arcade")
        conn.execute(
            """
            INSERT INTO user_org_memberships (user_id, org_slug)
            VALUES (%s, %s)
            ON CONFLICT (user_id, org_slug) DO NOTHING
            """,
            (user_id, org_slug),
        )

        print(f"seeded user {normalized} roles={roles} org={org_slug}")


def seed_apps(conn: psycopg.Connection) -> None:
    for app in LOCAL_APPS:
        app_row = conn.execute(
            """
            INSERT INTO apps (slug, label, path, type, sort_order, icon)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE
              SET label = EXCLUDED.label,
                  path = EXCLUDED.path,
                  type = EXCLUDED.type,
                  sort_order = EXCLUDED.sort_order,
                  icon = EXCLUDED.icon
            RETURNING id
            """,
            (
                app["slug"],
                app["label"],
                app["path"],
                app["type"],
                app["sort_order"],
                app["icon"],
            ),
        ).fetchone()
        app_id = str(app_row[0])
        conn.execute("DELETE FROM app_granting_roles WHERE app_id = %s", (app_id,))
        for role_name in app["granting_roles"]:
            conn.execute(
                """
                INSERT INTO app_granting_roles (app_id, role_id)
                SELECT %s, r.id FROM roles r WHERE r.name = %s
                ON CONFLICT DO NOTHING
                """,
                (app_id, role_name),
            )
        print(f"seeded app {app['slug']} roles={app['granting_roles']}")


def main() -> None:
    args = parse_args()
    conninfo = ensure_database_url()

    with psycopg.connect(conninfo, autocommit=True) as conn:
        ensure_empty_schema(conn, reset=args.reset)
        ensure_extensions(conn)
        apply_migrations(conn)
        seed_users_and_memberships(conn)
        seed_apps(conn)

    print("\nLocal DB ready.")
    print(f"DATABASE_URL={conninfo}")
    print("Recommended DEV_AUTH_EMAIL=michael@heretic.fund")


if __name__ == "__main__":
    main()
