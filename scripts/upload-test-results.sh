#!/usr/bin/env bash
#
# Upload a pytest JSON report to GCS.
#
# Usage:
#   ./scripts/upload-test-results.sh [APP] [FILE]
#
# Defaults:
#   APP  = agent
#   FILE = .report.json
#
# Uploads to:
#   gs://haderach-app-artifacts/test-results/$APP/$TIMESTAMP.json
#   gs://haderach-app-artifacts/test-results/$APP/latest.json
#
# Authentication:
#   Uses the test-results-publisher SA key at test-results-publisher-key.json.
#   Falls back to default gcloud credentials if the key is missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_ROOT="$(dirname "$SCRIPT_DIR")"

APP="${1:-agent}"
FILE="${2:-.report.json}"
BUCKET="haderach-app-artifacts"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SA_KEY="$AGENT_ROOT/test-results-publisher-key.json"

if [ ! -f "$FILE" ]; then
  echo "Error: $FILE not found. Run pytest with --json-report --json-report-file=$FILE first."
  exit 1
fi

if [ -f "$SA_KEY" ]; then
  echo "Authenticating as test-results-publisher SA..."
  gcloud auth activate-service-account --key-file="$SA_KEY" --quiet 2>/dev/null
fi

echo "Uploading $FILE → gs://$BUCKET/test-results/$APP/"

gcloud storage cp "$FILE" "gs://$BUCKET/test-results/$APP/$TIMESTAMP.json"
gcloud storage cp "$FILE" "gs://$BUCKET/test-results/$APP/latest.json"

echo "Done. Uploaded as $TIMESTAMP.json + latest.json"
