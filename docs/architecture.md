# Agent Service Architecture

## Purpose

The agent service is a shared backend that accepts chat messages, calls OpenAI
with tool definitions, executes tool calls (Firestore operations, sandboxed
Python for external APIs), and returns natural-language responses. It is
consumed by frontend chat panels across Haderach apps.

## Request flow

```text
Frontend ChatPanel
  │
  ├─ Authorization: Bearer <Firebase ID token>
  │
  ▼
POST /agent/api/chat  (Firebase Hosting rewrite → Cloud Run)
  │
  ▼
Auth middleware (get_verified_user)
  │  Verifies Firebase ID token via Admin SDK
  │  Extracts caller email from decoded token
  │  Rejects with 401 if missing/invalid/expired
  │
  ▼
FastAPI app.py  (max 10 tool-call rounds, 20K char result truncation)
  │
  ├─► OpenAI chat.completions.create (with tool definitions)
  │     │
  │     ▼
  │   Model returns tool_calls?
  │     │ yes                │ no
  │     ▼                    ▼
  │   Execute handler   Return reply
  │     │                    to frontend
  │     ├─ search_vendors ──► Firestore (vendor metadata + optional spend)
  │     ├─ query_spend ─────► Firestore (vendor_spend aggregations)
  │     ├─ add/modify/delete ► Firestore (vendor CRUD)
  │     ├─ hide_vendor ─────► Firestore (toggle hide flag)
  │     ├─ execute_python ──► Sandbox → Bill.com API / AWS CE
  │     │
  │     ▼
  │   Send tool result back to OpenAI → loop
  │
  ▼
Firestore (vendors + vendor_spend collections)
```

## Routing: Firestore-first

The agent uses a three-step routing pattern:

1. **search_vendors** (Firestore) — always called first for any vendor
   question. Returns metadata: name, billcomId, paymentMethod, track1099,
   owner, department, contract fields. Supports fuzzy name matching, field
   filters, group_by aggregation, and optional spend inclusion via
   `include_spend`.

2. **query_spend** (Firestore) — for cross-vendor spend aggregations: totals
   by month, spend grouped by payment method / department / billing frequency,
   top vendors by spend. Queries the `vendor_spend` collection directly.
   Excludes hidden vendors via live lookup.

3. **execute_python** (sandbox) — only called for data not in Firestore:
   individual Bill.com bill details, invoice numbers, PII, AWS per-service
   breakdowns or daily granularity. Uses the `billcomId` from step 1 for
   exact lookups.

## Module layout

| File | Responsibility |
|---|---|
| `service/app.py` | FastAPI application, `/chat` endpoint, orchestration loop, tool-result truncation |
| `service/auth.py` | Firebase ID token verification dependency (`get_verified_user`) |
| `service/tools.py` | OpenAI tool schemas + execution handlers |
| `service/prompts.py` | System prompt with routing rules, API patterns, behavior rules |
| `service/firestore_client.py` | Firestore read/write helpers, `search_vendors()`, `query_spend()`, hide/unhide |
| `service/sandbox.py` | Sandboxed Python executor for LLM-generated code (120s timeout) |
| `service/billcom_auth.py` | Shared Bill.com v3 login helper |
| `service/sync_billcom.py` | Nightly Bill.com → Firestore vendor metadata sync (`python -m service.sync_billcom`) |
| `service/sync_billcom_spend.py` | Nightly Bill.com bills → Firestore spend aggregation sync (`python -m service.sync_billcom_spend`) |
| `service/sync_aws_spend.py` | Nightly AWS Cost Explorer → Firestore spend aggregation sync (`python -m service.sync_aws_spend`) |
| `scripts/smoke-test.sh` | Post-deploy auth smoke tests — verifies health, unauthenticated rejection, and invalid-token rejection |

## Supported tools

| Tool | Params | Effect |
|---|---|---|
| `search_vendors` | query, filters, group_by, include_spend, spend_months, include_hidden | Fuzzy name search, field filters, aggregate counts, optional spend data |
| `query_spend` | month, start_month, end_month, vendor_name, group_by | Cross-vendor spend aggregations from `vendor_spend` collection |
| `add_vendor` | name (+ optional fields) | Creates `vendors/{id}` doc in Firestore |
| `delete_vendor` | identifier | Returns confirmation prompt (UI must approve). Blocked for Bill.com-synced vendors |
| `modify_vendor` | identifier | Opens edit modal in UI |
| `hide_vendor` | identifier, hide | Toggles hide flag — excludes vendor from spend analysis |
| `execute_python` | code | Runs sandboxed Python for Bill.com/AWS API queries |

## Firestore `vendors` schema

Unified collection — all vendors regardless of source.

**Synced from Bill.com (nightly):** `id`, `name`, `billcomId`, `nameLower`,
`paymentMethod`, `accountType`, `track1099`, `toolCall`, `lastSyncedAt`

**App-managed:** `owner`, `secondaryOwner`, `department`, `purpose`,
`spendType`, `hide`

**Contract fields:** `contractStartDate`, `contractEndDate`,
`contractLengthMonths`, `autoRenew`, `renewalRate`, `renewalNoticeDays`,
`billingFrequency`, `terminationTerms`

No PII stored (no email, phone, address, taxId).

## Firestore `vendor_spend` schema

Top-level collection with monthly spend summaries per vendor. Doc ID:
`{vendorId}_{YYYY-MM}`.

**Core fields:** `vendorId`, `vendorName`, `month`, `totalAmount`, `billCount`,
`toolCall`, `lastSyncedAt`

**Denormalized from vendor doc:** `paymentMethod`, `billingFrequency`,
`department`, `owner`, `track1099`, `accountType`, `purpose`, `spendType`,
`hide`

**Sources:**

- **Bill.com** — synced by `python -m service.sync_billcom_spend`. Paginates
  all bills, aggregates by vendor + month. `toolCall: "billcom"`. ~1,244 docs.
- **AWS Cost Explorer** — synced by `python -m service.sync_aws_spend`. Fetches
  12 months of monthly unblended costs. `toolCall: "aws-ce"`. `vendorId: "aws"`
  (matches Firestore vendor doc ID). ~12 docs.

## Delete guard

Bill.com-synced vendors (docs with a `billcomId`) cannot be deleted — they
would be re-created on the next nightly sync. The guard is enforced in both
the `delete_vendor` tool handler and the `DELETE /vendors/{id}` REST endpoint.
Users can hide vendors from spend analysis instead via `hide_vendor`.

## Runtime

- **Cloud Run** (`agent-api`, us-central1)
- **Image**: `us-central1-docker.pkg.dev/haderach-ai/haderach-apps/agent-api`
- **Secrets** (from Secret Manager):
  - `OPENAI_API_KEY`
  - `VENDOR_BILL_CREDENTIALS` (Bill.com v3 API: userName, password, orgId, devKey)
  - `VENDOR_AWS_BILLING_CREDENTIALS` (AWS CE: access_key_id, secret_access_key, region)
- **Model**: configurable via `OPENAI_MODEL` env var (default: `gpt-4o-mini`)

## Firestore access

The service uses the default compute service account, which has Datastore User
access on the `haderach-ai` project. Writes go through the Admin SDK (not
subject to client Firestore rules).

## Authentication

All sensitive endpoints require a valid Firebase ID token in the `Authorization`
header (`Bearer <idToken>`). The `get_verified_user` dependency in `service/auth.py`
verifies the token via Firebase Admin SDK and extracts the caller's email. Requests
without a valid token are rejected with HTTP 401.

The `/health` endpoint is unauthenticated.

## API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/chat` | Required | Chat with the agent (tool-calling loop) |
| `GET` | `/health` | None | Health check |
| `DELETE` | `/vendors/{vendor_id}` | Required | Delete a vendor (blocked for Bill.com-synced vendors) |
| `PATCH` | `/vendors/{vendor_id}` | Required | Partial update a vendor |
| `GET` | `/users?role=...` | Required | List users by role |
