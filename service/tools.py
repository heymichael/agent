"""OpenAI tool definitions and execution handlers.

Analytics tools (vendor_lookup, vendor_count, spend_total, spend_by_vendor,
spend_by_dimension, top_vendors) delegate to the MCP server module which
owns the resolution pipeline, period parsing, and response contract.

Write tools (add_vendor, delete_vendor, modify_vendor) and
execute_python remain here.
"""

import json

from . import pg_client
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
        "sourceSystem (billcom, aws-ce, manual), department, owner."
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
                            "track1099, billingFrequency, sourceSystem, department, "
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
            "name": "delete_vendor",
            "description": "Request deletion of a vendor (not Bill.com). Returns a confirmation prompt that the user must approve in the UI.",
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
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": (
                "Execute Python code to query external vendor APIs for TRANSACTIONAL data: "
                "Bill.com bills/spend/PII or AWS Cost Explorer cloud costs. "
                "Use vendor_lookup first to get the sourceSystemId and sourceSystem, then use it here. "
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

    user = pg_client.get_user_access_context(caller_email)
    if not user:
        return {"allowed_vendor_ids": [], "is_finance_admin": False}

    if "finance_admin" in user.get("roles", []):
        return {"is_finance_admin": True}

    effective_ids = pg_client.resolve_effective_vendor_ids(
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
    "execute_python": execute_execute_python,
}
