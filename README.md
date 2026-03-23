# Haderach Agent Service

Shared chat agent backend for the Haderach platform. Wraps OpenAI tool-calling
to perform CRUD operations on Firestore collections (currently: vendors).

## Architecture

See [docs/architecture.md](docs/architecture.md).

## Local development

```bash
# Create a virtualenv
python3 -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set required env vars
export OPENAI_API_KEY="sk-..."
export GOOGLE_CLOUD_PROJECT="haderach-ai"

# Run the service
uvicorn service.app:app --reload --port 8080
```

The API is available at `http://localhost:8080`. In production it's mounted at
`/agent/api/` via Firebase Hosting rewrite.

## API

### `POST /chat`

```json
{
  "messages": [
    { "role": "user", "content": "Add Datadog as an active DevOps vendor" }
  ],
  "context": { "app": "vendors" }
}
```

Response:

```json
{
  "reply": "Done — I've added Datadog as an active vendor in the DevOps category.",
  "tool_calls_executed": ["add_vendor"]
}
```

### `GET /health`

Returns `{"status": "ok"}`.

## Deployment

Merges to `main` trigger the `build-and-deploy` workflow, which builds a Docker
image, pushes it to Artifact Registry, and deploys to Cloud Run.
