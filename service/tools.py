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
            "name": "get_vendor",
            "description": "Retrieve a vendor from the app's local Firestore registry by name or ID. This does NOT search Bill.com — use execute_python for Bill.com vendor lookups.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "Vendor name or ID to look up",
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
            "name": "execute_python",
            "description": (
                "Execute Python code to query external vendor APIs (Bill.com, AWS Cost Explorer). "
                "Use this for ANY question about Bill.com data (vendors, bills, spend, 1099 status, payment info) "
                "or AWS cloud costs. "
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
    return json.dumps({
        "ok": True,
        "action": "confirm_delete",
        "vendor": {"id": vendor["id"], "name": vendor.get("name", vendor["id"])},
    })


def execute_get_vendor(args: dict) -> str:
    vendor = firestore_client.resolve_vendor(args["identifier"])
    if not vendor:
        return json.dumps({"ok": False, "error": f"Vendor '{args['identifier']}' not found"})
    return json.dumps({"ok": True, "vendor": vendor})


def execute_modify_vendor(args: dict) -> str:
    vendor = firestore_client.resolve_vendor(args["identifier"])
    if not vendor:
        return json.dumps({"ok": False, "error": f"Vendor '{args['identifier']}' not found"})
    return json.dumps({
        "ok": True,
        "action": "open_edit",
        "vendor": {"id": vendor["id"], "name": vendor.get("name", vendor["id"])},
    })


def execute_execute_python(args: dict) -> str:
    result = execute_python(args["code"])
    return json.dumps(result)


TOOL_HANDLERS = {
    "add_vendor": execute_add_vendor,
    "delete_vendor": execute_delete_vendor,
    "get_vendor": execute_get_vendor,
    "modify_vendor": execute_modify_vendor,
    "execute_python": execute_execute_python,
}
