"""OpenAI tool definitions and execution handlers.

Analytics tools (vendor_lookup, vendor_count, spend_total, spend_by_vendor,
spend_by_dimension, top_vendors) delegate to the MCP server module which
owns the resolution pipeline, period parsing, and response contract.

Write tools (add_vendor, delete_vendor, modify_vendor, hide_vendor) and
execute_python remain here unchanged.
"""

import json

from . import firestore_client
from .sandbox import execute_python
from mcp_server.tools import (
    handle_vendor_lookup,
    handle_vendor_count,
    handle_spend_total,
    handle_spend_by_vendor,
    handle_spend_by_dimension,
    handle_top_vendors,
)

# ---------------------------------------------------------------------------
# Shared filter properties (reused across spend tools)
# ---------------------------------------------------------------------------

_PERIOD_PARAM = {
    "type": "string",
    "description": (
        "Time period. Formats: YYYY-MM (month), YYYY-QN (quarter), "
        "YYYY-HN (half), YYYY (year), YTD, last-N-months (e.g. "
        "last-3-months). Omit for all time."
    ),
}

_FILTERS_PARAM = {
    "type": "object",
    "description": (
        "Exact-match filters to narrow results. Multiple filters are AND-combined. "
        "Supported fields: paymentMethod (Check, ACH, CreditCard, Wire, PayPal), "
        "accountType (Business, Individual), track1099 (true/false), "
        "billingFrequency (monthly, annual, usage-based), "
        "toolCall (billcom, aws-ce, manual), department, owner."
    ),
}

# ---------------------------------------------------------------------------
# Tool schemas (registered with the OpenAI API)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    # -- Analytics tools (MCP-backed) --
    {
        "type": "function",
        "function": {
            "name": "vendor_lookup",
            "description": (
                "Look up a vendor by name, ID, or alias. Returns the full "
                "vendor profile including metadata and contract fields. "
                "Accepts partial names and abbreviations (e.g. 'AWS', 'Rhonda')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {
                        "type": "string",
                        "description": "Vendor name, ID, or alias to look up.",
                    },
                },
                "required": ["vendor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vendor_count",
            "description": (
                "Count vendors, optionally filtered and/or grouped by a "
                "dimension (e.g. 'how many 1099 vendors?', 'vendors by "
                "payment type')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_by": {
                        "type": "string",
                        "description": (
                            "Field to group counts by: paymentMethod, accountType, "
                            "track1099, billingFrequency, toolCall, department, owner."
                        ),
                    },
                    "filters": _FILTERS_PARAM,
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spend_total",
            "description": "Grand total spend for a time period, optionally filtered.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": _PERIOD_PARAM,
                    "filters": _FILTERS_PARAM,
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spend_by_vendor",
            "description": (
                "Spend for a single vendor (monthly breakdown) or all vendors "
                "(ranked by total). Specify vendor for per-vendor history; "
                "omit for a cross-vendor ranking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {
                        "type": "string",
                        "description": "Vendor name, ID, or alias. Omit for all vendors.",
                    },
                    "period": _PERIOD_PARAM,
                    "filters": _FILTERS_PARAM,
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spend_by_dimension",
            "description": (
                "Spend grouped by a single dimension (e.g. 'spend by payment "
                "type', 'spend by department'). Results sorted by amount "
                "descending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "description": (
                            "Field to group by: paymentMethod, accountType, "
                            "track1099, billingFrequency, toolCall, department, "
                            "owner, vendorName."
                        ),
                    },
                    "period": _PERIOD_PARAM,
                    "filters": _FILTERS_PARAM,
                },
                "required": ["dimension"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "top_vendors",
            "description": "Top N vendors by spend in a time period.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Number of vendors to return (default 10).",
                    },
                    "period": _PERIOD_PARAM,
                    "filters": _FILTERS_PARAM,
                },
            },
        },
    },
    # -- Write tools (unchanged) --
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
                "from analytics tool results by default. "
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
            "name": "execute_python",
            "description": (
                "Execute Python code to query external vendor APIs for TRANSACTIONAL data: "
                "Bill.com bills/spend/PII or AWS Cost Explorer cloud costs. "
                "Use vendor_lookup first to get the billcomId and toolCall, then use it here. "
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


def _build_caller_context(caller_email: str) -> dict | None:
    """Build caller context for spend-level access control.

    Resolves the caller's effective vendor set from their
    allowed_departments, allowed_vendor_ids, and denied_vendor_ids.
    finance_admin users bypass filtering entirely.
    """
    if not caller_email:
        return {"allowed_vendor_ids": [], "is_finance_admin": False}

    user = firestore_client.get_user_access_context(caller_email)
    if not user:
        return {"allowed_vendor_ids": [], "is_finance_admin": False}

    if "finance_admin" in user.get("roles", []):
        return {"is_finance_admin": True}

    effective_ids = firestore_client.resolve_effective_vendor_ids(
        user.get("allowed_departments", []),
        user.get("allowed_vendor_ids", []),
        user.get("denied_vendor_ids", []),
    )
    return {"allowed_vendor_ids": effective_ids, "is_finance_admin": False}


def execute_vendor_lookup(args: dict, caller_email: str = "") -> str:
    return json.dumps(handle_vendor_lookup(args))


def execute_vendor_count(args: dict, caller_email: str = "") -> str:
    return json.dumps(handle_vendor_count(args))


def execute_spend_total(args: dict, caller_email: str = "") -> str:
    ctx = _build_caller_context(caller_email)
    return json.dumps(handle_spend_total(args, caller_context=ctx))


def execute_spend_by_vendor(args: dict, caller_email: str = "") -> str:
    ctx = _build_caller_context(caller_email)
    return json.dumps(handle_spend_by_vendor(args, caller_context=ctx))


def execute_spend_by_dimension(args: dict, caller_email: str = "") -> str:
    ctx = _build_caller_context(caller_email)
    return json.dumps(handle_spend_by_dimension(args, caller_context=ctx))


def execute_top_vendors(args: dict, caller_email: str = "") -> str:
    ctx = _build_caller_context(caller_email)
    return json.dumps(handle_top_vendors(args, caller_context=ctx))


def execute_add_vendor(args: dict, caller_email: str = "") -> str:
    args.setdefault("status", "active")
    try:
        result = firestore_client.add_vendor(args)
        return json.dumps({"ok": True, "vendor": result})
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def execute_delete_vendor(args: dict, caller_email: str = "") -> str:
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


def execute_modify_vendor(args: dict, caller_email: str = "") -> str:
    vendor = firestore_client.resolve_vendor(args["identifier"])
    if not vendor:
        return json.dumps({"ok": False, "error": f"Vendor '{args['identifier']}' not found"})
    return json.dumps({
        "ok": True,
        "action": "open_edit",
        "vendor": {"id": vendor["id"], "name": vendor.get("name", vendor["id"])},
    })


def execute_hide_vendor(args: dict, caller_email: str = "") -> str:
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


def execute_execute_python(args: dict, caller_email: str = "") -> str:
    result = execute_python(args["code"])
    return json.dumps(result)


TOOL_HANDLERS = {
    "vendor_lookup": execute_vendor_lookup,
    "vendor_count": execute_vendor_count,
    "spend_total": execute_spend_total,
    "spend_by_vendor": execute_spend_by_vendor,
    "spend_by_dimension": execute_spend_by_dimension,
    "top_vendors": execute_top_vendors,
    "add_vendor": execute_add_vendor,
    "delete_vendor": execute_delete_vendor,
    "modify_vendor": execute_modify_vendor,
    "hide_vendor": execute_hide_vendor,
    "execute_python": execute_execute_python,
}
