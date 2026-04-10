#!/usr/bin/env bash
#
# Upload a pytest JSON report to GCS.
#
# Usage:
#   ./scripts/upload-test-results.sh [APP] [FILE]
#
# Defaults:
#   APP  = agent
#   FILE = test-results.json
#
# Uploads to:
#   gs://haderach-app-artifacts/test-results/$APP/$TIMESTAMP.json
#   gs://haderach-app-artifacts/test-results/$APP/latest.json
#
# Prerequisites:
#   - gcloud CLI authenticated (WIF in CI, local SA key for dev)
#   - test-results-publisher SA has objectAdmin on test-results/ prefix

set -euo pipefail

APP="${1:-agent}"
FILE="${2:-test-results.json}"
BUCKET="haderach-app-artifacts"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [ ! -f "$FILE" ]; then
  echo "Error: $FILE not found. Run pytest with --json-report --json-report-file=$FILE first."
  exit 1
fi

echo "Uploading $FILE → gs://$BUCKET/test-results/$APP/"

gcloud storage cp "$FILE" "gs://$BUCKET/test-results/$APP/$TIMESTAMP.json"
gcloud storage cp "$FILE" "gs://$BUCKET/test-results/$APP/latest.json"

echo "Done. Uploaded as $TIMESTAMP.json + latest.json"
