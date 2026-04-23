# Haderach Agent Service

Shared chat agent backend for the Haderach platform. Wraps OpenAI tool-calling
to manage vendors in Postgres and query external billing APIs (Bill.com, AWS
Cost Explorer) via sandboxed Python execution.

## Architecture

See [docs/architecture.md](docs/architecture.md).

## Local development

```bash
# Create a virtualenv
python3 -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start a local Postgres 15 container
docker compose -f docker-compose.local.yml up -d

# Copy .env.example to .env
cp .env.example .env

# Build the local schema and seed the minimum auth/app rows
python scripts/bootstrap_local_db.py

# Run the service (all config is loaded from .env via python-dotenv)
uvicorn service.app:app --reload --port 8080
```

The API is available at `http://localhost:8080`. In production it's mounted at
`/agent/api/` via Firebase Hosting rewrite.

The default `.env.example` uses a fully local database on `localhost:5436` and
`DEV_AUTH_EMAIL`, so you can run the agent without Cloud SQL Proxy, Firebase
credentials, or production database access. Requests authenticate as
`michael@heretic.fund` by default unless you override with `X-Test-Email`.

If you need a clean rebuild, rerun the bootstrap with `--reset`:

```bash
python scripts/bootstrap_local_db.py --reset
```

### Working against shared demo data

Routine local development should load the curated demo dataset from
`gs://haderach-demo-data` into `docker-compose.local.yml` on port `5436`.
See `haderach-platform/docs/demo-data-runbook.md` section 5 for the
end-to-end download + load steps.

### Owner-only ops paths

The following paths bypass curation and are reserved for the data owner.
They must not be used as routine developer workflows.

**Prod snapshot (migration validation only).** `scripts/pull_prod_snapshot.sh`
restores raw prod data into a local Docker Postgres on `localhost:5434` so
the owner can validate a migration before deploy. Tear down with
`docker compose -f docker-compose.snapshot.yml down -v` when finished. See
`scripts/README.md` for the full procedure.

**Direct Cloud SQL proxy (live debugging only).** When the owner needs to
inspect or debug the live Cloud SQL instance directly:

```bash
cloud-sql-proxy haderach-ai:us-central1:haderach-main --port 5433 \
  --credentials-file=agent-local-dev-sa-key.json
```

Then point `DATABASE_URL` at `localhost:5433`. Retrieve the password from
Secret Manager:

```bash
gcloud secrets versions access latest --secret=DATABASE_URL --project=haderach-ai
```

### GCP credentials

The default local DB path does not require GCP credentials at all — leave
`GOOGLE_APPLICATION_CREDENTIALS` commented out in `.env`. You only need the
service account key when using one of the owner-only ops paths above, or
when running GCP-backed vendor sync integrations
(`service.sync_gcp_spend`).

```bash
# One-time setup (key already exists for most devs):
gcloud iam service-accounts keys create agent-local-dev-sa-key.json \
  --iam-account=agent-local-dev@haderach-ai.iam.gserviceaccount.com
```

The `*-sa-key.json` pattern is gitignored. Never commit key files.

## Vendor sync

Bill.com vendor metadata is synced into the Postgres `vendors` table.
Run manually:

```bash
source .venv/bin/activate
python -m service.sync_billcom
```

This paginates all Bill.com vendors (~926) and upserts to Postgres with
merge semantics (app-managed fields like owner and contract terms are never
overwritten). Takes ~110s.

## Spend sync

Spend data from external APIs is synced into Postgres. Granular line items
go into `vendor_spend_detail`, then roll up to `vendor_monthly_spend`
(summary). Run manually:

```bash
source .venv/bin/activate

# Bill.com bills (~200–270s, ~1,244 rows) — writes to summary only
python -m service.sync_billcom_spend

# AWS Cost Explorer (~10s) — detail rows (by Service + UsageType) + summary rollup
python -m service.sync_aws_spend

# GCP BigQuery billing export — detail rows (by Service + SKU) + summary rollup
python -m service.sync_gcp_spend
```

All scripts are idempotent — each run upserts rows via `ON CONFLICT DO UPDATE`.

### Sync job tracking

Every sync run is logged to `sync_job_log` (one row per run) and
`sync_job_step` (one row per step). Steps use standardized names across all
jobs:

| Step | Used by | Meaning |
|------|---------|---------|
| `api_fetch` | All | Got data from the source API |
| `vendor_sync` | Bill.com | Vendor master records refreshed |
| `detail_upsert` | AWS, GCP | Granular rows landed in `vendor_spend_detail` |
| `summary_upsert` | All | Monthly summary rows landed in `vendor_monthly_spend` |
| `reconcile` | AWS, GCP | Verified `SUM(detail)` matches summary per month |

Each step records `status`, `duration_ms`, `row_count`, and `error` (on
failure). The reconcile step logs mismatches into `metadata` for debugging.
Implemented via `service/sync_tracker.py` (`SyncTracker` class).

### Detail table field mapping

Each sync job maps vendor-native fields to canonical columns on
`vendor_spend_detail`. Column names are standardized; values are
vendor-native (no semantic normalization).

| Vendor | `category` | `subcategory` | `project` | `metadata` |
|--------|-----------|--------------|----------|-----------|
| AWS | Service | UsageType | — | — |
| GCP | service.description | sku.description | project.id | `{"sku_id": ..., "region": ...}` |

## Feedback

Two feedback channels are stored in Postgres:

- **Chat feedback** (`POST /feedback`) — per-message thumbs up/down with optional comment. Keyed on `(chat_session_id, message_seq)`.
- **Site feedback** (`POST /feedback/site`) — general app feedback with a JSONB snapshot of the user's open panes for context.

Both require authentication. Schema: `migrations/006_chat_feedback.sql`, `migrations/007_site_feedback.sql`, `migrations/008_feedback_collected.sql`.

## Hidden vendors

Vendors with `hidden_from_agent = true` are excluded from all agent tool
queries (resolver, vendor_list, vendor_count, spend queries) but remain in
Postgres with their data intact. The REST API (`/vendors`, `/spend`)
continues to return all vendors regardless of this flag. Use this to hide
duplicate entries when a more authoritative source exists (e.g., hide the
Bill.com "Amazon Web Services" in favor of the Cost Explorer "AWS" vendor).

## Authentication

All endpoints except `/health` require a Firebase ID token in the
`Authorization` header. The frontend obtains the token via
`firebase.auth().currentUser.getIdToken()` and sends it as a Bearer token.

```
Authorization: Bearer <Firebase ID token>
```

Unauthenticated or invalid-token requests receive HTTP 401.

### Dev-mode identity override

When `DEV_AUTH_EMAIL` is set (local development only), Firebase token
verification is skipped entirely. An `X-Test-Email` header can override the
authenticated identity for that request:

```
X-Test-Email: scoped-user@example.com
```

If the header is absent, `DEV_AUTH_EMAIL` is used as the caller. This
mechanism enables e2e testing with different access levels without switching
env vars or restarting the server. It has no effect in production (where
`DEV_AUTH_EMAIL` is never set).

## Testing

### Test file map

Three test files cover the vendor write pipeline. Each targets a different
layer:

| File | Layer | Runs as | What it tests |
|------|-------|---------|---------------|
| `test_csv_e2e.py` | Input validation | Admin | Bad columns, invalid UUIDs, edge cases, read-only queries |
| `test_csv_e2e.py` | Business logic | Admin | CSV confirm flow, single-vendor edits (auth gate bypassed) |
| `test_write_auth_e2e.py` | Authorization | Scoped user | Allowed/denied edits, mixed CSV rejection |
| `test_write_auth.py` | Authorization | Mocked (unit) | Same auth logic, no network — fast and deterministic |

**Input validation** fails requests before the auth check runs — outcome is
the same regardless of caller identity. **Business logic** is reached only
after auth passes (admin bypass). **Authorization** is the only layer whose
outcome depends on who the caller is.

See `.cursor/rules/e2e-testing.mdc` for infrastructure setup instructions.

## API

### `POST /chat`

Requires `Authorization: Bearer <idToken>`.

```json
{
  "messages": [
    { "role": "user", "content": "How many 1099 vendors do we have?" }
  ]
}
```

Response:

```json
{
  "reply": "You have 150 1099 vendors.",
  "tool_calls_executed": ["vendor_count"],
  "pending_actions": [],
  "disambiguation": null,
  "downloads": [],
  "session_id": "uuid",
  "tool_messages": []
}
```

| Field | Type | Purpose |
|---|---|---|
| `reply` | string | Agent's natural-language response |
| `tool_calls_executed` | string[] | Names of tools called this turn |
| `pending_actions` | PendingAction[] | `confirm_edit` or `confirm_csv_batch` actions for the frontend to present |
| `disambiguation` | object \| null | Ambiguous vendor match with candidates for inline selection |
| `downloads` | Download[] | CSV files to offer as downloads (`filename`, `content`, `mime`) |
| `session_id` | string | Chat session ID for multi-turn continuity and feedback |
| `tool_messages` | dict[] | Raw tool call/result messages from this turn, replayed by the frontend on the next request for multi-turn context |

When tools generate downloadable content (e.g., `vendor_list` with 10+
results), the `downloads` array contains objects with `filename`, `content`
(CSV string), and `mime` fields. The CSV is stripped from the OpenAI tool
context to save tokens.

### `GET /health`

Returns `{"status": "ok"}`. No authentication required.

### `DELETE /vendors/{vendor_id}`

Requires `Authorization: Bearer <idToken>`.
Deletes a vendor from Postgres. Returns 400 for synced vendors (those with
`source_system` != 'manual') — synced vendors would be re-created on the
next nightly sync.

### `PATCH /vendors/{vendor_id}`

Requires `Authorization: Bearer <idToken>`.
Partial update on a vendor. Body is a JSON object of fields to update.

### `GET /users?role={role}`

Requires `Authorization: Bearer <idToken>`.
Lists users whose roles include the given role.

## Deployment

Merges to `main` trigger the `build-and-deploy` workflow, which builds a Docker
image, pushes it to Artifact Registry, and deploys to Cloud Run.
