"""Stochastic runner for LLM-dependent tests.

Activated with ``--stochastic``.  Re-runs every ``@llm_live``-marked test
up to N times (``--stochastic-runs``, default 4) and applies a dual pass
criterion:

  * **4-consecutive**: stop early as soon as the test passes 4 times in a
    row (default, configurable via ``--stochastic-consecutive``).
  * **90 % floor**: if the consecutive threshold is never reached, accept
    the test when >= 90 % of all runs passed.

Non-``@llm_live`` tests run normally regardless of the flag.

Stochastic metadata (runs, passed count, pass rate, max consecutive
passes, and aggregated cost) is injected into the json-report via
``user_properties``.

Register this plugin in ``conftest.py``::

    pytest_plugins = ["tests.stochastic_plugin"]
"""

from __future__ import annotations

import pytest
from _pytest.runner import runtestprotocol


# ── CLI options ──────────────────────────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("stochastic", "Stochastic LLM test runner")
    group.addoption(
        "--stochastic",
        action="store_true",
        default=False,
        help="Re-run @llm_live tests multiple times and apply pass criteria.",
    )
    group.addoption(
        "--stochastic-runs",
        type=int,
        default=10,
        metavar="N",
        help="Maximum number of runs per @llm_live test (default: 10).",
    )
    group.addoption(
        "--stochastic-consecutive",
        type=int,
        default=4,
        metavar="N",
        help="Consecutive passes required for early exit (default: 4).",
    )
    group.addoption(
        "--stochastic-floor",
        type=float,
        default=0.9,
        metavar="P",
        help="Minimum pass rate (0.0–1.0) to accept a test (default: 0.9).",
    )


# ── Per-session accumulator ─────────────────────────────────────────────


_stochastic_results: list[dict] = []


# ── Protocol override ───────────────────────────────────────────────────


def _is_stochastic_target(item: pytest.Item) -> bool:
    if not item.config.getoption("stochastic"):
        return False
    return any(m.name == "llm_live" for m in item.iter_markers())


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    if not _is_stochastic_target(item):
        return None  # normal execution

    max_runs = item.config.getoption("stochastic_runs")
    required_consecutive = item.config.getoption("stochastic_consecutive")
    pass_floor = item.config.getoption("stochastic_floor")

    run_outcomes: list[bool] = []
    consecutive = 0
    max_consecutive = 0
    total_cost = 0.0
    total_prompt = 0
    total_completion = 0
    last_reports: list = []

    for _ in range(max_runs):
        reports = runtestprotocol(item, nextitem=nextitem, log=False)
        last_reports = reports

        call_passed = all(r.passed for r in reports if r.when == "call")
        run_outcomes.append(call_passed)

        for r in reports:
            if r.when == "call":
                for key, val in getattr(r, "user_properties", []):
                    if key == "cost" and isinstance(val, dict):
                        total_cost += val.get("cost_usd") or 0
                        total_prompt += val.get("prompt_tokens", 0)
                        total_completion += val.get("completion_tokens", 0)

        if call_passed:
            consecutive += 1
            max_consecutive = max(max_consecutive, consecutive)
        else:
            consecutive = 0

        if max_consecutive >= required_consecutive:
            break

    passed_count = sum(run_outcomes)
    total_runs = len(run_outcomes)
    pass_rate = passed_count / total_runs if total_runs else 0
    stochastic_ok = (
        max_consecutive >= required_consecutive or pass_rate >= pass_floor
    )

    stochastic_meta = {
        "runs": total_runs,
        "passed": passed_count,
        "pass_rate": round(pass_rate, 3),
        "max_consecutive_passes": max_consecutive,
        "stochastic_passed": stochastic_ok,
        "cost_usd": round(total_cost, 6),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
    }
    _stochastic_results.append({"nodeid": item.nodeid, **stochastic_meta})

    for report in last_reports:
        if report.when == "call":
            report.user_properties.append(("stochastic", stochastic_meta))
            if stochastic_ok and not report.passed:
                report.outcome = "passed"
                report.longrepr = None
            elif not stochastic_ok and report.passed:
                report.outcome = "failed"
                report.longrepr = (
                    f"Stochastic: {passed_count}/{total_runs} passed "
                    f"({pass_rate:.0%}), need {required_consecutive} consecutive "
                    f"or {pass_floor:.0%} floor"
                )
        item.ihook.pytest_runtest_logreport(report=report)

    return True  # we handled the protocol


# ── Terminal summary ────────────────────────────────────────────────────


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _stochastic_results:
        return

    tr = terminalreporter
    tr.section("Stochastic Results")

    total_cost = 0.0
    passed = 0
    failed = 0

    for entry in _stochastic_results:
        total_cost += entry["cost_usd"]
        status = "PASS" if entry["stochastic_passed"] else "FAIL"
        if entry["stochastic_passed"]:
            passed += 1
        else:
            failed += 1
        tr.write_line(
            f"  {status}  {entry['nodeid']}  "
            f"({entry['passed']}/{entry['runs']} = {entry['pass_rate']:.0%}, "
            f"consec={entry['max_consecutive_passes']}, "
            f"${entry['cost_usd']:.4f})"
        )

    tr.write_line("")
    tr.write_line(
        f"  Stochastic: {passed} passed, {failed} failed, "
        f"total cost ${total_cost:.4f}"
    )
