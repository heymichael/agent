"""MCP protocol entry point for the test-status server.

Run with:
    python -m mcp_test_server

Exposes query and execution tools for the stochastic LLM test framework
over stdio transport for consumption by Cursor or any MCP-compatible client.
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from .tools import (
    handle_list_scenarios,
    handle_test_summary,
    handle_failure_detail,
    handle_test_history,
    handle_run_scenarios,
    handle_publish_results,
)

mcp = FastMCP("haderach-test-status")


# ── Query tools ──────────────────────────────────────────────────────────


@mcp.tool()
async def list_e2e_tests(
    agent: str | None = None,
    domain: str | None = None,
    module: str | None = None,
    capability: str | None = None,
    tool: str | None = None,
) -> dict:
    """List BDD end-to-end test scenarios from .feature files, filtered by tag dimensions.

    Returns scenarios grouped by feature with their capability tags.
    All filter parameters are optional — omit to list everything.

    Args:
        agent: Filter by agent (e.g. "vendor_management", "expense_analytics").
        domain: Filter by domain (e.g. "vendors", "spend").
        module: Filter by module (e.g. "single_edit", "csv_download").
        capability: Filter by capability (e.g. "csv_bulk_edit", "spend_detail").
        tool: Filter by tool (e.g. "modify_vendor", "spend_by_vendor").
    """
    return handle_list_scenarios(agent, domain, module, capability, tool)


@mcp.tool()
async def test_summary(
    group_by: str = "tool",
    run: str | None = None,
    app: str = "agent",
) -> dict:
    """Grouped stochastic test summary from a test report.

    Groups tests by the chosen tag dimension and returns per-group rows
    with pass rates, cost, and duration, plus a TOTAL row.

    Uses the latest local report by default. Pass ``run`` to load a
    historical run from GCS first (overwrites .report.json).

    Args:
        group_by: Tag dimension to group by. One of: "tool", "domain",
                  "module", "capability", "agent" (default "tool").
        run: Historical run to fetch (filename, stem, or substring from
             test_history). Omit to use the latest local report.
        app: Application name for GCS lookup (default "agent").
    """
    if run:
        return await asyncio.to_thread(handle_test_summary, group_by, run, app)
    return handle_test_summary(group_by)


@mcp.tool()
async def failure_detail() -> dict:
    """Show detailed failure info for every failed test in the latest local report.

    Returns the test name, assertion message, failure count over total runs,
    pass rate, and cost for each failure.
    """
    return handle_failure_detail()


@mcp.tool()
async def test_history(
    app: str = "agent",
    limit: int = 10,
) -> dict:
    """List historical test runs published to GCS.

    Returns timestamps of previous test runs so you can track test frequency.

    Args:
        app: Application name (default "agent").
        limit: Number of recent runs to return (default 10).
    """
    return handle_test_history(app, limit)


# ── Execution tools ──────────────────────────────────────────────────────


@mcp.tool()
async def run_scenarios(
    agent: str | None = None,
    domain: str | None = None,
    module: str | None = None,
    capability: str | None = None,
    tool: str | None = None,
    stochastic: bool = False,
    runs: int | None = None,
    run_name: str | None = None,
) -> dict:
    """Run BDD test scenarios matching the given filters.

    Builds a pytest command with marker filters and executes it.
    Returns structured results with pass/fail, cost, and optional
    stochastic metadata.

    Args:
        agent: Filter by agent.
        domain: Filter by domain.
        module: Filter by module.
        capability: Filter by capability.
        tool: Filter by tool.
        stochastic: Enable stochastic mode (re-run @llm_live tests).
        runs: Number of stochastic runs (default 10).
        run_name: Human-readable label for this run (e.g. "vendor-baseline").
    """
    return await asyncio.to_thread(
        handle_run_scenarios,
        agent, domain, module, capability, tool, stochastic, runs, run_name,
    )


@mcp.tool()
async def publish_results(app: str = "agent") -> dict:
    """Publish the latest test report to GCS (timestamped + latest).

    Uploads .report.json to gs://haderach-app-artifacts/test-results/{app}/
    using the test-results-publisher SA.

    Args:
        app: Application name for the GCS path (default "agent").
    """
    return await asyncio.to_thread(handle_publish_results, app)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
