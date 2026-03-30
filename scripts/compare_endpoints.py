#!/usr/bin/env python3
"""Compare production (Firestore) and local (Postgres) agent API responses.

Hits both servers with the same authenticated requests and diffs the
responses: status codes, record counts, field shapes, and values.

Usage:
    # 1. Get a token from the browser console:
    #    await firebase.auth().currentUser.getIdToken()
    #
    # 2. Run:
    python scripts/compare_endpoints.py --token "<paste>"

    # Optional: override URLs
    python scripts/compare_endpoints.py --token "..." \
        --prod https://haderach.ai/agent/api \
        --local http://localhost:8080
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

ENDPOINTS = [
    {"method": "GET", "path": "/health", "auth": False, "compare": "exact"},
    {"method": "GET", "path": "/me", "auth": True, "compare": "shape"},
    {"method": "GET", "path": "/vendors", "auth": True, "compare": "list", "key": "name"},
    {"method": "GET", "path": "/spend", "auth": True, "compare": "spend",
     "params": {"from": "2025-04", "to": "2026-03"}},
    {"method": "GET", "path": "/users", "auth": True, "compare": "list", "key": "email"},
    {"method": "GET", "path": "/users/michael@heretic.fund", "auth": True, "compare": "shape"},
    {"method": "GET", "path": "/apps", "auth": True, "compare": "list", "key": "id"},
    {"method": "GET", "path": "/vendors/AWS", "auth": True, "compare": "shape"},
]


def _fetch(base_url: str, endpoint: dict, token: str | None) -> dict:
    url = f"{base_url.rstrip('/')}{endpoint['path']}"
    headers = {}
    if endpoint.get("auth") and token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.request(
            endpoint["method"], url,
            headers=headers,
            params=endpoint.get("params"),
            timeout=30,
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return {"status": resp.status_code, "body": body, "error": None}
    except Exception as e:
        return {"status": None, "body": None, "error": str(e)}


def _sorted_keys(obj):
    if isinstance(obj, dict):
        return sorted(obj.keys())
    return []


def _compare_exact(prod, local, endpoint):
    issues = []
    if prod["body"] != local["body"]:
        issues.append(f"  Body differs: prod={json.dumps(prod['body'])} local={json.dumps(local['body'])}")
    return issues


def _compare_shape(prod, local, endpoint):
    issues = []
    if isinstance(prod["body"], dict) and isinstance(local["body"], dict):
        prod_keys = set(prod["body"].keys())
        local_keys = set(local["body"].keys())
        missing = prod_keys - local_keys
        extra = local_keys - prod_keys
        if missing:
            issues.append(f"  Keys in prod but not local: {missing}")
        if extra:
            issues.append(f"  Keys in local but not prod: {extra}")
    return issues


def _compare_list(prod, local, endpoint):
    issues = []
    key = endpoint.get("key", "id")

    prod_body = prod["body"]
    local_body = local["body"]

    if isinstance(prod_body, dict) and "data" in prod_body:
        prod_body = prod_body["data"]
    if isinstance(local_body, dict) and "data" in local_body:
        local_body = local_body["data"]

    if not isinstance(prod_body, list) or not isinstance(local_body, list):
        issues.append(f"  Expected lists, got prod={type(prod_body).__name__} local={type(local_body).__name__}")
        return issues

    issues.append(f"  Record count: prod={len(prod_body)} local={len(local_body)}")

    prod_keys_set = {r.get(key) for r in prod_body if isinstance(r, dict)}
    local_keys_set = {r.get(key) for r in local_body if isinstance(r, dict)}

    missing = prod_keys_set - local_keys_set
    extra = local_keys_set - prod_keys_set

    if missing:
        sample = sorted(missing)[:10]
        issues.append(f"  In prod but not local ({len(missing)}): {sample}")
    if extra:
        sample = sorted(extra)[:10]
        issues.append(f"  In local but not prod ({len(extra)}): {sample}")

    if not missing and not extra and len(prod_body) == len(local_body):
        if prod_body and local_body:
            prod_fields = set(prod_body[0].keys()) if isinstance(prod_body[0], dict) else set()
            local_fields = set(local_body[0].keys()) if isinstance(local_body[0], dict) else set()
            field_missing = prod_fields - local_fields
            field_extra = local_fields - prod_fields
            if field_missing:
                issues.append(f"  Fields in prod but not local: {field_missing}")
            if field_extra:
                issues.append(f"  Fields in local but not prod: {field_extra}")

    return issues


def _compare_spend(prod, local, endpoint):
    issues = []

    prod_data = prod["body"].get("data", []) if isinstance(prod["body"], dict) else []
    local_data = local["body"].get("data", []) if isinstance(local["body"], dict) else []

    issues.append(f"  Spend rows: prod={len(prod_data)} local={len(local_data)}")

    if prod_data and local_data:
        prod_vendors = {r.get("vendor") or r.get("vendorId") for r in prod_data}
        local_vendors = {r.get("vendor") or r.get("vendorId") for r in local_data}
        issues.append(f"  Unique vendors with spend: prod={len(prod_vendors)} local={len(local_vendors)}")

        prod_total = sum(r.get("amount", 0) for r in prod_data)
        local_total = sum(r.get("amount", 0) for r in local_data)
        issues.append(f"  Total spend: prod=${prod_total:,.2f} local=${local_total:,.2f}")
        if abs(prod_total - local_total) > 0.01:
            issues.append(f"  ** SPEND MISMATCH: diff=${abs(prod_total - local_total):,.2f}")

        missing_vendors = prod_vendors - local_vendors
        extra_vendors = local_vendors - prod_vendors
        if missing_vendors:
            sample = sorted(missing_vendors)[:5]
            issues.append(f"  Vendors in prod but not local: {sample}")
        if extra_vendors:
            sample = sorted(extra_vendors)[:5]
            issues.append(f"  Vendors in local but not prod: {sample}")

    return issues


COMPARATORS = {
    "exact": _compare_exact,
    "shape": _compare_shape,
    "list": _compare_list,
    "spend": _compare_spend,
}


def run(prod_url: str, local_url: str, token: str):
    print(f"Production: {prod_url}")
    print(f"Local:      {local_url}")
    print(f"Token:      ...{token[-12:]}")
    print()

    all_pass = True

    for ep in ENDPOINTS:
        label = f"{ep['method']} {ep['path']}"
        if ep.get("params"):
            label += f"?{'&'.join(f'{k}={v}' for k, v in ep['params'].items())}"

        with ThreadPoolExecutor(max_workers=2) as pool:
            prod_future = pool.submit(_fetch, prod_url, ep, token)
            local_future = pool.submit(_fetch, local_url, ep, token)
            prod_result = prod_future.result()
            local_result = local_future.result()

        print(f"{'─' * 60}")
        print(f"{label}")
        print(f"  Status: prod={prod_result['status']} local={local_result['status']}")

        if prod_result["error"]:
            print(f"  PROD ERROR: {prod_result['error']}")
            all_pass = False
            continue
        if local_result["error"]:
            print(f"  LOCAL ERROR: {local_result['error']}")
            all_pass = False
            continue

        if prod_result["status"] != local_result["status"]:
            print(f"  ** STATUS MISMATCH")
            all_pass = False
            continue

        comparator = COMPARATORS.get(ep["compare"], _compare_shape)
        issues = comparator(prod_result, local_result, ep)
        for issue in issues:
            if "MISMATCH" in issue or "but not" in issue.upper():
                all_pass = False
            print(issue)

    print(f"\n{'─' * 60}")
    if all_pass:
        print("RESULT: All endpoints match.")
    else:
        print("RESULT: Differences detected — review above.")
    return 0 if all_pass else 1


def main():
    parser = argparse.ArgumentParser(description="Compare prod vs local agent API responses")
    parser.add_argument("--token", required=True, help="Firebase ID token")
    parser.add_argument("--prod", default="https://haderach.ai/agent/api", help="Production base URL")
    parser.add_argument("--local", default="http://localhost:8080", help="Local base URL")
    args = parser.parse_args()

    sys.exit(run(args.prod, args.local, args.token))


if __name__ == "__main__":
    main()
