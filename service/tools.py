"""OpenAI tool definitions and execution handlers."""

import json

from . import firestore_client
from .sandbox import execute_python

# ---------------------------------------------------------------------------
# Tool schemas (registered with the OpenAI API)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "add_vendor",
            "description": "Add a new vendor to the app's local Firestore registry. This does NOT create a vendor in Bill.com.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Vendor name (e.g. 'Datadog')",
                    },
                    "category": {
                        "type": "string",
                        "description": "Vendor category (e.g. 'Cloud Infrastructure', 'DevOps')",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "inactive", "pending"],
                        "description": "Vendor status (defaults to 'active')",
                    },
                    "billingCycle": {
                        "type": "string",
                        "enum": ["monthly", "annual", "usage-based"],
                    },
                    "paymentMethod": {
                        "type": "string",
                        "enum": ["credit_card", "invoice", "ach", "wire"],
                    },
                    "contractRenews": {
                        "type": "string",
                        "description": "ISO date when the contract renews",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Person responsible for this vendor",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_vendor",
            "description": "Request deletion of a vendor from the app's local Firestore registry (not Bill.com). Returns a confirmation prompt that the user must approve in the UI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "Vendor name or ID to delete",
                    },
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_vendors",
            "description": (
                "Search the vendor registry in Firestore. Use this as the FIRST step for any vendor question. "
                "Returns vendor metadata including billcomId (for follow-up Bill.com API queries via execute_python), "
                "payment method, 1099 status, owner, department, and contract fields. "
                "Use group_by to get aggregate counts (e.g. count vendors by payment method or 1099 status). "
                "Set include_spend to true to attach monthly spend summaries from Firestore "
                "(avoids a live Bill.com API call for historical spend questions)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Vendor name to search for (prefix match, case-insensitive). E.g. 'Michael' matches 'Michael D Mader'.",
                    },
                    "filters": {
                        "type": "object",
                        "description": "Exact-match filters on vendor fields. E.g. {\"track1099\": true}, {\"paymentMethod\": \"Check\"}, {\"toolCall\": \"billcom\"}.",
                    },
                    "group_by": {
                        "type": "string",
                        "description": "Field name to aggregate counts by. Returns {counts: {value: count}, total: N} instead of individual records. E.g. 'paymentMethod', 'track1099', 'department'.",
                    },
                    "include_spend": {
                        "type": "boolean",
                        "description": "If true, include recent monthly spend summaries (from vendor_spend collection) for each matched vendor. Each vendor result gets a 'spend' array with {month, totalAmount, billCount} entries.",
                    },
                    "spend_months": {
                        "type": "integer",
                        "description": "Number of months of spend history to include when include_spend is true. Defaults to 6.",
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "If true, include vendors that have been hidden from spend analysis. Defaults to false.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_vendor",
            "description": "Open the vendor edit modal in the UI for a vendor in the app's local Firestore registry. Does not update fields directly — opens the edit form.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "Vendor name or ID to edit",
                    },
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hide_vendor",
            "description": (
                "Hide or unhide a vendor from spend analysis. Hidden vendors are excluded "
                "from search_vendors results and query_spend aggregations by default. "
                "Use this instead of deleting Bill.com-synced vendors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "Vendor name or ID to hide/unhide",
                    },
                    "hide": {
                        "type": "boolean",
                        "description": "True to hide, false to unhide. Defaults to true.",
                    },
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_spend",
            "description": (
                "Query aggregated spend data from Firestore (vendor_spend collection). "
                "Use for cross-vendor spend questions: totals by month, spend grouped by "
                "payment method / department / billing frequency, or top vendors by spend. "
                "Data is synced nightly from Bill.com bills. For per-vendor spend, prefer "
                "search_vendors with include_spend instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "month": {
                        "type": "string",
                        "description": "Exact month to query (YYYY-MM). E.g. '2026-02'.",
                    },
                    "start_month": {
                        "type": "string",
                        "description": "Start of month range (inclusive, YYYY-MM). Use with end_month for multi-month queries.",
                    },
                    "end_month": {
                        "type": "string",
                        "description": "End of month range (inclusive, YYYY-MM). Use with start_month.",
                    },
                    "vendor_name": {
                        "type": "string",
                        "description": "Filter by vendor name (substring match, case-insensitive).",
                    },
                    "group_by": {
                        "type": "string",
                        "description": "Group and sum spend by this field. Results are sorted by totalAmount descending. Fields: 'paymentMethod', 'department', 'owner', 'billingFrequency', 'track1099', 'accountType', 'purpose', 'spendType', 'vendorName'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of results to return (default 50). For 'top N vendors' queries, set this to N. Applies to both grouped and ungrouped results.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": (
                "Execute Python code to query external vendor APIs for TRANSACTIONAL data: "
                "Bill.com bills/spend/PII or AWS Cost Explorer cloud costs. "
                "Always call search_vendors first to get the billcomId, then use it here for exact lookups. "
                "Pre-installed libraries: boto3, requests, json, os, datetime, math, re, collections, decimal. "
                "Vendor credentials are available as environment variables (never print them). "
                "Print the result as JSON to stdout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute. Must print results as JSON to stdout.",
                    },
                },
                "required": ["code"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool execution handlers
# ---------------------------------------------------------------------------


def execute_add_vendor(args: dict) -> str:
    args.setdefault("status", "active")
    try:
        result = firestore_client.add_vendor(args)
        return json.dumps({"ok": True, "vendor": result})
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def execute_delete_vendor(args: dict) -> str:
    vendor = firestore_client.resolve_vendor(args["identifier"])
    if not vendor:
        return json.dumps({"ok": False, "error": f"Vendor '{args['identifier']}' not found"})
    if vendor.get("billcomId"):
        return json.dumps({
            "ok": False,
            "error": (
                f"'{vendor.get('name')}' is synced from Bill.com and can't be deleted — "
                "it would be re-created on the next nightly sync. "
                "You can hide it from spend analysis instead using hide_vendor."
            ),
        })
    return json.dumps({
        "ok": True,
        "action": "confirm_delete",
        "vendor": {"id": vendor["id"], "name": vendor.get("name", vendor["id"])},
    })


def execute_search_vendors(args: dict) -> str:
    result = firestore_client.search_vendors(
        query=args.get("query"),
        filters=args.get("filters"),
        group_by=args.get("group_by"),
        include_spend=args.get("include_spend", False),
        spend_months=args.get("spend_months", 6),
        include_hidden=args.get("include_hidden", False),
    )
    return json.dumps({"ok": True, **result})


def execute_modify_vendor(args: dict) -> str:
    vendor = firestore_client.resolve_vendor(args["identifier"])
    if not vendor:
        return json.dumps({"ok": False, "error": f"Vendor '{args['identifier']}' not found"})
    return json.dumps({
        "ok": True,
        "action": "open_edit",
        "vendor": {"id": vendor["id"], "name": vendor.get("name", vendor["id"])},
    })


def execute_hide_vendor(args: dict) -> str:
    vendor = firestore_client.resolve_vendor(args["identifier"])
    if not vendor:
        return json.dumps({"ok": False, "error": f"Vendor '{args['identifier']}' not found"})
    hide = args.get("hide", True)
    vendor_id = vendor.get("billcomId") or vendor.get("id", "")
    try:
        updated = firestore_client.set_vendor_hidden(vendor_id, hide)
        action = "hidden" if hide else "unhidden"
        return json.dumps({
            "ok": True,
            "action": action,
            "vendor": {"id": vendor_id, "name": updated.get("name", vendor_id)},
        })
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def execute_query_spend(args: dict) -> str:
    kwargs: dict = {
        "month": args.get("month"),
        "start_month": args.get("start_month"),
        "end_month": args.get("end_month"),
        "vendor_name": args.get("vendor_name"),
        "group_by": args.get("group_by"),
    }
    if "limit" in args:
        kwargs["limit"] = args["limit"]
    result = firestore_client.query_spend(**kwargs)
    return json.dumps({"ok": True, **result})


def execute_execute_python(args: dict) -> str:
    result = execute_python(args["code"])
    return json.dumps(result)


TOOL_HANDLERS = {
    "add_vendor": execute_add_vendor,
    "delete_vendor": execute_delete_vendor,
    "search_vendors": execute_search_vendors,
    "modify_vendor": execute_modify_vendor,
    "hide_vendor": execute_hide_vendor,
    "query_spend": execute_query_spend,
    "execute_python": execute_execute_python,
}
