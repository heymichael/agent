# Agent Service Architecture

## Purpose

The agent service is a shared backend that accepts chat messages, calls OpenAI
with tool definitions, executes tool calls (MCP analytics layer, Firestore
writes, sandboxed Python for external APIs), and returns natural-language
responses. It is consumed by frontend chat panels across Haderach apps.

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
  │     │
  │     ├─ Analytics tools ──► MCP server module ──► Firestore
  │     │   (vendor_lookup, vendor_count,
  │     │    spend_total, spend_by_vendor,
  │     │    spend_by_dimension, top_vendors)
  │     │
  │     ├─ Write tools ──────► Firestore (vendor CRUD)
  │     │   (add/modify/delete/hide_vendor)
  │     │
  │     ├─ execute_python ──► Sandbox → Bill.com API / AWS CE
  │     │
  │     ▼
  │   Send tool result back to OpenAI → loop
  │
  ▼
Firestore (vendors + vendor_spend collections)
```

## MCP analytics layer

Analytics tools are backed by the `mcp_server/` module, which provides:

- **Intent-aligned tool handlers** — six tools with clear, specific purposes
  instead of general-purpose query builders
- **Vendor resolution pipeline** — single `resolve_vendor()` function that
  handles exact ID, exact name, alias, normalised, and token/fuzzy matching.
  Returns ok/ambiguous/not_found.
- **Period parser** — deterministic conversion of period strings (YYYY-MM,
  YYYY-QN, YTD, last-N-months) to month ranges
- **Filter validation** — enum fields validated at schema level, dynamic
  fields (department, owner) validated against Firestore data
- **Standardised response contract** — every tool returns a `status` field:
  `ok`, `ambiguous`, `not_found`, `not_authorized`, or `invalid_filter`

The MCP module is imported directly by the production agent (in-process, no
protocol overhead). It can also be run as a standalone MCP server via
`python -m mcp_server` for Cursor or other MCP clients (stdio transport).

## Module layout

| File | Responsibility |
|---|---|
| `service/app.py` | FastAPI application, `/chat` endpoint, orchestration loop, tool-result truncation |
| `service/auth.py` | Firebase ID token verification dependency (`get_verified_user`) |
| `service/tools.py` | OpenAI tool schemas + thin handler wrappers that delegate to MCP module |
| `service/prompts.py` | System prompt with tool list, response contract rules, and behaviour rules |
| `service/firestore_client.py` | Firestore read/write helpers, vendor CRUD, spend queries, user CRUD, feature flags, access resolution |
| `service/sandbox.py` | Sandboxed Python executor for LLM-generated code (120s timeout) |
| `service/billcom_auth.py` | Shared Bill.com v3 login helper |
| `service/sync_billcom.py` | Nightly Bill.com → Firestore vendor metadata sync |
| `service/sync_billcom_spend.py` | Nightly Bill.com bills → Firestore spend aggregation sync |
| `service/sync_aws_spend.py` | Nightly AWS Cost Explorer → Firestore spend aggregation sync |
| `mcp_server/tools.py` | Intent-aligned analytics tool handlers (core logic) |
| `mcp_server/resolver.py` | Vendor resolution pipeline, filter validation, alias support |
| `mcp_server/period_parser.py` | Deterministic period string parser |
| `mcp_server/server.py` | MCP protocol entry point (stdio transport) |
| `scripts/smoke-test.sh` | Post-deploy auth smoke tests |
| `scripts/seed_apps.py` | Seed `apps` collection in Firestore with initial app definitions |

## Supported tools

### Analytics tools (MCP-backed)

| Tool | Params | Effect |
|---|---|---|
| `vendor_lookup` | vendor | Resolve vendor by name/ID/alias, return full profile |
| `vendor_count` | filters?, group_by? | Count vendors, optionally grouped by a dimension |
| `spend_total` | period?, filters? | Grand total spend for a period |
| `spend_by_vendor` | vendor?, period?, filters? | Spend for one vendor (monthly) or all (ranked) |
| `spend_by_dimension` | dimension, period?, filters? | Spend grouped by a dimension |
| `top_vendors` | n, period?, filters? | Top N vendors by spend |

### Write tools

| Tool | Params | Effect |
|---|---|---|
| `add_vendor` | name (+ optional fields) | Creates `vendors/{id}` doc in Firestore |
| `delete_vendor` | identifier | Returns confirmation prompt. Blocked for Bill.com-synced vendors |
| `modify_vendor` | identifier | Opens edit modal in UI |
| `hide_vendor` | identifier, hide | Toggles hide flag — excludes vendor from analytics |

### Live API tool

| Tool | Params | Effect |
|---|---|---|
| `execute_python` | code | Runs sandboxed Python for Bill.com/AWS API queries |

## Vendor resolution pipeline

All tools that accept a `vendor` parameter use a single shared
`resolve_vendor()` function in `mcp_server/resolver.py`:

1. Exact document-ID match
2. Exact name match (case-insensitive)
3. Alias match (`aliases` array field on vendor docs)
4. Normalised match (strip punctuation, collapse whitespace)
5. Token/fuzzy match (all query tokens present in vendor name)
6. Single match → `ok` with `vendor_id`
7. Multiple matches → `ambiguous` with candidates
8. No match → `not_found`

After resolution, all downstream logic uses `vendor_id`, never the raw input.

## Parameter classification

**Enum (hardcoded in tool schema):**
- `paymentMethod`: Check, ACH, CreditCard, Wire, PayPal
- `accountType`: Business, Individual
- `track1099`: true, false
- `billingFrequency`: monthly, annual, usage-based
- `toolCall`: billcom, aws-ce, manual

**Resolve (validated against Firestore data at query time):**
- `vendor` — full resolution pipeline
- `department` — validated against distinct values
- `owner` — validated against distinct values
- `period` — deterministic parser (YYYY-MM, YYYY-QN, YYYY-HN, YYYY, YTD, last-N-months)

## Firestore `vendors` schema

Unified collection — all vendors regardless of source.

**Synced from Bill.com (nightly):** `id`, `name`, `billcomId`, `nameLower`,
`paymentMethod`, `accountType`, `track1099`, `toolCall`, `lastSyncedAt`

**App-managed:** `owner`, `secondaryOwner`, `department`, `purpose`,
`spendType`, `hide`, `aliases`

**Contract fields:** `contractStartDate`, `contractEndDate`,
`contractLengthMonths`, `autoRenew`, `renewalRate`, `renewalNoticeDays`,
`billingFrequency`, `terminationTerms`

No PII stored (no email, phone, address, taxId).

## Firestore `apps` schema

Top-level collection defining app entries and their permission configuration.
Doc ID is the app slug (e.g., `stocks`, `system_administration`).

| Field | Type | Purpose |
|-------|------|---------|
| `id` | `string` | App slug (matches doc ID) |
| `label` | `string` | Display name |
| `path` | `string` | URL path prefix (e.g., `/stocks/`) |
| `type` | `"app" \| "admin"` | Whether it's a regular app or an admin app |
| `granting_roles` | `string[]` | Roles that grant access to this app |
| `sort_order` | `number` | Display ordering |

Seeded via `scripts/seed_apps.py`. The `PATCH /apps/{app_id}` endpoint allows admins to modify `label`, `granting_roles`, and `sort_order` at runtime.

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
  12 months of monthly unblended costs. `toolCall: "aws-ce"`. ~12 docs.

## Delete guard

Bill.com-synced vendors (docs with a `billcomId`) cannot be deleted — they
would be re-created on the next nightly sync. The guard is enforced in both
the `delete_vendor` tool handler and the `DELETE /vendors/{id}` REST endpoint.
Users can hide vendors from spend analysis instead via `hide_vendor`.

## Caller context (spend permissions)

All MCP analytics tool handlers accept an optional `caller_context` with
`allowed_vendor_ids` and `is_finance_admin`. Spend tools filter results to
the caller's allowed vendor list. Finance admins bypass filtering.

`_build_caller_context` resolves the caller's effective vendor set via
`resolve_effective_vendor_ids` in `firestore_client.py` — combining
`allowed_departments`, `allowed_vendor_ids`, and `denied_vendor_ids` from
the user doc. Filtering is always active; there is no feature flag gate.

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
| `GET` | `/spend` | Required | Monthly spend by vendor. `vendor_ids` optional — omit to return all accessible vendors |
| `GET` | `/vendors` | Required | List all vendors with full field set |
| `DELETE` | `/vendors/{vendor_id}` | Required | Delete a vendor (blocked for Bill.com-synced vendors) |
| `PATCH` | `/vendors/{vendor_id}` | Required | Partial update a vendor |
| `GET` | `/apps` | Required | List all app definitions (from `apps` collection) |
| `PATCH` | `/apps/{app_id}` | `admin` | Update app label, granting roles, or sort order |
| `GET` | `/users?role=...` | Required | List users (optional role filter) |
| `GET` | `/users/{email}` | Required | Single user detail with resolved vendor names |
| `POST` | `/users` | `admin` | Create a new user |
| `PATCH` | `/users/{email}` | `admin` or `finance_admin` | Update user roles/name (`admin`) or access fields (`finance_admin`) |
| `DELETE` | `/users/{email}` | `admin` | Delete a user |

## Spend visualization threshold

The `GET /spend` endpoint accepts an optional `vendor_ids` query parameter.
When omitted, it returns spend for all vendors the caller has access to. This
lets the frontend avoid HTTP 414 errors when the selected vendor set is large.

The **vendors** frontend applies a two-tier threshold (both set to **30**):

1. **URL threshold** — if more than 30 vendor IDs would be sent, the frontend
   omits `vendor_ids` from the request and filters the response client-side to
   match the user's selection.
2. **Visualization threshold** — after fetching, the frontend ranks vendors by
   total spend across the requested date range. Only the top 30 are shown
   individually; the rest are aggregated into an **"Other"** bucket displayed
   in neutral gray (`#b0b0b0`).

Both thresholds are defined in the `vendors` app:
- URL threshold: `VENDOR_URL_THRESHOLD` in `src/fetchVendorSpend.ts`
- Visualization grouping: `MAX_VENDORS` in `src/groupSpendRows.ts`

## MCP server (standalone)

The analytics tools can be run as a standalone MCP server for Cursor or other
MCP clients:

```bash
source .venv/bin/activate
python -m mcp_server
```

This starts a stdio-transport MCP server exposing the six analytics tools.
Requires GCP Application Default Credentials for Firestore access.
