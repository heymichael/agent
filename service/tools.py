"""OpenAI tool definitions and execution handlers."""

import json

from . import firestore_client

# ---------------------------------------------------------------------------
# Tool schemas (registered with the OpenAI API)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "add_vendor",
            "description": "Add a new vendor to the system.",
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
            "description": "Request deletion of a vendor. This does not delete immediately — it returns a confirmation prompt that the user must approve in the UI.",
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
            "description": "Retrieve a vendor's full details by name or ID.",
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


TOOL_HANDLERS = {
    "add_vendor": execute_add_vendor,
    "delete_vendor": execute_delete_vendor,
    "get_vendor": execute_get_vendor,
}
