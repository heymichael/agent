#!/usr/bin/env bash
#
# Post-deploy smoke tests for the agent service.
# Verifies the service is alive and the auth gate is active.
#
# Usage: ./scripts/smoke-test.sh <service-url>
# Example: ./scripts/smoke-test.sh https://agent-api-dtugaxyjxa-uc.a.run.app

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <service-url>" >&2
  exit 1
fi

BASE_URL="${1%/}"
FAILED=0

check() {
  local label="$1" method="$2" path="$3" expected="$4"
  shift 4
  local status
  status=$(curl -s -o /dev/null -w '%{http_code}' -X "$method" "$@" "${BASE_URL}${path}")
  if [[ "$status" == "$expected" ]]; then
    echo "PASS  $label (HTTP $status)"
  else
    echo "FAIL  $label — expected $expected, got $status"
    FAILED=1
  fi
}

echo "Smoke-testing ${BASE_URL} …"
echo

check "health returns 200" \
  GET /health 200

check "unauthenticated request returns 401" \
  POST /chat 401 \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"smoke"}]}'

check "garbage token returns 401" \
  POST /chat 401 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer not-a-real-token" \
  -d '{"messages":[{"role":"user","content":"smoke"}]}'

echo
if [[ $FAILED -ne 0 ]]; then
  echo "SMOKE TESTS FAILED"
  exit 1
fi
echo "All smoke tests passed."
