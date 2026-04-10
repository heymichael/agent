"""Tool handlers for the test-status MCP server.

All tools operate against local filesystem artifacts:
  - tests/features/*.feature  (BDD scenarios)
  - .report.json              (pytest-json-report output)
  - tests/                    (unit test files)

GCS upload uses the test-results-publisher SA key for authentication.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent
FEATURES_DIR = AGENT_ROOT / "tests" / "features"
REPORT_PATH = AGENT_ROOT / ".report.json"
VENV_PYTHON = AGENT_ROOT / ".venv" / "bin" / "python"
GCS_BUCKET = "haderach-app-artifacts"


# ── Feature file parser ─────────────────────────────────────────────────


def _parse_features() -> list[dict]:
    """Parse all .feature files and return structured scenario data."""
    results = []
    for fpath in sorted(FEATURES_DIR.glob("*.feature")):
        feature_tags: list[str] = []
        feature_name = ""
        current_scenario_tags: list[str] = []
        scenarios: list[dict] = []

        for line in fpath.read_text().splitlines():
            stripped = line.strip()

            if stripped.startswith("@") and not stripped.startswith("@skip"):
                tags = re.findall(r"@(\w+)", stripped)
                if not feature_name:
                    feature_tags = tags
                else:
                    current_scenario_tags.extend(tags)

            elif stripped.startswith("Feature:"):
                feature_name = stripped[len("Feature:"):].strip()

            elif re.match(r"Scenario( Outline)?:", stripped):
                name = re.sub(r"Scenario( Outline)?:\s*", "", stripped)
                all_tags = feature_tags + current_scenario_tags
                scenarios.append({
                    "name": name,
                    "tags": all_tags,
                    "feature": feature_name,
                    "file": fpath.name,
                })
                current_scenario_tags = []

            elif stripped and not stripped.startswith(
                ("Given", "When", "Then", "And", "But", "#",
                 "Background", "Examples", "|", "\"")
            ):
                current_scenario_tags = []

        results.extend(scenarios)
    return results


def _matches_filters(
    tags: list[str],
    agent: str | None,
    domain: str | None,
    module: str | None,
    capability: str | None,
    tool: str | None,
) -> bool:
    """Check if a scenario's tags match the requested filters."""
    tag_set = set(tags)
    if agent and f"agent_{agent}" not in tag_set:
        return False
    if domain and f"domain_{domain}" not in tag_set:
        return False
    if module and f"module_{module}" not in tag_set:
        return False
    if capability and f"capability_{capability}" not in tag_set:
        return False
    if tool and f"tool_{tool}" not in tag_set:
        return False
    return True


# ── JSON report reader ───────────────────────────────────────────────────


def _read_report(path: Path | None = None) -> dict | None:
    rpath = path or REPORT_PATH
    if not rpath.exists():
        return None
    try:
        return json.loads(rpath.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _nodeid_to_scenario(nodeid: str) -> str:
    """Extract a readable test name from a pytest nodeid."""
    parts = nodeid.rsplit("::", 1)
    return parts[-1] if len(parts) > 1 else nodeid


# ── Query tool handlers ─────────────────────────────────────────────────


def handle_list_scenarios(
    agent: str | None = None,
    domain: str | None = None,
    module: str | None = None,
    capability: str | None = None,
    tool: str | None = None,
) -> dict:
    """List BDD scenarios from .feature files, filtered by tag dimensions."""
    scenarios = _parse_features()
    filtered = [
        s for s in scenarios
        if _matches_filters(s["tags"], agent, domain, module, capability, tool)
    ]
    by_feature: dict[str, list] = {}
    for s in filtered:
        by_feature.setdefault(s["feature"], []).append({
            "name": s["name"],
            "tags": s["tags"],
        })
    return {
        "total": len(filtered),
        "features": {k: {"count": len(v), "scenarios": v} for k, v in by_feature.items()},
    }


def handle_scenario_coverage(
    agent: str | None = None,
    domain: str | None = None,
    module: str | None = None,
    capability: str | None = None,
    tool: str | None = None,
) -> dict:
    """Join BDD scenarios against the latest json-report to show pass/fail/cost."""
    scenarios = _parse_features()
    filtered = [
        s for s in scenarios
        if _matches_filters(s["tags"], agent, domain, module, capability, tool)
    ]

    report = _read_report()
    if not report:
        return {
            "total_scenarios": len(filtered),
            "report_available": False,
            "note": "No .report.json found. Run tests with --json-report first.",
        }

    test_map: dict[str, dict] = {}
    for t in report.get("tests", []):
        name = _nodeid_to_scenario(t["nodeid"])
        test_map[name] = t

    results = []
    passed = failed = not_run = 0
    total_cost = 0.0

    for s in filtered:
        test_name = re.sub(r"[^\w]", "_", s["name"].lower()).strip("_")
        match = None
        for key, val in test_map.items():
            if test_name in key.lower().replace("-", "_"):
                match = val
                break

        if match:
            outcome = match.get("outcome", "unknown")
            cost = 0.0
            for prop_key, prop_val in match.get("user_properties", []):
                if prop_key == "cost" and isinstance(prop_val, dict):
                    cost = prop_val.get("cost_usd") or 0.0
            total_cost += cost

            if outcome == "passed":
                passed += 1
            else:
                failed += 1

            results.append({
                "scenario": s["name"],
                "feature": s["feature"],
                "outcome": outcome,
                "cost_usd": round(cost, 6),
            })
        else:
            not_run += 1
            results.append({
                "scenario": s["name"],
                "feature": s["feature"],
                "outcome": "not_run",
                "cost_usd": 0,
            })

    return {
        "total_scenarios": len(filtered),
        "passed": passed,
        "failed": failed,
        "not_run": not_run,
        "total_cost_usd": round(total_cost, 4),
        "scenarios": results,
    }


def handle_test_summary() -> dict:
    """Aggregate test results from the latest json-report."""
    report = _read_report()
    if not report:
        return {
            "report_available": False,
            "note": "No .report.json found. Run tests with --json-report first.",
        }

    summary = report.get("summary", {})
    tests = report.get("tests", [])

    total_cost = 0.0
    stochastic_cost = 0.0
    for t in tests:
        for key, val in t.get("user_properties", []):
            if key == "cost" and isinstance(val, dict):
                total_cost += val.get("cost_usd") or 0
            if key == "stochastic" and isinstance(val, dict):
                stochastic_cost += val.get("cost_usd") or 0

    scenarios = _parse_features()

    return {
        "report_available": True,
        "total_tests": summary.get("total", 0),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "errors": summary.get("error", 0),
        "skipped": summary.get("skipped", 0),
        "duration_seconds": round(report.get("duration", 0), 2),
        "total_cost_usd": round(total_cost, 4),
        "stochastic_cost_usd": round(stochastic_cost, 4),
        "bdd_scenarios_defined": len(scenarios),
        "created": report.get("created"),
    }


def handle_list_unit_tests(
    file: str | None = None,
    search: str | None = None,
) -> dict:
    """List unit tests by running pytest --collect-only."""
    cmd = [str(VENV_PYTHON), "-m", "pytest", "--collect-only", "-q"]

    if file:
        cmd.append(f"tests/{file}")
    else:
        cmd.append("tests/")
        cmd.extend(["--ignore=tests/step_defs", "--ignore=tests/features"])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(AGENT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"error": "Collection timed out after 30s"}

    lines = proc.stdout.strip().splitlines()
    tests = [l for l in lines if "::" in l and not l.startswith(("=", "-", " "))]

    if search:
        pattern = search.lower()
        tests = [t for t in tests if pattern in t.lower()]

    by_file: dict[str, list[str]] = {}
    for t in tests:
        parts = t.split("::", 1)
        fname = parts[0]
        tname = parts[1] if len(parts) > 1 else t
        by_file.setdefault(fname, []).append(tname)

    return {
        "total": len(tests),
        "files": {k: {"count": len(v), "tests": v} for k, v in by_file.items()},
    }


def handle_test_history(app: str = "agent", limit: int = 10) -> dict:
    """Fetch historical test results from GCS."""
    client, err = _gcs_client()
    if err:
        return {"available": False, "error": err}

    prefix = f"test-results/{app}/"
    blobs = list(client.bucket(GCS_BUCKET).list_blobs(prefix=prefix))

    runs = sorted(
        [b.name.split("/")[-1].replace(".json", "") for b in blobs
         if b.name.endswith(".json") and "latest.json" not in b.name],
        reverse=True,
    )[:limit]

    if not runs:
        return {
            "available": True,
            "app": app,
            "runs": [],
            "note": "No historical results found. Publish results first.",
        }

    return {
        "available": True,
        "app": app,
        "total_runs": len(runs),
        "runs": runs,
    }


def handle_list_playwright_tests() -> dict:
    """List Playwright test results from GCS. (Deferred — GCS not yet provisioned.)"""
    return {
        "available": False,
        "note": "Playwright GCS upload not yet configured (Phase 6).",
    }


# ── Execution tool handlers ─────────────────────────────────────────────


def _build_marker_expr(
    agent: str | None = None,
    domain: str | None = None,
    module: str | None = None,
    capability: str | None = None,
    tool: str | None = None,
) -> str | None:
    """Build a pytest -m expression from tag filters."""
    parts = []
    if agent:
        parts.append(f"agent_{agent}")
    if domain:
        parts.append(f"domain_{domain}")
    if module:
        parts.append(f"module_{module}")
    if capability:
        parts.append(f"capability_{capability}")
    if tool:
        parts.append(f"tool_{tool}")
    return " and ".join(parts) if parts else None


def handle_run_scenarios(
    agent: str | None = None,
    domain: str | None = None,
    module: str | None = None,
    capability: str | None = None,
    tool: str | None = None,
    stochastic: bool = False,
    runs: int | None = None,
) -> dict:
    """Run BDD scenarios matching the given filters and return structured results."""
    cmd = [
        str(VENV_PYTHON), "-m", "pytest",
        "tests/step_defs/",
        "--json-report", "--json-report-file=.report.json",
        "-v",
    ]

    marker = _build_marker_expr(agent, domain, module, capability, tool)
    if marker:
        cmd.extend(["-m", marker])

    if stochastic:
        cmd.append("--stochastic")
        if runs:
            cmd.extend(["--stochastic-runs", str(runs)])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(AGENT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"error": "Test run timed out after 600s"}

    report = _read_report()
    if not report:
        return {
            "exit_code": proc.returncode,
            "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
            "stderr_tail": proc.stderr[-1000:] if proc.stderr else "",
            "error": "No json-report generated",
        }

    summary = report.get("summary", {})
    tests = report.get("tests", [])

    total_cost = 0.0
    results = []
    for t in tests:
        cost = 0.0
        stochastic_data = None
        for key, val in t.get("user_properties", []):
            if key == "cost" and isinstance(val, dict):
                cost = val.get("cost_usd") or 0
            if key == "stochastic" and isinstance(val, dict):
                stochastic_data = val
        total_cost += cost
        entry = {
            "test": _nodeid_to_scenario(t["nodeid"]),
            "outcome": t.get("outcome"),
            "cost_usd": round(cost, 6),
        }
        if stochastic_data:
            entry["stochastic"] = stochastic_data
            total_cost += stochastic_data.get("cost_usd", 0)
        results.append(entry)

    return {
        "exit_code": proc.returncode,
        "total": summary.get("total", 0),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "duration_seconds": round(report.get("duration", 0), 2),
        "total_cost_usd": round(total_cost, 4),
        "tests": results,
    }


def handle_run_regression(
    domain: str | None = None,
    agent: str | None = None,
) -> dict:
    """Run a full stochastic regression for a domain or agent."""
    return handle_run_scenarios(
        agent=agent,
        domain=domain,
        stochastic=True,
    )


def _gcs_client():
    """Create a GCS client authenticated with the test-results-publisher SA key."""
    from google.cloud import storage as gcs
    from google.oauth2 import service_account

    sa_key = AGENT_ROOT / "test-results-publisher-key.json"
    if not sa_key.exists():
        return None, "SA key not found at test-results-publisher-key.json"
    creds = service_account.Credentials.from_service_account_file(str(sa_key))
    return gcs.Client(credentials=creds, project=creds.project_id), None


def handle_publish_results(app: str = "agent") -> dict:
    """Publish the latest json-report to GCS (timestamped + latest)."""
    if not REPORT_PATH.exists():
        return {"error": "No .report.json to publish. Run tests first."}

    client, err = _gcs_client()
    if err:
        return {"error": err}

    bucket = client.bucket(GCS_BUCKET)
    data = REPORT_PATH.read_text()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prefix = f"test-results/{app}"

    ts_blob = bucket.blob(f"{prefix}/{ts}.json")
    ts_blob.upload_from_string(data, content_type="application/json")

    latest_blob = bucket.blob(f"{prefix}/latest.json")
    latest_blob.upload_from_string(data, content_type="application/json")

    return {
        "published": True,
        "app": app,
        "timestamp": ts,
        "paths": [f"gs://{GCS_BUCKET}/{prefix}/{ts}.json", f"gs://{GCS_BUCKET}/{prefix}/latest.json"],
    }
