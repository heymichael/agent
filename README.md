# Haderach Agent Service

Shared chat agent backend for the Haderach platform. Wraps OpenAI tool-calling
to manage vendors in Firestore and query external billing APIs (Bill.com, AWS
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
export GOOGLE_CLOUD_PROJECT="haderach-ai"
export VENDOR_BILL_CREDENTIALS='{"userName":"...","password":"...","orgId":"...","devKey":"..."}'
export VENDOR_AWS_BILLING_CREDENTIALS='{"access_key_id":"...","secret_access_key":"...","region":"us-east-1"}'

# Run the service
uvicorn service.app:app --reload --port 8080
```

The API is available at `http://localhost:8080`. In production it's mounted at
`/agent/api/` via Firebase Hosting rewrite.

## Vendor sync

Bill.com vendor metadata is synced into the Firestore `vendors` collection.
Run manually:

```bash
source .venv/bin/activate
python -m service.sync_billcom
```

This paginates all Bill.com vendors (~926) and batch-writes to Firestore with
merge semantics (app-managed fields like owner and contract terms are never
overwritten). Takes ~110s.

## Spend sync

Monthly spend summaries from external APIs are aggregated into the Firestore
`vendor_spend` collection. Run manually:

```bash
source .venv/bin/activate

# Bill.com bills (~200–270s, ~1,244 docs)
python -m service.sync_billcom_spend

# AWS Cost Explorer (~10s, ~12 docs)
python -m service.sync_aws_spend
```

Both scripts are idempotent — each run overwrites all spend docs for their
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
  "tool_calls_executed": ["search_vendors"]
}
```

### `GET /health`

Returns `{"status": "ok"}`. No authentication required.

### `DELETE /vendors/{vendor_id}`

Requires `Authorization: Bearer <idToken>`.
Deletes a vendor from Firestore. Returns 400 for Bill.com-synced vendors
(those with a `billcomId`) — use the `hide_vendor` agent tool instead.

### `PATCH /vendors/{vendor_id}`

Requires `Authorization: Bearer <idToken>`.
Partial update on a vendor document. Body is a JSON object of fields to update.

### `GET /users?role={role}`

Requires `Authorization: Bearer <idToken>`.
Lists users whose roles array contains the given role.

## Deployment

Merges to `main` trigger the `build-and-deploy` workflow, which builds a Docker
image, pushes it to Artifact Registry, and deploys to Cloud Run.
