# `agent/scripts/`

Operational scripts for the agent service. Run from the repo root with the
agent virtualenv active and `DATABASE_URL` set, unless noted.

## Prod snapshot — `pull_prod_snapshot.sh`

Pulls a fresh snapshot of prod's `haderach` Postgres into a local Docker
container, so migrations and code changes can be verified against real data
before deploy. There is no staging environment; this snapshot is the only
pre-prod safety check.

### One-time setup

- Docker Desktop installed and running.
- [`cloud-sql-proxy`](https://cloud.google.com/sql/docs/postgres/sql-proxy)
  v2 on `$PATH`.
- Version-matched Postgres client tools (`pg_dump`, `pg_restore`, `psql`)
  for **PostgreSQL 15** — the prod server's major version. On macOS:
  `brew install postgresql@15`. The script prefers
  `/opt/homebrew/opt/postgresql@15/bin` automatically; override with
  `PG_CLIENT_BIN=/some/other/bin scripts/pull_prod_snapshot.sh` if needed.
  Mismatched client major versions (e.g. v17+) will emit GUCs (such as
  `transaction_timeout`) that the v15 server rejects on restore.
- `gcloud` authenticated against the `haderach-ai` project with read access
  to the `DATABASE_URL` secret.
- `agent-local-dev-sa-key.json` present at the agent repo root (see the
  top-level `README.md` for how to create it).

### Usage

```bash
scripts/pull_prod_snapshot.sh
```

The script is idempotent — re-run it to refresh. It will:

1. Bring up `docker-compose.snapshot.yml` (Postgres 15 on `localhost:5434`,
   container `haderach-snapshot-pg`, volume `agent_snapshot-pgdata`).
2. Start a temporary `cloud-sql-proxy` against
   `haderach-ai:us-central1:haderach-main` on `localhost:5435`.
3. `pg_dump` the prod `haderach` DB in custom format to a temp file.
4. Drop and recreate `haderach_snapshot` in the local container.
5. `pg_restore` into `haderach_snapshot` and print a per-table row summary.

The temporary proxy is killed on exit. The local container keeps running so
you can connect to it directly:

```bash
DATABASE_URL=postgresql://snapshot:localdev@localhost:5434/haderach_snapshot
```

### Working against the snapshot

Point a separate shell or `.env` at the snapshot URL above, then exercise
the agent or run a migration locally:

```bash
DATABASE_URL=postgresql://snapshot:localdev@localhost:5434/haderach_snapshot \
  psql -f migrations/017_orgs.sql
```

For multi-org isolation testing locally, reassign one snapshot user to
Arcade with a one-off `UPDATE` after migration `020` runs. This is dev-only
and not part of the prod migration sequence.

### Tear down

```bash
docker compose -f docker-compose.snapshot.yml down          # stop container
docker compose -f docker-compose.snapshot.yml down -v       # also delete data
```

### Port map

| Port | Service                                              |
|------|------------------------------------------------------|
| 5432 | (CMS dev Postgres, if running — `haderach-cms`)      |
| 5433 | Cloud SQL Auth Proxy → prod (long-running, dev use)  |
| 5434 | **Local snapshot Postgres** (this script)            |
| 5435 | Temporary Cloud SQL Auth Proxy (this script, ephemeral) |

### No PII concerns

The current operational data does not contain personal information beyond
employee email addresses already present in source control via
`agent/scripts/seed_users.py`. The dump is loaded as-is. Revisit this if
the data model gains PII.

## Other scripts

See the docstring at the top of each `.py` file. Most need
`DATABASE_URL` set and the agent virtualenv active.
