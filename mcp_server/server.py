"""MCP protocol entry point for the vendor analytics server.

Run with:
    python -m mcp_server.server

Exposes the six vendor analytics tools over stdio transport for consumption
by Cursor, Claude Desktop, or any MCP-compatible client.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import (
    handle_vendor_lookup,
    handle_vendor_count,
    handle_spend_total,
    handle_spend_by_vendor,
    handle_spend_by_dimension,
    handle_top_vendors,
)

mcp = FastMCP("haderach-vendor-analytics")


@mcp.tool()
async def vendor_lookup(vendor: str) -> dict:
    """Look up a vendor by name, ID, or alias.

    Returns the full vendor profile including metadata, contract fields,
    and payment information. Accepts partial names, abbreviations (e.g.
    "AWS"), or UUIDs.
    """
    return handle_vendor_lookup({"vendor": vendor})


@mcp.tool()
async def vendor_count(
    group_by: str | None = None,
    filters: dict | None = None,
) -> dict:
    """Count vendors, optionally grouped by a dimension.

    Args:
        group_by: Field to group counts by (e.g. "paymentMethod",
            "department", "track1099", "accountType").
        filters: Exact-match filters to narrow the count. Keys are field
            names, values must match exactly. Supported fields:
            paymentMethod (Check, ACH, CreditCard, Wire, PayPal),
            accountType (Business, Individual), track1099 (true/false),
            billingFrequency (monthly, annual, usage-based),
            sourceSystem (billcom, aws-ce, manual), department, owner.
    """
    return handle_vendor_count({"group_by": group_by, "filters": filters})


@mcp.tool()
async def spend_total(
    period: str | None = None,
    filters: dict | None = None,
) -> dict:
    """Grand total spend for a time period, optionally filtered.

    Args:
        period: Time period. Formats: YYYY-MM (month), YYYY-QN (quarter),
            YYYY-HN (half), YYYY (year), YTD, last-N-months (e.g.
            last-3-months). Omit for all time.
        filters: Exact-match filters to narrow results. Same fields as
            vendor_count.
    """
    return handle_spend_total({"period": period, "filters": filters})


@mcp.tool()
async def spend_by_vendor(
    vendor: str | None = None,
    period: str | None = None,
    filters: dict | None = None,
) -> dict:
    """Spend breakdown for a single vendor or all vendors.

    When vendor is specified, returns monthly spend history for that
    vendor. When omitted, returns spend totals per vendor sorted by
    amount descending.

    Args:
        vendor: Vendor name, ID, or alias. Omit for all vendors.
        period: Time period (same formats as spend_total).
        filters: Exact-match filters to narrow results.
    """
    return handle_spend_by_vendor({"vendor": vendor, "period": period, "filters": filters})


@mcp.tool()
async def spend_by_dimension(
    dimension: str,
    period: str | None = None,
    filters: dict | None = None,
) -> dict:
    """Spend grouped by a single dimension.

    Args:
        dimension: Field to group by. Options: paymentMethod, accountType,
            track1099, billingFrequency, sourceSystem, department, owner,
            vendorName.
        period: Time period (same formats as spend_total).
        filters: Exact-match filters to narrow results before grouping.
            Multiple filters are AND-combined.
    """
    return handle_spend_by_dimension({"dimension": dimension, "period": period, "filters": filters})


@mcp.tool()
async def top_vendors(
    n: int = 10,
    period: str | None = None,
    filters: dict | None = None,
) -> dict:
    """Top N vendors by spend in a time period.

    Args:
        n: Number of vendors to return (default 10).
        period: Time period (same formats as spend_total).
        filters: Exact-match filters to narrow results before ranking.
    """
    return handle_top_vendors({"n": n, "period": period, "filters": filters})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
