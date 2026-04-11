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


def _iter_user_props(props: list) -> list[tuple[str, dict]]:
    """Normalise user_properties from json-report.

    pytest-json-report stores properties as single-key dicts
    (``[{"cost": {...}}, ...]``), but in-memory pytest reports use
    tuples (``[("cost", {...}), ...]``).  This helper yields
    ``(key, value)`` pairs regardless of format.
    """
    result = []
    for prop in props:
        if isinstance(prop, dict):
            result.extend(prop.items())
        elif isinstance(prop, (list, tuple)) and len(prop) == 2:
            result.append((prop[0], prop[1]))
    return result


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


_VALID_GROUP_BY = {"tool", "domain", "module", "capability", "agent"}


def _extract_test_meta(test: dict) -> dict:
    """Pull stochastic, cost, and duration metadata from a single test entry."""
    stochastic = None
    cost_usd = 0.0
    for key, val in _iter_user_props(test.get("user_properties", [])):
        if key == "stochastic" and isinstance(val, dict):
            stochastic = val
        elif key == "cost" and isinstance(val, dict):
            cost_usd = val.get("cost_usd") or 0.0

    call = test.get("call") or {}
    duration = call.get("duration", 0.0)

    if stochastic:
        runs = stochastic.get("runs", 1)
        passed = stochastic.get("passed", 0)
        pass_rate = stochastic.get("pass_rate", 0.0)
        stochastic_passed = stochastic.get("stochastic_passed", False)
        is_100 = stochastic_passed and pass_rate == 1.0
        is_partial = stochastic_passed and pass_rate < 1.0
        total_cost = stochastic.get("cost_usd", 0.0) or cost_usd
    else:
        outcome = test.get("outcome", "failed")
        runs = 1
        passed = 1 if outcome == "passed" else 0
        pass_rate = 1.0 if outcome == "passed" else 0.0
        is_100 = outcome == "passed"
        is_partial = False
        total_cost = cost_usd

    return {
        "runs": runs,
        "passed": passed,
        "pass_rate": pass_rate,
        "is_100": is_100,
        "is_partial": is_partial,
        "duration_s": duration,
        "cost_usd": total_cost,
    }


def handle_test_summary(group_by: str = "tool", run: str | None = None, app: str = "agent") -> dict:
    """Grouped stochastic test summary from a json-report.

    Groups tests by the chosen tag dimension and returns per-group stats
    plus a totals row computed from deduplicated tests.

    When ``run`` is provided, fetches that historical run from GCS first.
    """
    if group_by not in _VALID_GROUP_BY:
        return {"error": f"Invalid group_by '{group_by}'. Must be one of: {sorted(_VALID_GROUP_BY)}"}

    if run:
        err = _fetch_run(run, app)
        if err:
            return {"error": err}

    report = _read_report()
    if not report:
        return {
            "report_available": False,
            "note": "No .report.json found. Run tests with --json-report first.",
        }

    tests = report.get("tests", [])
    prefix = f"{group_by}_"

    groups: dict[str, list[dict]] = {}
    all_metas: list[dict] = []

    for t in tests:
        meta = _extract_test_meta(t)
        all_metas.append(meta)

        keywords = t.get("keywords", [])
        matched = [kw[len(prefix):] for kw in keywords if kw.startswith(prefix)]
        for group_name in matched:
            groups.setdefault(group_name, []).append(meta)

    def _aggregate(metas: list[dict]) -> dict:
        total_runs = sum(m["runs"] for m in metas)
        total_passed = sum(m["passed"] for m in metas)
        return {
            "tests_run": len(metas),
            "passed_100pct": sum(1 for m in metas if m["is_100"]),
            "passed_partial": sum(1 for m in metas if m["is_partial"]),
            "pass_rate": round(total_passed / total_runs, 3) if total_runs else 0.0,
            "duration_s": round(sum(m["duration_s"] for m in metas), 2),
            "cost_usd": round(sum(m["cost_usd"] for m in metas), 4),
        }

    rows = []
    for name in sorted(groups):
        row = _aggregate(groups[name])
        row["group"] = name
        rows.append(row)

    totals = _aggregate(all_metas)
    totals["group"] = "TOTAL"
    rows.append(totals)

    return {
        "report_available": True,
        "run_name": report.get("run_name"),
        "group_by": group_by,
        "created": report.get("created"),
        "rows": rows,
    }


def _parse_run_entry(filename: str) -> dict:
    """Parse a GCS blob filename into a structured run entry.

    Filenames are ``{timestamp}.json`` or ``{timestamp}--{run_name}.json``.
    """
    stem = filename.replace(".json", "")
    if "--" in stem:
        ts, name = stem.split("--", 1)
        return {"timestamp": ts, "run_name": name}
    return {"timestamp": stem, "run_name": None}


def _normalize_name(s: str) -> str:
    """Collapse a scenario or test name to lowercase alpha-only for matching."""
    return re.sub(r"[^a-z]", "", s.lower())


def _build_prompt_map() -> dict[str, str]:
    """Map normalized scenario names to their user prompt from feature files."""
    prompts: dict[str, str] = {}
    for fpath in sorted(FEATURES_DIR.glob("*.feature")):
        current_scenario = ""
        for line in fpath.read_text().splitlines():
            stripped = line.strip()
            if re.match(r"Scenario( Outline)?:", stripped):
                name = re.sub(r"Scenario( Outline)?:\s*", "", stripped)
                current_scenario = _normalize_name(name)
            elif current_scenario and re.match(
                r'When the user (says|uploads)', stripped,
            ):
                match = re.search(r'"(.+?)"', stripped)
                if match and "<" not in match.group(1):
                    prompts[current_scenario] = match.group(1)
                    current_scenario = ""
    return prompts


def handle_failure_detail() -> dict:
    """Return detailed info for every failed test in the latest local report."""
    report = _read_report()
    if not report:
        return {
            "report_available": False,
            "note": "No .report.json found. Run tests with --json-report first.",
        }

    prompt_map = _build_prompt_map()
    failures = []
    for t in report.get("tests", []):
        meta = _extract_test_meta(t)
        if meta["is_100"] or meta["is_partial"]:
            continue

        test_name = _nodeid_to_scenario(t["nodeid"])
        call = t.get("call") or {}
        crash_msg = call.get("crash", {}).get("message", "")
        if len(crash_msg) > 150:
            crash_msg = crash_msg[:150] + "…"

        normalized = _normalize_name(test_name)
        prompt = None
        for key, val in prompt_map.items():
            if key in normalized or normalized in key:
                prompt = val
                break

        failures.append({
            "test": test_name,
            "prompt": prompt,
            "assertion": crash_msg,
            "passed": meta["passed"],
            "runs": meta["runs"],
        })

    return {
        "total_failures": len(failures),
        "failures": failures,
    }


def handle_test_history(app: str = "agent", limit: int = 10) -> dict:
    """Fetch historical test results from GCS."""
    client, err = _gcs_client()
    if err:
        return {"available": False, "error": err}

    prefix = f"test-results/{app}/"
    blobs = list(client.bucket(GCS_BUCKET).list_blobs(prefix=prefix))

    filenames = sorted(
        [b.name.split("/")[-1] for b in blobs
         if b.name.endswith(".json") and "latest.json" not in b.name],
        reverse=True,
    )[:limit]

    if not filenames:
        return {
            "available": True,
            "app": app,
            "runs": [],
            "note": "No historical results found. Publish results first.",
        }

    runs = [_parse_run_entry(f) for f in filenames]

    return {
        "available": True,
        "app": app,
        "total_runs": len(runs),
        "runs": runs,
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
    run_name: str | None = None,
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

    if run_name:
        cmd.extend(["--run-name", run_name])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,
            cwd=str(AGENT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"error": "Test run timed out after 7200s"}

    report = _read_report()
    if not report:
        return {
            "exit_code": proc.returncode,
            "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
            "stderr_tail": proc.stderr[-1000:] if proc.stderr else "",
            "error": "No json-report generated",
        }

    publish_result = handle_publish_results()

    summary = report.get("summary", {})
    tests = report.get("tests", [])

    total_cost = 0.0
    results = []
    for t in tests:
        cost = 0.0
        stochastic_data = None
        for key, val in _iter_user_props(t.get("user_properties", [])):
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

    result = {
        "exit_code": proc.returncode,
        "total": summary.get("total", 0),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "duration_seconds": round(report.get("duration", 0), 2),
        "total_cost_usd": round(total_cost, 4),
        "tests": results,
    }
    if publish_result.get("published"):
        result["published"] = publish_result.get("paths", [])
    else:
        result["publish_error"] = publish_result.get("error", "unknown")
    return result


def _gcs_client():
    """Create a GCS client authenticated with the test-results-publisher SA key."""
    from google.cloud import storage as gcs
    from google.oauth2 import service_account

    sa_key = AGENT_ROOT / "test-results-publisher-key.json"
    if not sa_key.exists():
        return None, "SA key not found at test-results-publisher-key.json"
    creds = service_account.Credentials.from_service_account_file(str(sa_key))
    return gcs.Client(credentials=creds, project=creds.project_id), None


def _fetch_run(run: str, app: str = "agent") -> str | None:
    """Download a historical run from GCS into .report.json.

    ``run`` can be a full filename (``2026-04-10T14:30:00Z--name.json``),
    a stem without ``.json``, or a substring that uniquely matches a blob.
    Returns an error string on failure, None on success.
    """
    client, err = _gcs_client()
    if err:
        return err

    prefix = f"test-results/{app}/"
    blobs = [
        b for b in client.bucket(GCS_BUCKET).list_blobs(prefix=prefix)
        if b.name.endswith(".json") and "latest.json" not in b.name
    ]

    needle = run if run.endswith(".json") else run + ".json"
    exact = [b for b in blobs if b.name.endswith(f"/{needle}")]
    if not exact:
        exact = [b for b in blobs if run in b.name]

    if len(exact) == 0:
        return f"No run matching '{run}' found in GCS."
    if len(exact) > 1:
        names = [b.name.split("/")[-1] for b in exact[:5]]
        return f"Ambiguous — {len(exact)} runs match '{run}': {names}"

    data = exact[0].download_as_text()
    REPORT_PATH.write_text(data)
    return None


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

    report = _read_report()
    run_name = report.get("run_name") if report else None
    ts_stem = f"{ts}--{run_name}" if run_name else ts

    ts_blob = bucket.blob(f"{prefix}/{ts_stem}.json")
    ts_blob.upload_from_string(data, content_type="application/json")

    latest_blob = bucket.blob(f"{prefix}/latest.json")
    latest_blob.upload_from_string(data, content_type="application/json")

    return {
        "published": True,
        "app": app,
        "run_name": run_name,
        "timestamp": ts,
        "paths": [f"gs://{GCS_BUCKET}/{prefix}/{ts_stem}.json", f"gs://{GCS_BUCKET}/{prefix}/latest.json"],
    }
