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
  ▼
POST /agent/api/chat  (Firebase Hosting rewrite → Cloud Run)
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
  │     ├─ search_vendors ──► Firestore (vendor metadata)
  │     ├─ add/modify/delete ► Firestore (vendor CRUD)
  │     ├─ execute_python ──► Sandbox → Bill.com API / AWS CE
  │     │
  │     ▼
  │   Send tool result back to OpenAI → loop
  │
  ▼
Firestore (vendors collection)
```

## Routing: Firestore-first

The agent uses a two-step routing pattern:

1. **search_vendors** (Firestore) — always called first for any vendor
   question. Returns metadata: name, billcomId, paymentMethod, track1099,
   owner, department, contract fields. Supports fuzzy name matching, field
   filters, and group_by aggregation.

2. **execute_python** (sandbox) — only called for transactional data that
   isn't in Firestore: Bill.com bills/spend/PII, AWS Cost Explorer cloud
   costs. Uses the `billcomId` from step 1 for exact lookups.

Cross-source joins (queries needing both Firestore metadata and live spend
data) are explicitly unsupported until spend summaries are synced.

## Module layout

| File | Responsibility |
|---|---|
| `service/app.py` | FastAPI application, `/chat` endpoint, orchestration loop, tool-result truncation |
| `service/tools.py` | OpenAI tool schemas + execution handlers |
| `service/prompts.py` | System prompt with routing rules, API patterns, behavior rules |
| `service/firestore_client.py` | Firestore read/write helpers, `search_vendors()` |
| `service/sandbox.py` | Sandboxed Python executor for LLM-generated code (120s timeout) |
| `service/sync_billcom.py` | Nightly Bill.com → Firestore vendor sync (run via `python -m service.sync_billcom`) |

## Supported tools

| Tool | Params | Effect |
|---|---|---|
| `search_vendors` | query, filters, group_by | Fuzzy name search, field filters, aggregate counts against Firestore |
| `add_vendor` | name (+ optional fields) | Creates `vendors/{id}` doc in Firestore |
| `delete_vendor` | identifier | Returns confirmation prompt (UI must approve) |
| `modify_vendor` | identifier | Opens edit modal in UI |
| `execute_python` | code | Runs sandboxed Python for Bill.com/AWS API queries |

## Firestore `vendors` schema

Unified collection — all vendors regardless of source.

**Synced from Bill.com (nightly):** `id`, `name`, `billcomId`, `nameLower`,
`paymentMethod`, `accountType`, `track1099`, `toolCall`, `lastSyncedAt`

**App-managed:** `owner`, `secondaryOwner`, `department`, `purpose`, `spendType`

**Contract fields:** `contractStartDate`, `contractEndDate`,
`contractLengthMonths`, `autoRenew`, `renewalRate`, `renewalNoticeDays`,
`billingFrequency`, `terminationTerms`

No PII stored (no email, phone, address, taxId).

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

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat` | Chat with the agent (tool-calling loop) |
| `GET` | `/health` | Health check |
| `DELETE` | `/vendors/{vendor_id}` | Delete a vendor |
| `PATCH` | `/vendors/{vendor_id}` | Partial update a vendor |
| `GET` | `/users?role=...` | List users by role |
