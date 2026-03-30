#!/usr/bin/env python3
"""Seed the Postgres `users` and `user_roles` tables.

Usage:
    cd agent
    source .venv/bin/activate
    DATABASE_URL="postgresql://..." python scripts/seed_users.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(interpolate=False)

from service.pg_client import get_pool

USERS = {
    "michael@haderach.ai": ["admin", "finance_admin"],
    "michael@heretic.fund": ["admin", "finance_admin"],
    "huy@heretic.fund": ["admin", "finance_admin"],
    "mariam@heretic.fund": ["admin", "finance_admin"],
    "mariam@heretic.ventures": ["admin", "finance_admin"],
    "alexmader@gmail.com": ["haderach_user"],
    "binamader@gmail.com": ["haderach_user"],
    "suman@heretic.fund": ["admin"],
    "michael.d.mader@gmail.com": ["user"],
}


def main():
    pool = get_pool()

    with pool.connection() as conn:
        for email, roles in USERS.items():
            normalized = email.strip().lower()

            row = conn.execute(
                """INSERT INTO users (email)
                   VALUES (%s)
                   ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                   RETURNING id""",
                (normalized,),
            ).fetchone()
            user_id = str(row["id"])

            conn.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
            for role_name in roles:
                conn.execute(
                    """INSERT INTO user_roles (user_id, role_id)
                       SELECT %s, r.id FROM roles r WHERE r.name = %s
                       ON CONFLICT DO NOTHING""",
                    (user_id, role_name),
                )

            print(f"  upserted users/{normalized} ({user_id}) roles={roles}")

    print("\nDone.")


if __name__ == "__main__":
    main()
