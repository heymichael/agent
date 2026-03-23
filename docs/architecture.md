# Agent Service Architecture

## Purpose

The agent service is a shared backend that accepts chat messages, calls OpenAI
with tool definitions, executes tool calls against Firestore, and returns
natural-language responses. It is consumed by frontend chat panels across
Haderach apps.

## Request flow

```text
Frontend ChatPanel
  │
  ▼
POST /agent/api/chat  (Firebase Hosting rewrite → Cloud Run)
  │
  ▼
FastAPI app.py
  │
  ├─► OpenAI chat.completions.create (with tool definitions)
  │     │
  │     ▼
  │   Model returns tool_calls?
  │     │ yes                │ no
  │     ▼                    ▼
  │   Execute handler   Return reply
  │   (firestore_client)     to frontend
  │     │
  │     ▼
  │   Send tool result back to OpenAI → loop
  │
  ▼
Firestore (vendors collection)
```

## Module layout

| File | Responsibility |
|---|---|
| `service/app.py` | FastAPI application, `/chat` endpoint, orchestration loop |
| `service/tools.py` | OpenAI tool schemas + execution handlers |
| `service/prompts.py` | System prompt for the agent |
| `service/firestore_client.py` | Firestore read/write helpers |

## Runtime

- **Cloud Run** (`agent-api`, us-central1)
- **Image**: `us-central1-docker.pkg.dev/haderach-ai/haderach-apps/agent-api`
- **Secrets**: `OPENAI_API_KEY` injected from Secret Manager
- **Model**: configurable via `OPENAI_MODEL` env var (default: `gpt-4o-mini`)

## Firestore access

The service uses the default compute service account, which has Datastore User
access on the `haderach-ai` project. Writes go through the Admin SDK (not
subject to client Firestore rules).

## Supported tools

| Tool | Required params | Effect |
|---|---|---|
| `add_vendor` | name, category, status | Creates `vendors/{slug}` doc |
| `update_vendor` | identifier, updates | Partial update on existing doc |
| `get_vendor` | identifier | Reads and returns full doc |
