#!/usr/bin/env bash
#
# Pull a fresh snapshot of the prod operational Postgres (haderach) into a
# local Docker Postgres container (haderach_snapshot). Use this snapshot to
# verify migrations and exercise the agent against real data before deploy.
#
# Usage:
#   scripts/pull_prod_snapshot.sh
#
# Prerequisites (one-time):
#   - Docker Desktop running
#   - cloud-sql-proxy v2 on $PATH
#   - gcloud authed against project haderach-ai
#   - Postgres client tools (pg_dump, pg_restore, psql) on $PATH
#   - agent-local-dev-sa-key.json present at the repo root
#
# Result: a local Postgres at
#   postgresql://snapshot:localdev@localhost:5434/haderach_snapshot
# containing a full restore of prod's `haderach` database.
#
# Idempotent: re-running drops and recreates the snapshot DB.

set -euo pipefail

PROJECT_ID="haderach-ai"
PROD_INSTANCE="haderach-ai:us-central1:haderach-main"
PROD_DB="haderach"
PROXY_PORT="5435"

SNAPSHOT_HOST="localhost"
SNAPSHOT_PORT="5434"
SNAPSHOT_USER="snapshot"
SNAPSHOT_PASSWORD="localdev"
SNAPSHOT_DB="haderach_snapshot"
SNAPSHOT_CONTAINER="haderach-snapshot-pg"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SA_KEY="$REPO_ROOT/agent-local-dev-sa-key.json"
COMPOSE_FILE="$REPO_ROOT/docker-compose.snapshot.yml"

log()  { printf '\033[1;34m[snapshot]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[snapshot]\033[0m %s\n' "$*" >&2; exit 1; }

require() {
  command -v "$1" >/dev/null 2>&1 \
    || fail "missing required command: $1"
}

require docker
require cloud-sql-proxy
require gcloud

# Prod runs Postgres 15. Prefer matching client tools so dumps don't include
# GUCs the v15 server doesn't recognize at restore time (e.g. v17+'s
# transaction_timeout). Allow an override via PG_CLIENT_BIN.
PG15_BIN="${PG_CLIENT_BIN:-/opt/homebrew/opt/postgresql@15/bin}"
if [ -x "$PG15_BIN/pg_dump" ]; then
  PATH="$PG15_BIN:$PATH"
fi

require pg_dump
require pg_restore
require psql

PG_DUMP_MAJOR="$(pg_dump --version | awk '{print $3}' | cut -d. -f1)"
if [ "$PG_DUMP_MAJOR" != "15" ]; then
  fail "pg_dump major version is $PG_DUMP_MAJOR; need 15 to match prod. Install postgresql@15 (brew install postgresql@15) or set PG_CLIENT_BIN."
fi

[ -f "$SA_KEY" ]        || fail "missing service account key: $SA_KEY"
[ -f "$COMPOSE_FILE" ]  || fail "missing compose file: $COMPOSE_FILE"

if ! docker info >/dev/null 2>&1; then
  fail "docker daemon is not running"
fi

log "fetching prod DATABASE_URL from Secret Manager"
PROD_URL="$(gcloud secrets versions access latest \
  --secret=DATABASE_URL --project="$PROJECT_ID")"

# Prod URL is of the form
#   postgresql://USER:PASSWORD@/DBNAME?host=/cloudsql/INSTANCE
# with the password URL-encoded in-line. Rather than decoding it, rewrite
# the URI to point at the local cloud-sql-proxy and let pg_dump consume the
# whole URI so libpq does the decoding.
PROD_URL_VIA_PROXY="$(printf '%s' "$PROD_URL" \
  | sed -E 's|@/([^?]+).*|@127.0.0.1:'"$PROXY_PORT"'/\1|')"

case "$PROD_URL_VIA_PROXY" in
  postgresql://*@127.0.0.1:"$PROXY_PORT"/*) ;;
  *) fail "could not rewrite prod DATABASE_URL to point at the proxy" ;;
esac

log "[1/5] starting local snapshot Postgres on :$SNAPSHOT_PORT"
docker compose -f "$COMPOSE_FILE" up -d >/dev/null

log "    waiting for $SNAPSHOT_CONTAINER to become healthy"
for _ in $(seq 1 60); do
  status="$(docker inspect \
    --format='{{.State.Health.Status}}' \
    "$SNAPSHOT_CONTAINER" 2>/dev/null || true)"
  [ "$status" = "healthy" ] && break
  sleep 1
done
[ "$status" = "healthy" ] \
  || fail "snapshot Postgres did not become healthy"

log "[2/5] starting cloud-sql-proxy on :$PROXY_PORT"
PROXY_LOG="$(mktemp -t cloud-sql-proxy.snapshot.XXXXXX.log)"
cloud-sql-proxy "$PROD_INSTANCE" \
  --port "$PROXY_PORT" \
  --credentials-file="$SA_KEY" \
  >"$PROXY_LOG" 2>&1 &
PROXY_PID=$!

cleanup() {
  if kill -0 "$PROXY_PID" 2>/dev/null; then
    kill "$PROXY_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
  fi
  rm -f "$PROXY_LOG" "${DUMP:-}"
}
trap cleanup EXIT

log "    waiting for proxy to accept connections"
ready=0
for _ in $(seq 1 60); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$PROXY_PORT") 2>/dev/null; then
    exec 3<&-; exec 3>&-
    ready=1
    break
  fi
  sleep 0.5
done
[ "$ready" -eq 1 ] || {
  cat "$PROXY_LOG" >&2
  fail "cloud-sql-proxy never accepted connections on :$PROXY_PORT"
}

DUMP="$(mktemp -t haderach_snapshot.XXXXXX.dump)"

log "[3/5] pg_dump prod $PROD_DB → $DUMP"
pg_dump \
  --dbname="$PROD_URL_VIA_PROXY" \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file="$DUMP"

log "[4/5] resetting $SNAPSHOT_DB on local Postgres"
PGPASSWORD="$SNAPSHOT_PASSWORD" psql \
  --host="$SNAPSHOT_HOST" \
  --port="$SNAPSHOT_PORT" \
  --username="$SNAPSHOT_USER" \
  --dbname=postgres \
  --quiet --no-psqlrc \
  -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS \"$SNAPSHOT_DB\" WITH (FORCE);" \
  -c "CREATE DATABASE \"$SNAPSHOT_DB\" OWNER \"$SNAPSHOT_USER\";" \
  >/dev/null

log "[5/5] pg_restore → $SNAPSHOT_DB"
PGPASSWORD="$SNAPSHOT_PASSWORD" pg_restore \
  --host="$SNAPSHOT_HOST" \
  --port="$SNAPSHOT_PORT" \
  --username="$SNAPSHOT_USER" \
  --dbname="$SNAPSHOT_DB" \
  --no-owner \
  --no-privileges \
  --exit-on-error \
  "$DUMP"

log "snapshot ready"
echo
echo "  DATABASE_URL=postgresql://$SNAPSHOT_USER:$SNAPSHOT_PASSWORD@$SNAPSHOT_HOST:$SNAPSHOT_PORT/$SNAPSHOT_DB"
echo

log "table summary (approximate live row counts):"
PGPASSWORD="$SNAPSHOT_PASSWORD" psql \
  --host="$SNAPSHOT_HOST" \
  --port="$SNAPSHOT_PORT" \
  --username="$SNAPSHOT_USER" \
  --dbname="$SNAPSHOT_DB" \
  --no-psqlrc \
  -c "SELECT schemaname, relname, n_live_tup AS approx_rows
        FROM pg_stat_user_tables
       ORDER BY schemaname, relname;"
