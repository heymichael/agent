#!/usr/bin/env python3
"""Apply pending database migrations to Cloud SQL Postgres.

Usage (CI):
    DATABASE_URL="postgresql://..." python scripts/run_migrations.py

Usage (local with bootstrap):
    DATABASE_URL="postgresql://..." python scripts/run_migrations.py --bootstrap

This script:
  1. Connects to the database via DATABASE_URL
  2. Ensures the schema_migrations table exists (runs 025 if needed)
  3. Computes SHA-256 checksums for all migration files
  4. Skips migrations already recorded in schema_migrations
  5. Applies pending migrations in order, recording each in schema_migrations
  6. Fails fast on any error (no partial migrations)

The --bootstrap flag marks migrations 001-024 as already applied without
running them. Use this once to initialize a production database that was
set up before the CI migration process existed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_schema_migrations_filename ON schema_migrations (filename);
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply pending database migrations")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Mark migrations 001-024 as applied without running them (one-time setup)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which migrations would run without applying them",
    )
    return parser.parse_args()


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL environment variable is required")
    return url


def compute_checksum(path: Path) -> str:
    """Compute SHA-256 checksum of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def get_migration_files() -> list[tuple[Path, str]]:
    """Return sorted list of (path, checksum) for all migration files."""
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [(p, compute_checksum(p)) for p in paths]


def ensure_schema_migrations_table(conn: psycopg.Connection) -> None:
    """Create schema_migrations table if it doesn't exist."""
    conn.execute(SCHEMA_MIGRATIONS_DDL)


def get_applied_migrations(conn: psycopg.Connection) -> dict[str, str]:
    """Return dict of filename -> checksum for all applied migrations."""
    rows = conn.execute(
        "SELECT filename, checksum FROM schema_migrations"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def record_migration(
    conn: psycopg.Connection, filename: str, checksum: str, applied_by: str | None
) -> None:
    """Record a migration as applied."""
    conn.execute(
        """
        INSERT INTO schema_migrations (filename, checksum, applied_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (filename) DO UPDATE SET checksum = EXCLUDED.checksum
        """,
        (filename, checksum, applied_by),
    )


def apply_migration(conn: psycopg.Connection, path: Path) -> None:
    """Execute a migration file."""
    sql = path.read_text()
    conn.execute(sql)


def main() -> None:
    args = parse_args()
    database_url = get_database_url()
    applied_by = os.environ.get("GITHUB_ACTOR") or os.environ.get("USER")

    migrations = get_migration_files()
    if not migrations:
        raise SystemExit("No migration files found in migrations/")

    with psycopg.connect(database_url, autocommit=True) as conn:
        ensure_schema_migrations_table(conn)
        applied = get_applied_migrations(conn)

        pending: list[tuple[Path, str]] = []
        for path, checksum in migrations:
            filename = path.name
            if filename in applied:
                existing_checksum = applied[filename]
                if existing_checksum != checksum:
                    raise SystemExit(
                        f"Checksum mismatch for {filename}:\n"
                        f"  Applied: {existing_checksum}\n"
                        f"  Current: {checksum}\n"
                        "Migration files must not be modified after application."
                    )
                continue
            pending.append((path, checksum))

        if not pending:
            print("All migrations already applied.")
            return

        if args.dry_run:
            print(f"Would apply {len(pending)} migration(s):")
            for path, _ in pending:
                print(f"  - {path.name}")
            return

        if args.bootstrap:
            # Mark migrations 001-023 as already applied (before CI process).
            # Migration 024 (card app retirement) and later will be applied normally.
            bootstrap_cutoff = "024_retire_card_app.sql"
            bootstrap_count = 0
            for path, checksum in pending:
                if path.name >= bootstrap_cutoff:
                    break
                print(f"[bootstrap] marking {path.name} as applied")
                record_migration(conn, path.name, checksum, f"{applied_by} (bootstrap)")
                bootstrap_count += 1
            pending = pending[bootstrap_count:]
            if not pending:
                print(f"Bootstrap complete. {bootstrap_count} migrations marked as applied.")
                return
            print(f"Bootstrap marked {bootstrap_count} migrations. Proceeding with {len(pending)} remaining.")

        print(f"Applying {len(pending)} pending migration(s)...")
        for path, checksum in pending:
            print(f"  applying {path.name}...")
            try:
                apply_migration(conn, path)
                record_migration(conn, path.name, checksum, applied_by)
                print(f"  ✓ {path.name} applied")
            except Exception as e:
                print(f"  ✗ {path.name} FAILED: {e}")
                raise SystemExit(f"Migration {path.name} failed") from e

        print(f"\nAll {len(pending)} migration(s) applied successfully.")


if __name__ == "__main__":
    main()
