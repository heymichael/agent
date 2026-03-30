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

# Set required env vars (or copy .env.example to .env)
export OPENAI_API_KEY="sk-..."
export DATABASE_URL="postgresql://haderach-app:<password>@localhost:5433/haderach"
export VENDOR_BILL_CREDENTIALS='{"userName":"...","password":"...","orgId":"...","devKey":"..."}'
export VENDOR_AWS_BILLING_CREDENTIALS='{"access_key_id":"...","secret_access_key":"...","region":"us-east-1"}'

# Run the service
uvicorn service.app:app --reload --port 8080
```

The API is available at `http://localhost:8080`. In production it's mounted at
`/agent/api/` via Firebase Hosting rewrite.

### Cloud SQL connection (local)

For local development, start the Cloud SQL Auth Proxy to tunnel to the
production database:

```bash
cloud-sql-proxy haderach-ai:us-central1:haderach-main --port 5433
```

Then set `DATABASE_URL` to point at `localhost:5433`. Credentials are stored in
Secret Manager — retrieve them with:

```bash
gcloud secrets versions access latest --secret=DATABASE_URL --project=haderach-ai
```

### GCP credentials

The service needs GCP credentials for Firebase Auth token verification. Two
options:

**Option A — Service account key (recommended).** Place a key file in the repo
root and point to it in `.env`. The key never expires, so you won't get
interrupted by credential expiry during dev sessions.

```bash
# One-time setup:
gcloud iam service-accounts keys create agent-local-dev-sa-key.json \
  --iam-account=agent-local-dev@haderach-ai.iam.gserviceaccount.com

# Add to .env
echo 'GOOGLE_APPLICATION_CREDENTIALS=agent-local-dev-sa-key.json' >> .env
```

The `*-sa-key.json` pattern is gitignored. Never commit key files.

**Option B — Application Default Credentials.** Run
`gcloud auth application-default login`. Credentials expire periodically and
require re-authentication.

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

Monthly spend summaries from external APIs are aggregated into the
`vendor_monthly_spend` table. Run manually:

```bash
source .venv/bin/activate

# Bill.com bills (~200–270s, ~1,244 rows)
python -m service.sync_billcom_spend

# AWS Cost Explorer (~10s, ~12 rows)
python -m service.sync_aws_spend
```

Both scripts are idempotent — each run upserts all spend rows for their
source. Bill.com sync paginates all bills and aggregates by vendor + month.
AWS sync fetches 12 months of monthly unblended costs from Cost Explorer.

## Authentication

All endpoints except `/health` require a Firebase ID token in the
`Authorization` header. The frontend obtains the token via
`firebase.auth().currentUser.getIdToken()` and sends it as a Bearer token.

```
Authorization: Bearer <Firebase ID token>
```

Unauthenticated or invalid-token requests receive HTTP 401.

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
  "tool_calls_executed": ["vendor_count"]
}
```

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
