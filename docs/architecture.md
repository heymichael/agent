# Agent Service Architecture

## Purpose

The agent service is a shared backend that accepts chat messages, calls OpenAI
with tool definitions, executes tool calls (SQL analytics layer, Postgres
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
  │     ├─ Analytics tools ──► MCP server module ──► Postgres (SQL)
  │     │   (vendor_lookup, vendor_count,
  │     │    spend_total, spend_by_vendor,
  │     │    spend_by_dimension, top_vendors)
  │     │
  │     ├─ Write tools ──────► Postgres (vendor CRUD via pg_client)
  │     │   (add/modify/delete_vendor)
  │     │
  │     ├─ execute_python ──► Sandbox → Bill.com API / AWS CE
  │     │
  │     ▼
  │   Send tool result back to OpenAI → loop
  │
  ▼
Cloud SQL Postgres (haderach-main)
```

## Database

All data is stored in **Cloud SQL Postgres 15** (`haderach-main` instance,
`haderach` database). Connection is via Cloud SQL Auth Proxy mounted at
`/cloudsql/` in Cloud Run, with the `DATABASE_URL` secret providing the
full connection string.

### Schema

Tables: `departments`, `roles`, `vendors`, `vendor_monthly_spend`,
`vendor_spend_detail`, `sync_job_log`, `sync_job_step`, `users`, `apps`,
`branding`

Join tables: `user_roles`, `user_allowed_departments`, `user_allowed_vendors`,
`user_denied_vendors`, `app_granting_roles`

All primary keys are UUID. All relationships enforced via foreign keys.
Full schema: `migrations/001_init.sql`.

### Key tables

**vendors** — all vendors regardless of source. Identified by `(source_system,
source_system_id)` unique natural key with UUID `id` as surrogate PK. The
`hidden_from_agent` boolean (default `false`) excludes duplicate vendors from
agent interactions while preserving their data. Schema:
`migrations/004_hidden_from_agent.sql`.

**vendor_monthly_spend** — monthly spend summaries per vendor. `date` column
stores the first of each month. Unique on `(vendor_id, date)`. Derived from
`vendor_spend_detail` via rollup for vendors with detail data.

**vendor_spend_detail** — granular spend line items per vendor. Canonical
columns (`category`, `subcategory`, `project`, `user_email`) are mapped from
vendor-native fields by each sync job. A `metadata` JSONB column holds
vendor-specific extras. Unique on `(vendor_id, date, COALESCE(category, ''),
COALESCE(subcategory, ''), COALESCE(project, ''), COALESCE(user_email, ''))`.
Schema: `migrations/002_vendor_spend_detail.sql`.

**sync_job_log** — one row per sync job execution. Tracks `job_name`,
`status` (running/completed/failed), `started_at`, `finished_at`,
`duration_ms`, `error`, and `metadata` (JSONB).

**sync_job_step** — one row per step within a sync run, FK to
`sync_job_log`. Standardized step names across all jobs: `api_fetch`,
`vendor_sync`, `detail_upsert`, `summary_upsert`, `reconcile`. Tracks
`row_count`, `duration_ms`, `error`, and step-level `metadata`.
Schema: `migrations/003_sync_job_log.sql`.

**branding** — singleton table (`CHECK (id = 1)`) storing the org logo SVG
and lockup toggle. `logo_svg TEXT` holds raw SVG markup; `show_lockup BOOLEAN`
controls whether the "Haderach" wordmark is displayed next to the logo.
Schema: `migrations/005_branding.sql`.

**users** — email-keyed user accounts. Roles via `user_roles` join table.
Access controls via `user_allowed_departments`, `user_allowed_vendors`,
`user_denied_vendors`.

**apps** — app definitions with slug, label, path. Roles via
`app_granting_roles` join table.

## MCP analytics layer

Analytics tools are backed by the `mcp_server/` module, which provides:

- **Intent-aligned tool handlers** — eight tools with clear, specific purposes
  instead of general-purpose query builders. All aggregation pushed to SQL.
- **Vendor resolution pipeline** — `resolve_vendor_by_identifier()` in
  `pg_client.py` handles exact UUID, exact name, alias, normalised, and
  pg_trgm fuzzy matching. Returns ok/ambiguous/not_found.
- **Period parser** — deterministic conversion of period strings (YYYY-MM,
  YYYY-QN, YTD, last-N-months) to month ranges
- **Filter validation** — enum fields validated at schema level, dynamic
  fields (department, owner) validated against Postgres data
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
| `service/pg_client.py` | Postgres connection pool, all CRUD queries, vendor/user/app/spend/department/role operations |
| `service/sandbox.py` | Sandboxed Python executor for LLM-generated code (120s timeout) |
| `service/billcom_auth.py` | Shared Bill.com v3 login helper |
| `service/sync_billcom.py` | Nightly Bill.com → Postgres vendor sync |
| `service/sync_billcom_spend.py` | Nightly Bill.com bills → Postgres spend aggregation sync |
| `service/sync_tracker.py` | Step-level execution tracking for sync jobs (writes to `sync_job_log` / `sync_job_step`) |
| `service/sync_aws_spend.py` | Nightly AWS Cost Explorer → detail rows + summary rollup (with step logging) |
| `mcp_server/tools.py` | Intent-aligned analytics tool handlers — SQL queries for all aggregation |
| `mcp_server/resolver.py` | Filter validation (`resolve_filter`, `validate_filters`), field-to-SQL column mapping |
| `mcp_server/period_parser.py` | Deterministic period string parser |
| `mcp_server/server.py` | MCP protocol entry point (stdio transport) |
| `scripts/smoke-test.sh` | Post-deploy auth smoke tests |
| `scripts/seed_apps.py` | Seed `apps` table with initial app definitions |
| `scripts/seed_users.py` | Seed `users` table with role assignments |
| `scripts/seed_departments.py` | Bulk-update vendor departments from CSV |
| `migrations/001_init.sql` | Full schema: all tables, indexes, constraints, seed roles |
| `migrations/002_vendor_spend_detail.sql` | Granular spend detail table with canonical columns + JSONB metadata |
| `migrations/003_sync_job_log.sql` | Sync job run + step tracking tables |
| `migrations/004_hidden_from_agent.sql` | Add `hidden_from_agent` boolean to vendors |
| `migrations/005_branding.sql` | Singleton branding table (logo SVG + lockup toggle) |

## Supported tools

### Analytics tools (SQL-backed)

| Tool | Params | Effect |
|---|---|---|
| `vendor_lookup` | vendor | Resolve vendor by name/ID/alias, return full profile |
| `vendor_count` | filters?, group_by? | Count vendors, optionally grouped by a dimension |
| `vendor_list` | filters?, limit? | List vendors matching filter criteria with key fields |
| `spend_total` | period?, filters? | Grand total spend for a period |
| `spend_by_vendor` | vendor?, period?, filters? | Spend for one vendor (monthly) or all (ranked) |
| `spend_by_dimension` | dimension, period?, filters? | Spend grouped by a dimension |
| `top_vendors` | n, period?, filters? | Top N vendors by spend |
| `spend_detail` | vendor, period, group_by?, category?, project? | Granular spend breakdown by service/SKU/project |
| `spend_detail_dimensions` | vendor, dimension? | Discover available categories/subcategories/projects for a vendor |

### Write tools

| Tool | Params | Effect |
|---|---|---|
| `add_vendor` | name (+ optional fields) | Creates a new vendor row |
| `delete_vendor` | identifier | Returns confirmation prompt. Blocked for synced vendors |
| `modify_vendor` | identifier | Opens edit modal in UI |

### Live API tool

| Tool | Params | Effect |
|---|---|---|
| `execute_python` | code | Runs sandboxed Python for Bill.com/AWS API queries |

## Vendor resolution pipeline

All tools that accept a `vendor` parameter use
`resolve_vendor_by_identifier()` in `service/pg_client.py`:

1. Exact UUID or source_system_id match
2. Exact name match (case-insensitive)
3. Alias match (`aliases` array column on vendors)
4. Normalised match (strip punctuation, collapse whitespace)
5. Token/fuzzy match (all query tokens present in vendor name)
6. Single match → `ok` with `vendor_id`
7. Multiple matches → `ambiguous` with candidates
8. No match → `not_found`

All resolution steps exclude vendors where `hidden_from_agent = true`.
Exact name matches skip the Pass 2 ambiguity check — once a vendor matches
by name, it is authoritative.

After resolution, all downstream logic uses `vendor_id`, never the raw input.

## CSV downloads

When `vendor_list` returns 10+ results, the tool response includes a `csv`
field with the full result set formatted as CSV. In `app.py`, the CSV is
stripped from the tool result before sending to OpenAI (saving tokens) and
surfaced via a `downloads` list on `ChatResponse`. Each download has
`filename`, `content`, and `mime` fields. The frontend renders a download
button that triggers a client-side blob download — no server-side file
storage required.

## Parameter classification

**Enum (hardcoded in tool schema):**
- `paymentMethod`: Check, ACH, CreditCard, Wire, PayPal
- `accountType`: Business, Individual
- `track1099`: true, false
- `billingFrequency`: monthly, annual, usage-based
- `sourceSystem`: billcom, aws-ce, gcp, manual

**Resolve (validated against Postgres data at query time):**
- `vendor` — full resolution pipeline
- `department` — validated against distinct values in departments table
- `owner` — validated against distinct values in users table
- `period` — deterministic parser (YYYY-MM, YYYY-QN, YYYY-HN, YYYY, YTD, last-N-months)

## Delete guard

Synced vendors (those with `source_system` != 'manual') cannot be deleted —
they would be re-created on the next nightly sync. The guard is enforced in
both the `delete_vendor` tool handler and the `DELETE /vendors/{id}` REST
endpoint.

## Caller context (spend permissions)

All MCP analytics tool handlers accept an optional `caller_context` with
`allowed_vendor_ids` and `is_finance_admin`. Spend tools filter results to
the caller's allowed vendor list via SQL WHERE clauses. Finance admins bypass
filtering.

`_build_caller_context` resolves the caller's effective vendor set via
`resolve_effective_vendor_ids` in `pg_client.py` — combining
`user_allowed_departments`, `user_allowed_vendors`, and `user_denied_vendors`
from the join tables. Filtering is always active; there is no feature flag gate.

## Runtime

- **Cloud Run** (`agent-api`, us-central1)
- **Cloud SQL** (`haderach-main`, Postgres 15, us-central1)
- **Image**: `us-central1-docker.pkg.dev/haderach-ai/haderach-apps/agent-api`
- **Secrets** (from Secret Manager):
  - `OPENAI_API_KEY`
  - `DATABASE_URL` (auto-managed by Terraform)
  - `VENDOR_BILL_CREDENTIALS` (Bill.com v3 API: userName, password, orgId, devKey)
  - `VENDOR_AWS_BILLING_CREDENTIALS` (AWS CE: access_key_id, secret_access_key, region)
- **Model**: configurable via `OPENAI_MODEL` env var (default: `gpt-4o-mini`)

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
| `GET` | `/branding` | None | Logo SVG and lockup flag for the app chrome |
| `GET` | `/spend` | Required | Monthly spend by vendor. `vendor_ids` optional — omit to return all accessible vendors |
| `GET` | `/vendors` | Required | List all vendors with full field set |
| `DELETE` | `/vendors/{vendor_id}` | Required | Delete a vendor (blocked for synced vendors) |
| `PATCH` | `/vendors/{vendor_id}` | Required | Partial update a vendor |
| `GET` | `/apps` | Required | List all app definitions |
| `PATCH` | `/apps/{app_id}` | `admin` | Update app label, granting roles, or sort order |
| `GET` | `/users?role=...` | Required | List users (optional role filter) |
| `GET` | `/users/{email}` | Required | Single user detail with resolved vendor names |
| `POST` | `/users` | `admin` | Create a new user |
| `PATCH` | `/users/{email}` | `admin` or `finance_admin` | Update user roles/name (`admin`) or access fields (`finance_admin`) |
| `DELETE` | `/users/{email}` | `admin` | Delete a user |

## MCP server (standalone)

The analytics tools can be run as a standalone MCP server for Cursor or other
MCP clients:

```bash
source .venv/bin/activate
DATABASE_URL="postgresql://..." python -m mcp_server
```

This starts a stdio-transport MCP server exposing the analytics tools.
Requires `DATABASE_URL` env var pointing to the Postgres instance.
