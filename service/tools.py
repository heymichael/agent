"""OpenAI tool definitions and execution handlers.

Analytics tools (vendor_lookup, vendor_count, spend_total, spend_by_vendor,
spend_by_dimension, top_vendors) delegate to the MCP server module which
owns the resolution pipeline, period parsing, and response contract.

Write tools (add_vendor, modify_vendor) and execute_python remain here.
delete_vendor is disabled — deletion must go through a system admin.
"""

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import date as date_type

from . import pg_client

logger = logging.getLogger(__name__)
from .sandbox import execute_python
from mcp_server.tools import (
    handle_vendor_lookup,
    handle_vendor_count,
    handle_vendor_list,
    handle_spend_total,
    handle_spend_by_vendor,
    handle_spend_by_dimension,
    handle_top_vendors,
    handle_spend_detail,
    handle_spend_detail_dimensions,
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
    "description": "Filters to narrow results. Multiple filters are AND-combined. Use '*' for 'has a value' (IS NOT NULL) and 'none' for 'no value' (IS NULL) on any field.",
    "properties": {
        "paymentMethod": {
            "type": "string",
            "enum": ["Check", "ACH", "CreditCard", "Wire", "PayPal"],
        },
        "accountType": {
            "type": "string",
            "enum": ["Business", "Individual"],
        },
        "track1099": {"type": "boolean"},
        "billingFrequency": {
            "type": "string",
            "enum": ["monthly", "annual", "usage-based"],
        },
        "sourceSystem": {
            "type": "string",
            "enum": ["billcom", "aws-ce", "gcp-billing", "manual"],
        },
        "department": {
            "type": "string",
            "description": "Department name (fuzzy matched). Use '*' for any, 'none' for none.",
        },
        "owner": {
            "type": "string",
            "description": "Person's name (fuzzy matched). Use '*' for any owner, 'none' for no owner.",
        },
        "secondaryOwner": {
            "type": "string",
            "description": "Person's name (fuzzy matched). Use '*' for any, 'none' for none.",
        },
        "purpose": {"type": "string"},
        "spendType": {"type": "string"},
        "autoRenew": {"type": "boolean"},
        "contractStart": {
            "description": "Date string or {from, to} range for contract start.",
            "oneOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                },
            ],
        },
        "contractEnd": {
            "description": "Date string or {from, to} range for contract end.",
            "oneOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                },
            ],
        },
        "contractMonths": {
            "description": "Integer or {min, max} range.",
            "oneOf": [
                {"type": "integer"},
                {
                    "type": "object",
                    "properties": {
                        "min": {"type": "integer"},
                        "max": {"type": "integer"},
                    },
                },
            ],
        },
        "renewalNotice": {
            "description": "Integer or {min, max} range for renewal notice days.",
            "oneOf": [
                {"type": "integer"},
                {
                    "type": "object",
                    "properties": {
                        "min": {"type": "integer"},
                        "max": {"type": "integer"},
                    },
                },
            ],
        },
        "renewalRate": {"type": "string"},
        "terminationTerms": {"type": "string"},
    },
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
                            "track1099, billingFrequency, sourceSystem, department, owner."
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
            "name": "vendor_list",
            "description": (
                "List vendors matching filter criteria. Returns vendor names "
                "and key fields (department, accountType, paymentMethod, etc.). "
                "Use when the user asks to see, list, or enumerate vendors "
                "matching conditions — e.g. 'list the 1099 vendors', "
                "'show me ACH vendors in marketing'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": _FILTERS_PARAM,
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max vendors to return. Defaults to 50. "
                            "Use a smaller number when the user asks for a sample."
                        ),
                    },
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
                    "metric": {
                        "type": "string",
                        "enum": ["spend", "vendorCount", "billCount"],
                        "description": "Which metric to return. Defaults to spend.",
                    },
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
                            "track1099, billingFrequency, sourceSystem, department, "
                            "owner, vendorName."
                        ),
                    },
                    "period": _PERIOD_PARAM,
                    "filters": _FILTERS_PARAM,
                    "metric": {
                        "type": "string",
                        "enum": ["spend", "vendorCount", "billCount"],
                        "description": "Which metric to return. Defaults to spend.",
                    },
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
                    "metric": {
                        "type": "string",
                        "enum": ["spend", "vendorCount", "billCount"],
                        "description": "Which metric to return. Defaults to spend.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spend_detail",
            "description": (
                "Query granular spend detail for a vendor, broken down by "
                "service/category, SKU/subcategory, and project. Use for drill-down "
                "questions like 'break down AWS spend by service' or 'what is our "
                "Cloud Run spend?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {
                        "type": "string",
                        "description": "Vendor name, ID, or alias.",
                    },
                    "period": _PERIOD_PARAM,
                    "group_by": {
                        "type": "string",
                        "enum": ["category", "subcategory", "project"],
                        "description": "Primary dimension for rows. Results are grouped by this dimension with months as columns.",
                    },
                    "secondary_group_by": {
                        "type": "string",
                        "enum": ["category", "subcategory", "project"],
                        "description": "Secondary dimension for columns. When set, produces a cross-tab: group_by values as rows, secondary_group_by values as columns, amounts summed across the period. Must differ from group_by.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter to a specific category (e.g. 'Amazon Elastic Compute Cloud').",
                    },
                    "project": {
                        "type": "string",
                        "description": "Filter to a specific project or linked account.",
                    },
                },
                "required": ["vendor", "period"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spend_detail_dimensions",
            "description": (
                "Discover what categories, subcategories, and projects are available "
                "for a vendor's spend detail. Use before spend_detail to learn what "
                "breakdowns exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {
                        "type": "string",
                        "description": "Vendor name, ID, or alias.",
                    },
                    "dimension": {
                        "type": "string",
                        "enum": ["category", "subcategory", "project"],
                        "description": "Return values for just this dimension. Omit for all.",
                    },
                },
                "required": ["vendor"],
            },
        },
    },
    # -- Write tools --
    {
        "type": "function",
        "function": {
            "name": "add_vendor",
            "description": "Add a new vendor to the database. This does NOT create a vendor in Bill.com.",
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
            "name": "modify_vendor",
            "description": (
                "Update vendor fields directly, or open the edit form if no "
                "fields are provided. When fields are provided, the update is "
                "applied immediately without opening the form."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "Vendor name or ID to edit",
                    },
                    "department": {
                        "type": "string",
                        "description": "Department name (fuzzy-matched against canonical values)",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Owner email (fuzzy-matched against canonical values)",
                    },
                    "secondary_owner": {
                        "type": "string",
                        "description": "Secondary owner email (fuzzy-matched against canonical values)",
                    },
                    "payment_method": {
                        "type": "string",
                        "description": "Payment method: Check, ACH, CreditCard, Wire, PayPal",
                    },
                    "billing_frequency": {
                        "type": "string",
                        "description": "Billing frequency: monthly, annual, usage-based",
                    },
                    "account_type": {
                        "type": "string",
                        "description": "Account type: Business, Individual",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "Vendor purpose/description",
                    },
                },
                "required": ["identifier"],
            },
        },
    },
    # -- CSV bulk edit tools --
    {
        "type": "function",
        "function": {
            "name": "generate_vendor_edit_csv",
            "description": (
                "Generate a downloadable CSV of vendors pre-filled with their "
                "current attributes, ready for the user to edit and upload back. "
                "Use when the user wants to make bulk changes. Offer to filter "
                "by owner, department, or provide all vendors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "departments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter to vendors in these departments.",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Filter to vendors owned by this person (name or email).",
                    },
                    "vendor_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific vendor names to include.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_vendor_csv",
            "description": (
                "Validate and process a CSV file that the user uploaded for "
                "bulk vendor updates. Reads the attached CSV automatically. "
                "Do not call this unless the user has attached a CSV file."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    # -- Table view control tools --
    {
        "type": "function",
        "function": {
            "name": "set_view_columns",
            "description": (
                "Change which columns are visible in a data table. "
                "Provide specific column keys, use column group names "
                "from the prompt, or set reset=true to restore defaults."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Table identifier (e.g. 'vendors').",
                    },
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Column keys to display.",
                    },
                    "reset": {
                        "type": "boolean",
                        "description": "If true, restore the default column set.",
                    },
                },
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_table_filters",
            "description": (
                "Apply row filters to a data table. Each filter targets "
                "a column with a set of allowed values. Only categorical "
                "and boolean columns support filtering. Set clear=true "
                "to remove all filters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Table identifier (e.g. 'vendors').",
                    },
                    "filters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {
                                    "type": "string",
                                    "description": "Column key to filter on.",
                                },
                                "values": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Allowed values for this column.",
                                },
                            },
                            "required": ["column", "values"],
                        },
                        "description": "Filters to apply.",
                    },
                    "combine": {
                        "type": "string",
                        "enum": ["and", "or"],
                        "description": (
                            "How to combine filters across columns. "
                            "Use 'and' when ALL conditions must match; "
                            "use 'or' when ANY condition should match."
                        ),
                    },
                    "clear": {
                        "type": "boolean",
                        "description": "If true, remove all active filters.",
                    },
                },
                "required": ["table"],
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
    Contractor vendors without an explicit grant are excluded.
    finance_admin users bypass filtering entirely.
    """
    if not caller_email:
        return {"allowed_vendor_ids": [], "is_finance_admin": False}

    user = pg_client.get_user_access_context(caller_email)
    if not user:
        return {"allowed_vendor_ids": [], "is_finance_admin": False}

    if "finance_admin" in user.get("roles", []):
        return {"is_finance_admin": True}

    effective_ids = pg_client.resolve_effective_vendor_ids(
        user.get("allowed_departments", []),
        user.get("allowed_vendor_ids", []),
        user.get("denied_vendor_ids", []),
        user_id=user.get("user_id"),
    )
    return {"allowed_vendor_ids": effective_ids, "is_finance_admin": False}


def _check_write_auth(caller_email: str, vendor_id: str, vendor_name: str | None = None) -> str | None:
    """Return a not_authorized JSON string if the caller cannot edit this vendor."""
    ctx = _build_caller_context(caller_email)
    if ctx.get("is_finance_admin"):
        return None
    allowed = ctx.get("allowed_vendor_ids") or []
    if vendor_id not in allowed:
        name = vendor_name or vendor_id
        return json.dumps({
            "status": "not_authorized",
            "message": f"You don't have permission to edit {name}.",
        })
    return None


def execute_vendor_lookup(args: dict, caller_email: str = "") -> str:
    return json.dumps(handle_vendor_lookup(args))


def execute_vendor_count(args: dict, caller_email: str = "") -> str:
    return json.dumps(handle_vendor_count(args))


def execute_vendor_list(args: dict, caller_email: str = "") -> str:
    return json.dumps(handle_vendor_list(args))


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


def execute_spend_detail(args: dict, caller_email: str = "") -> str:
    ctx = _build_caller_context(caller_email)
    return json.dumps(handle_spend_detail(args, caller_context=ctx))


def execute_spend_detail_dimensions(args: dict, caller_email: str = "") -> str:
    ctx = _build_caller_context(caller_email)
    return json.dumps(handle_spend_detail_dimensions(args, caller_context=ctx))


def execute_add_vendor(args: dict, caller_email: str = "") -> str:
    args.setdefault("status", "active")
    try:
        result = pg_client.add_vendor(args)
        return json.dumps({"ok": True, "vendor": result})
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def _match_response(result: pg_client.VendorMatch) -> dict:
    """Build common match metadata for tool responses."""
    resp: dict = {"match": result.match}
    if result.alternatives:
        resp["alternatives"] = [
            {"id": v["id"], "name": v["name"]} for v in result.alternatives
        ]
    return resp


def execute_delete_vendor(args: dict, caller_email: str = "") -> str:
    result = pg_client.resolve_vendor_by_identifier(args["identifier"])
    if not result:
        return json.dumps({"ok": False, "error": f"Vendor '{args['identifier']}' not found"})
    vendor = result.vendor
    if vendor.get("sourceSystem") != "manual":
        return json.dumps({
            "ok": False,
            "error": (
                f"'{vendor.get('name')}' is synced from {vendor.get('sourceSystem')} and can't be deleted — "
                "it would be re-created on the next nightly sync."
            ),
        })
    return json.dumps({
        "ok": True,
        **_match_response(result),
        "action": "confirm_delete",
        "vendor": {"id": vendor["id"], "name": vendor.get("name", vendor["id"])},
    })


def _resolve_field_value(field_name: str, value: str, candidates: list[str]) -> dict | None:
    """Resolve a field value against canonical candidates.

    Returns None on success (value is resolved in-place via the returned
    canonical string), or an error dict to return to the caller.
    """
    from service.resolve import resolve_canonical_value
    match = resolve_canonical_value(value, candidates)
    if not match:
        return {"ok": False, "error": f"Unknown {field_name}: '{value}'", "valid_values": sorted(candidates)}
    if match.match in ("exact", "close"):
        return None  # caller reads match.value
    return {
        "ok": False,
        "did_you_mean": match.value,
        "field": field_name,
        "provided": value,
        "alternatives": match.alternatives,
    }


def _user_candidates() -> list[tuple[str, str]]:
    """Build (candidate, user_id) pairs for both name and email."""
    pairs: list[tuple[str, str]] = []
    for u in pg_client.list_users():
        uid = u["id"]
        pairs.append((u["email"], uid))
        full = u.get("fullName", "")
        if full and full != u["email"]:
            pairs.append((full, uid))
    return pairs


_FK_FIELD_RESOLVERS = {
    "department": lambda: [(d["name"], d["id"]) for d in pg_client.list_departments()],
    "owner": _user_candidates,
    "secondary_owner": _user_candidates,
}

_ENUM_FIELD_VALUES = {
    "payment_method": ["Check", "ACH", "CreditCard", "Wire", "PayPal"],
    "billing_frequency": ["monthly", "annual", "usage-based"],
    "account_type": ["Business", "Individual"],
}

_DIRECT_FIELDS = {"purpose"}

_CONFIRM_FIELD_META = {
    "department": {
        "key": "departmentId", "label": "Department",
        "inputType": "select", "source": "departments",
        "currentDisplayKey": "department", "currentValueKey": "departmentId",
    },
    "owner": {
        "key": "ownerId", "label": "Owner",
        "inputType": "select", "source": "users",
        "currentDisplayKey": "owner", "currentValueKey": "ownerId",
    },
    "secondary_owner": {
        "key": "secondaryOwnerId", "label": "Secondary owner",
        "inputType": "select", "source": "users",
        "currentDisplayKey": "secondaryOwner", "currentValueKey": "secondaryOwnerId",
    },
    "payment_method": {
        "key": "paymentMethod", "label": "Payment method",
        "inputType": "select", "source": "enum",
        "currentDisplayKey": "paymentMethod",
    },
    "billing_frequency": {
        "key": "billingFrequency", "label": "Billing frequency",
        "inputType": "select", "source": "enum",
        "currentDisplayKey": "billingFrequency",
    },
    "account_type": {
        "key": "accountType", "label": "Account type",
        "inputType": "select", "source": "enum",
        "currentDisplayKey": "accountType",
    },
    "purpose": {
        "key": "purpose", "label": "Purpose",
        "inputType": "text",
        "currentDisplayKey": "purpose",
    },
}


def execute_modify_vendor(args: dict, caller_email: str = "") -> str:
    result = pg_client.resolve_vendor_by_identifier(args["identifier"])
    if not result:
        return json.dumps({"ok": False, "error": f"Vendor '{args['identifier']}' not found"})

    if result.match == "disambiguate":
        candidates = [{"id": result.vendor["id"], "name": result.vendor.get("name", result.vendor["id"])}]
        candidates += [{"id": v["id"], "name": v["name"]} for v in result.alternatives]
        return json.dumps({
            "ok": False, "status": "ambiguous",
            "match": "disambiguate",
            "candidates": candidates,
            "message": f"Multiple vendors match '{args['identifier']}'. Ask the user to clarify.",
        })

    vendor = result.vendor

    denied = _check_write_auth(caller_email, vendor["id"], vendor.get("name"))
    if denied:
        return denied

    field_args = {k: v for k, v in args.items() if k != "identifier" and v is not None}
    if not field_args:
        return json.dumps({
            "ok": True,
            **_match_response(result),
            "action": "open_edit",
            "vendor": {"id": vendor["id"], "name": vendor.get("name", vendor["id"])},
        })

    from service.resolve import resolve_canonical_value
    proposed_updates: dict = {}
    display_fields: list[dict] = []

    for field, value in field_args.items():
        meta = _CONFIRM_FIELD_META.get(field)
        if not meta:
            continue

        if field in _FK_FIELD_RESOLVERS:
            pairs = _FK_FIELD_RESOLVERS[field]()
            candidates = [name for name, _ in pairs]
            match = resolve_canonical_value(value, candidates)
            if match:
                id_map = {name: uid for name, uid in pairs}
                new_id = id_map[match.value]
                proposed_updates[meta["key"]] = new_id
                display_fields.append({
                    "key": meta["key"], "label": meta["label"],
                    "currentValue": vendor.get(meta["currentValueKey"]),
                    "currentDisplay": vendor.get(meta["currentDisplayKey"]) or "\u2014",
                    "newValue": new_id, "newDisplay": match.value,
                    "inputType": meta["inputType"], "source": meta["source"],
                })
            else:
                display_fields.append({
                    "key": meta["key"], "label": meta["label"],
                    "currentValue": vendor.get(meta["currentValueKey"]),
                    "currentDisplay": vendor.get(meta["currentDisplayKey"]) or "\u2014",
                    "newValue": "", "newDisplay": "",
                    "inputType": meta["inputType"], "source": meta["source"],
                    "unresolved": True,
                })

        elif field in _ENUM_FIELD_VALUES:
            candidates = _ENUM_FIELD_VALUES[field]
            match = resolve_canonical_value(value, candidates)
            if match:
                proposed_updates[meta["key"]] = match.value
                display_fields.append({
                    "key": meta["key"], "label": meta["label"],
                    "currentValue": vendor.get(meta["currentDisplayKey"]),
                    "currentDisplay": vendor.get(meta["currentDisplayKey"]) or "\u2014",
                    "newValue": match.value, "newDisplay": match.value,
                    "inputType": meta["inputType"], "source": meta["source"],
                    "options": candidates,
                })
            else:
                display_fields.append({
                    "key": meta["key"], "label": meta["label"],
                    "currentValue": vendor.get(meta["currentDisplayKey"]),
                    "currentDisplay": vendor.get(meta["currentDisplayKey"]) or "\u2014",
                    "newValue": "", "newDisplay": "",
                    "inputType": meta["inputType"], "source": meta["source"],
                    "options": candidates,
                    "unresolved": True,
                })

        elif field in _DIRECT_FIELDS:
            proposed_updates[meta["key"]] = value
            display_fields.append({
                "key": meta["key"], "label": meta["label"],
                "currentValue": vendor.get(meta["currentDisplayKey"]),
                "currentDisplay": vendor.get(meta["currentDisplayKey"]) or "\u2014",
                "newValue": value, "newDisplay": value,
                "inputType": meta["inputType"],
            })

    return json.dumps({
        "ok": True,
        **_match_response(result),
        "action": "confirm_edit",
        "vendor": {"id": vendor["id"], "name": vendor.get("name", vendor["id"])},
        "proposed_updates": proposed_updates,
        "display_fields": display_fields,
    })


def execute_execute_python(args: dict, caller_email: str = "") -> str:
    result = execute_python(args["code"])
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Generic CSV bulk edit infrastructure
# ---------------------------------------------------------------------------


class CsvColumnSpec:
    """Describes one column in a CSV profile."""
    __slots__ = ("csv_name", "db_name", "col_type", "resolver", "valid_values", "readonly")

    def __init__(
        self,
        csv_name: str,
        db_name: str,
        col_type: str,
        resolver=None,
        valid_values: list[str] | None = None,
        readonly: bool = False,
    ):
        self.csv_name = csv_name
        self.db_name = db_name
        self.col_type = col_type
        self.resolver = resolver
        self.valid_values = valid_values
        self.readonly = readonly


class TableCsvProfile:
    """Table-level config that drives CSV generation, validation, and resolution."""
    __slots__ = ("table", "pk_csv", "pk_key", "columns", "id_check_fn", "_by_name")

    def __init__(self, table: str, columns: list[CsvColumnSpec],
                 id_check_fn, pk_csv: str = "id", pk_key: str = "vendor_id"):
        self.table = table
        self.pk_csv = pk_csv
        self.pk_key = pk_key
        self.columns = columns
        self.id_check_fn = id_check_fn
        self._by_name = {c.csv_name: c for c in columns}

    @property
    def csv_headers(self) -> list[str]:
        return [self.pk_csv] + [c.csv_name for c in self.columns]

    def get_spec(self, csv_name: str) -> CsvColumnSpec | None:
        return self._by_name.get(csv_name)


VENDOR_CSV_PROFILE = TableCsvProfile(
    table="vendors",
    columns=[
        CsvColumnSpec("name", "name", "text", readonly=True),
        CsvColumnSpec("department", "department_id", "fk",
                      resolver=lambda: [(d["name"], d["id"]) for d in pg_client.list_departments()]),
        CsvColumnSpec("owner", "owner_id", "fk", resolver=_user_candidates),
        CsvColumnSpec("secondaryOwner", "secondary_owner_id", "fk", resolver=_user_candidates),
        CsvColumnSpec("billingFrequency", "billing_frequency", "enum",
                      valid_values=["monthly", "annual", "usage-based"]),
        CsvColumnSpec("purpose", "purpose", "text"),
        CsvColumnSpec("spendType", "spend_type", "text"),
        CsvColumnSpec("contractStartDate", "contract_start", "date"),
        CsvColumnSpec("contractEndDate", "contract_end", "date"),
        CsvColumnSpec("autoRenew", "auto_renew", "bool"),
    ],
    id_check_fn=pg_client.vendor_ids_exist,
)


def generate_edit_csv(profile: TableCsvProfile, records: list[dict]) -> str:
    """Generate a CSV string from records using the profile's column headers."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=profile.csv_headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    return buf.getvalue()


def parse_csv(content: str) -> tuple[list[str], list[dict]]:
    """Parse CSV content. Returns (headers, rows)."""
    content = content.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(content))
    headers = reader.fieldnames or []
    return list(headers), list(reader)


def validate_csv_columns(profile: TableCsvProfile, headers: list[str]) -> list[dict]:
    """Check that all CSV columns are recognised and the PK is present."""
    valid = set(profile.csv_headers)
    errors = [{"column": c, "message": f"Unknown column '{c}'"} for c in headers if c not in valid]
    if profile.pk_csv not in headers:
        errors.append({"column": profile.pk_csv,
                       "message": f"Primary key column '{profile.pk_csv}' is required"})
    return errors


def validate_csv_ids(profile: TableCsvProfile, rows: list[dict]) -> list[dict]:
    """Check that all PK values in the CSV exist in the table."""
    import re
    uuid_re = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I,
    )
    pk = profile.pk_csv
    errors: list[dict] = []
    valid_ids: list[str] = []
    for i, r in enumerate(rows):
        val = r.get(pk, "").strip()
        if not val:
            errors.append({"row": i + 2, "column": pk, "value": "",
                           "message": "Missing ID"})
        elif not uuid_re.match(val):
            errors.append({"row": i + 2, "column": pk, "value": val,
                           "message": f"'{val}' is not a valid UUID"})
        else:
            valid_ids.append(val)
    if errors:
        return errors
    existence = profile.id_check_fn(valid_ids)
    errors.extend(
        {"row": i + 2, "column": pk, "value": r.get(pk, ""),
         "message": f"ID '{r.get(pk, '')}' not found in {profile.table}"}
        for i, r in enumerate(rows)
        if not existence.get(r.get(pk, ""), False)
    )
    return errors


def validate_csv_values(
    profile: TableCsvProfile, rows: list[dict], headers: list[str],
) -> list[dict]:
    """Validate per-cell values against column types defined in the profile."""
    from service.resolve import resolve_canonical_value

    errors: list[dict] = []
    editable = set(headers) - {profile.pk_csv}
    editable = {c for c in editable if not (profile.get_spec(c) or CsvColumnSpec("", "", "text")).readonly}
    resolver_cache: dict = {}

    for i, row in enumerate(rows):
        row_num = i + 2
        for col in editable:
            val = row.get(col, "").strip()
            if not val:
                continue
            spec = profile.get_spec(col)
            if not spec:
                continue

            if spec.col_type == "fk":
                if spec.resolver not in resolver_cache:
                    resolver_cache[spec.resolver] = spec.resolver()
                pairs = resolver_cache[spec.resolver]
                if not resolve_canonical_value(val, [n for n, _ in pairs]):
                    errors.append({"row": row_num, "column": col, "value": val,
                                   "message": f"Could not resolve '{val}' for {col}"})

            elif spec.col_type == "enum":
                if not resolve_canonical_value(val, spec.valid_values):
                    errors.append({"row": row_num, "column": col, "value": val,
                                   "message": f"Invalid {col}: '{val}'. Valid: {', '.join(spec.valid_values)}"})

            elif spec.col_type == "bool":
                if val.lower() not in ("true", "false", "yes", "no", "1", "0"):
                    errors.append({"row": row_num, "column": col, "value": val,
                                   "message": f"Expected true/false for {col}, got '{val}'"})

            elif spec.col_type == "date":
                try:
                    date_type.fromisoformat(val)
                except ValueError:
                    errors.append({"row": row_num, "column": col, "value": val,
                                   "message": f"Invalid date for {col}: '{val}'. Expected YYYY-MM-DD."})

    return errors


def resolve_csv_updates(
    profile: TableCsvProfile, rows: list[dict], headers: list[str],
) -> list[dict]:
    """Build update dicts with resolved FK IDs and snake_case keys."""
    from service.resolve import resolve_canonical_value

    editable = set(headers) - {profile.pk_csv}
    editable = {c for c in editable if not (profile.get_spec(c) or CsvColumnSpec("", "", "text")).readonly}
    resolver_cache: dict = {}
    updates: list[dict] = []

    for row in rows:
        changes: dict = {}
        new_display: dict = {}
        for col in editable:
            val = row.get(col, "").strip()
            if not val:
                continue
            spec = profile.get_spec(col)
            if not spec:
                continue

            if spec.col_type == "fk":
                if spec.resolver not in resolver_cache:
                    resolver_cache[spec.resolver] = spec.resolver()
                pairs = resolver_cache[spec.resolver]
                match = resolve_canonical_value(val, [n for n, _ in pairs])
                if match:
                    id_map = {n: uid for n, uid in pairs}
                    changes[spec.db_name] = id_map[match.value]
                    new_display[spec.csv_name] = match.value

            elif spec.col_type == "enum":
                match = resolve_canonical_value(val, spec.valid_values)
                if match:
                    changes[spec.db_name] = match.value
                    new_display[spec.csv_name] = match.value

            elif spec.col_type == "bool":
                resolved = val.lower() in ("true", "yes", "1")
                changes[spec.db_name] = resolved
                new_display[spec.csv_name] = "Yes" if resolved else "No"

            elif spec.col_type in ("date", "text"):
                changes[spec.db_name] = val
                new_display[spec.csv_name] = val

        if changes:
            updates.append({
                profile.pk_key: row[profile.pk_csv],
                "changes": changes,
                "new_display": new_display,
            })

    return updates


def process_csv_upload(profile: TableCsvProfile, content: str) -> dict:
    """Run the full validation pipeline on CSV content. Returns a result dict."""
    headers, rows = parse_csv(content)

    if not rows:
        return {"ok": False, "error": "The CSV file is empty."}

    col_errors = validate_csv_columns(profile, headers)
    if col_errors:
        return {"ok": False, "stage": "column_check", "errors": col_errors}

    id_errors = validate_csv_ids(profile, rows)
    if id_errors:
        return {"ok": False, "stage": "id_check", "errors": id_errors}

    value_errors = validate_csv_values(profile, rows, headers)
    if value_errors:
        return {"ok": False, "stage": "value_check", "errors": value_errors}

    resolved = resolve_csv_updates(profile, rows, headers)
    if not resolved:
        return {"ok": True, "message": "No changes detected in the CSV."}

    all_vendors = pg_client.list_vendors()
    vendor_map = {v["id"]: v for v in all_vendors}

    field_counts: dict[str, int] = {}
    filtered: list[dict] = []
    for u in resolved:
        v = vendor_map.get(u[profile.pk_key])
        u["vendor_name"] = v["name"] if v else "Unknown"

        display_changes = []
        new_disp = u.pop("new_display", {})
        unchanged_keys = []
        for db_col in list(u["changes"]):
            spec = next((s for s in profile.columns if s.db_name == db_col), None)
            if not spec:
                continue
            new_val = u["changes"][db_col]

            if spec.col_type == "fk":
                fk_key = spec.csv_name + "Id"
                current_db = v.get(fk_key, "") if v else ""
                if str(current_db) == str(new_val):
                    unchanged_keys.append(db_col)
                    continue
            elif spec.col_type == "bool":
                current_raw = v.get(spec.csv_name) if v else None
                if current_raw == new_val:
                    unchanged_keys.append(db_col)
                    continue
            else:
                current_raw = v.get(spec.csv_name, "") if v else ""
                current_cmp = str(current_raw) if current_raw else ""
                if current_cmp == str(new_val):
                    unchanged_keys.append(db_col)
                    continue

            if spec.col_type == "fk":
                current_display = str(v.get(spec.csv_name, "")) if v else "—"
                current_display = current_display or "—"
            elif spec.col_type == "bool":
                current_display = "Yes" if (v and v.get(spec.csv_name)) else "No"
            else:
                current_raw = v.get(spec.csv_name, "") if v else ""
                current_display = str(current_raw) if current_raw else "—"
            display_changes.append({
                "label": spec.csv_name,
                "from": current_display,
                "to": new_disp.get(spec.csv_name, str(new_val)),
            })

        for k in unchanged_keys:
            del u["changes"][k]

        if not u["changes"]:
            continue

        u["display_changes"] = display_changes
        filtered.append(u)

        for field in u["changes"]:
            field_counts[field] = field_counts.get(field, 0) + 1

    resolved = filtered
    if not resolved:
        return {"ok": True, "message": "No changes detected in the CSV."}

    return {
        "ok": True,
        "action": "confirm_csv_batch",
        "updates": resolved,
        "summary": {
            "vendor_count": len(resolved),
            "field_counts": field_counts,
        },
    }


# ---------------------------------------------------------------------------
# Vendor CSV tool handlers (thin wrappers around generic infrastructure)
# ---------------------------------------------------------------------------


def execute_generate_vendor_edit_csv(args: dict, caller_email: str = "") -> str:
    from service.resolve import resolve_canonical_value

    vendors = pg_client.list_vendors()
    profile = VENDOR_CSV_PROFILE

    dept_filters = args.get("departments") or []
    if isinstance(dept_filters, str):
        dept_filters = [dept_filters]
    if dept_filters:
        dept_pairs = [(d["name"], d["id"]) for d in pg_client.list_departments()]
        dept_names = [name for name, _ in dept_pairs]
        matched_depts: set[str] = set()
        for df in dept_filters:
            match = resolve_canonical_value(df, dept_names)
            if match:
                matched_depts.add(match.value)
            else:
                return json.dumps({
                    "ok": False,
                    "error": f"Unknown department: '{df}'",
                    "valid_values": sorted(dept_names),
                })
        vendors = [v for v in vendors if v.get("department") in matched_depts]

    owner_filter = args.get("owner")
    if owner_filter:
        if owner_filter == "*":
            vendors = [v for v in vendors if v.get("ownerId")]
        elif owner_filter == "none":
            vendors = [v for v in vendors if not v.get("ownerId")]
        else:
            user_pairs = _user_candidates()
            user_names = [name for name, _ in user_pairs]
            match = resolve_canonical_value(owner_filter, user_names)
            if match:
                matched_uid = next(uid for name, uid in user_pairs if name == match.value)
                vendors = [v for v in vendors if v.get("ownerId") == matched_uid]
            else:
                return json.dumps({"ok": False, "error": f"Unknown owner: '{owner_filter}'"})

    vendor_names = args.get("vendor_names")
    unmatched: list[str] = []
    if vendor_names:
        name_set = {n.lower() for n in vendor_names}
        vendors = [v for v in vendors if v.get("name", "").lower() in name_set]
        matched = {v.get("name", "").lower() for v in vendors}
        unmatched = [n for n in vendor_names if n.lower() not in matched]

    if not vendors:
        return json.dumps({"ok": False, "error": "No vendors match the given filters."})

    csv_content = generate_edit_csv(profile, vendors)
    parts = ["vendors-edit"]
    for df in dept_filters:
        parts.append(df.lower().replace(" ", "-"))
    if owner_filter:
        parts.append(owner_filter.lower().replace(" ", "-").split("@")[0])
    filename = "-".join(parts) + ".csv"

    result = {
        "ok": True,
        "csv": csv_content,
        "csv_filename": filename,
        "row_count": len(vendors),
        "columns": profile.csv_headers,
    }
    if unmatched:
        result["unmatched_vendors"] = unmatched
    return json.dumps(result)


def execute_process_vendor_csv(args: dict, caller_email: str = "") -> str:
    from service.app import get_request_attachments

    attachments = get_request_attachments()
    csv_attachments = [a for a in attachments if a.get("filename", "").endswith(".csv")]
    if not csv_attachments:
        return json.dumps({"ok": False, "error": "No CSV file attached. Please attach a CSV file and try again."})

    result = process_csv_upload(VENDOR_CSV_PROFILE, csv_attachments[0]["content"])

    if result.get("ok") and result.get("updates"):
        ctx = _build_caller_context(caller_email)
        if not ctx.get("is_finance_admin"):
            allowed = set(ctx.get("allowed_vendor_ids") or [])
            denied_vendors = [
                u.get("vendor_name", u.get(VENDOR_CSV_PROFILE.pk_key, "Unknown"))
                for u in result["updates"]
                if u.get(VENDOR_CSV_PROFILE.pk_key) not in allowed
            ]
            if denied_vendors:
                return json.dumps({
                    "status": "not_authorized",
                    "message": f"You don't have permission to edit {len(denied_vendors)} vendor(s): {', '.join(denied_vendors)}.",
                    "denied_vendors": denied_vendors,
                })

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Table view config — schema-driven column metadata
# ---------------------------------------------------------------------------

_PG_TYPE_MAP: dict[str, str] = {
    "text": "categorical",
    "character varying": "categorical",
    "boolean": "boolean",
    "date": "date",
    "timestamp without time zone": "date",
    "timestamp with time zone": "date",
    "integer": "numeric",
    "bigint": "numeric",
    "smallint": "numeric",
    "numeric": "numeric",
    "double precision": "numeric",
    "real": "numeric",
    "ARRAY": "text",
    "jsonb": "text",
    "json": "text",
    "uuid": "text",
}


@dataclass(frozen=True)
class ColumnConfig:
    label: str
    col_type: str  # categorical | boolean | date | numeric | text
    db_name: str


@dataclass
class TableConfig:
    columns: dict[str, ColumnConfig]
    default_columns: list[str]
    column_groups: dict[str, list[str]]
    pinned: str

    @property
    def valid_columns(self) -> set[str]:
        return set(self.columns.keys())

    @property
    def filterable_columns(self) -> set[str]:
        return {k for k, v in self.columns.items()
                if v.col_type in ("categorical", "boolean")
                and k != self.pinned}

    @classmethod
    def from_table(
        cls,
        *,
        db_table: str,
        camel_map: dict[str, str],
        default_columns: list[str],
        column_groups: dict[str, list[str]],
        pinned: str,
    ) -> "TableConfig":
        """Build config by introspecting Postgres metadata.

        ``db_table`` can be a table or a view.
        ``camel_map`` maps snake_case DB column names to camelCase API keys.
        Columns without a ``COMMENT ON COLUMN`` are excluded as internal.
        """
        pool = pg_client.get_pool()
        with pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.column_name, c.data_type,
                       pgd.description AS comment
                FROM   information_schema.columns c
                LEFT JOIN pg_catalog.pg_description pgd
                       ON pgd.objoid = %s::regclass
                      AND pgd.objsubid = c.ordinal_position
                WHERE  c.table_schema = 'public'
                  AND  c.table_name   = %s
                ORDER BY c.ordinal_position
                """,
                (db_table, db_table),
            ).fetchall()

        columns: dict[str, ColumnConfig] = {}
        for row in rows:
            comment = row.get("comment")
            if not comment:
                continue
            db_name = row["column_name"]
            api_key = camel_map.get(db_name, db_name)
            col_type = _PG_TYPE_MAP.get(row["data_type"], "text")
            columns[api_key] = ColumnConfig(
                label=comment, col_type=col_type, db_name=db_name,
            )

        logger.info(
            "TableConfig[%s]: %d columns, %d filterable",
            db_table, len(columns),
            sum(1 for v in columns.values()
                if v.col_type in ("categorical", "boolean")),
        )
        return cls(
            columns=columns,
            default_columns=default_columns,
            column_groups=column_groups,
            pinned=pinned,
        )


TABLE_CONFIGS: dict[str, TableConfig] = {}


# ---------------------------------------------------------------------------
# Table view tool handlers
# ---------------------------------------------------------------------------


def execute_set_view_columns(args: dict, caller_email: str = "") -> str:
    table_id = args.get("table", "")
    config = TABLE_CONFIGS.get(table_id)
    if not config:
        return json.dumps({
            "ok": False,
            "error": f"Unknown table: '{table_id}'",
            "valid_tables": sorted(TABLE_CONFIGS.keys()),
        })

    if args.get("reset"):
        return json.dumps({
            "action": "set_columns",
            "table": table_id,
            "view_columns": config.default_columns,
        })

    columns = args.get("columns", [])
    if not columns:
        return json.dumps({
            "ok": False,
            "error": "No columns specified. Provide a list of column keys or set reset=true.",
        })

    invalid = [c for c in columns if c not in config.valid_columns]
    if invalid:
        return json.dumps({
            "ok": False,
            "error": f"Invalid column keys: {invalid}",
            "valid_columns": sorted(config.valid_columns),
        })

    seen: set[str] = set()
    deduped = []
    for c in columns:
        if c not in seen:
            seen.add(c)
            deduped.append(c)

    return json.dumps({
        "action": "set_columns",
        "table": table_id,
        "view_columns": deduped,
    })


def execute_set_table_filters(args: dict, caller_email: str = "") -> str:
    table_id = args.get("table", "")
    config = TABLE_CONFIGS.get(table_id)
    if not config:
        return json.dumps({
            "ok": False,
            "error": f"Unknown table: '{table_id}'",
            "valid_tables": sorted(TABLE_CONFIGS.keys()),
        })

    if args.get("clear"):
        return json.dumps({
            "action": "set_filters",
            "table": table_id,
            "table_filters": [],
        })

    filters = args.get("filters", [])
    if not filters:
        return json.dumps({
            "ok": False,
            "error": "No filters specified. Provide a list of filter objects or set clear=true.",
        })

    if args.get("combine") == "or":
        return json.dumps({
            "ok": False,
            "error": (
                "Table filters only support AND (all conditions must match). "
                "OR filtering across columns is not available. "
                "Suggest the user try a data query (e.g. vendor_list) instead."
            ),
        })

    _WILDCARD_CHARS = set("*?%")

    for f in filters:
        col = f.get("column", "")
        if col not in config.valid_columns:
            return json.dumps({
                "ok": False,
                "error": f"Unknown column: '{col}'",
                "valid_columns": sorted(config.valid_columns),
            })
        if col not in config.filterable_columns:
            col_cfg = config.columns[col]
            return json.dumps({
                "ok": False,
                "error": (
                    f"Column '{col}' ({col_cfg.label}) is type "
                    f"'{col_cfg.col_type}' and does not support set-based "
                    f"filtering. Suggest the user use the search bar "
                    f"above the table for text searches."
                ),
            })
        for val in f.get("values", []):
            if val in ("*", "none"):
                continue
            if _WILDCARD_CHARS & set(val):
                return json.dumps({
                    "ok": False,
                    "error": (
                        f"Filter values must be exact matches. "
                        f"'{val}' looks like a pattern (prefix, wildcard, "
                        f"or substring). Table filters do not support "
                        f"pattern matching. Suggest the user use the "
                        f"search bar above the table instead."
                    ),
                })

    return json.dumps({
        "action": "set_filters",
        "table": table_id,
        "table_filters": filters,
    })


TOOL_HANDLERS = {
    "vendor_lookup": execute_vendor_lookup,
    "vendor_count": execute_vendor_count,
    "vendor_list": execute_vendor_list,
    "spend_total": execute_spend_total,
    "spend_by_vendor": execute_spend_by_vendor,
    "spend_by_dimension": execute_spend_by_dimension,
    "top_vendors": execute_top_vendors,
    "spend_detail": execute_spend_detail,
    "spend_detail_dimensions": execute_spend_detail_dimensions,
    "add_vendor": execute_add_vendor,
    "modify_vendor": execute_modify_vendor,
    "generate_vendor_edit_csv": execute_generate_vendor_edit_csv,
    "process_vendor_csv": execute_process_vendor_csv,
    "set_view_columns": execute_set_view_columns,
    "set_table_filters": execute_set_table_filters,
}


# ---------------------------------------------------------------------------
# Domain-specific tool subsets
# ---------------------------------------------------------------------------

_EXPENSE_TOOL_NAMES = {
    "spend_total", "spend_by_vendor", "spend_by_dimension",
    "top_vendors", "spend_detail", "spend_detail_dimensions",
}

_VENDOR_TOOL_NAMES = {
    "vendor_lookup", "vendor_count", "vendor_list",
    "add_vendor", "modify_vendor",
    "generate_vendor_edit_csv", "process_vendor_csv",
    "set_view_columns", "set_table_filters",
}

ASK_EXPENSE_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_expense_agent",
        "description": (
            "Delegate a spend or cost question to the expense analytics "
            "agent. Use when the user asks about spend, costs, expenses, "
            "or analytics."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The user's spend-related question, passed verbatim.",
                },
            },
            "required": ["question"],
        },
    },
}

EXPENSE_TOOL_DEFINITIONS = [
    t for t in TOOL_DEFINITIONS if t["function"]["name"] in _EXPENSE_TOOL_NAMES
]

VENDOR_TOOL_DEFINITIONS = [
    t for t in TOOL_DEFINITIONS if t["function"]["name"] in _VENDOR_TOOL_NAMES
] + [ASK_EXPENSE_AGENT_TOOL]

EXPENSE_TOOL_HANDLERS = {
    k: v for k, v in TOOL_HANDLERS.items() if k in _EXPENSE_TOOL_NAMES
}

VENDOR_TOOL_HANDLERS = {
    k: v for k, v in TOOL_HANDLERS.items() if k in _VENDOR_TOOL_NAMES
}
