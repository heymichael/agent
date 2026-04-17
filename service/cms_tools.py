"""CMS tool definitions and Payload REST API handlers.

All handlers call the Payload CMS REST API using CMS_API_URL and
CMS_API_KEY. Tools are grouped by agent mode:

- Editing: get, create, update, lock/unlock, submit_for_approval,
  restore_version, add_to_schedule
- Scheduling: add_to_schedule, get_item (read-only confirmation)
- Admin: create_content_type, update_content_type_schema,
  commit_content_type, extend_content_type_schema
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

CMS_API_URL = os.environ.get("CMS_API_URL", "")
CMS_API_KEY = os.environ.get("CMS_API_KEY", "")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"users API-Key {CMS_API_KEY}",
        "Content-Type": "application/json",
    }


def _api(path: str) -> str:
    return f"{CMS_API_URL}{path}"


# ---------------------------------------------------------------------------
# Editing mode tools
# ---------------------------------------------------------------------------


def handle_cms_get_item(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    r = httpx.get(_api(f"/api/content-items/{item_id}"), headers=_headers(), params={"depth": 1}, timeout=10)
    if r.status_code == 404:
        return json.dumps({"status": "not_found", "message": f"Content item {item_id} not found."})
    r.raise_for_status()
    item = r.json()

    ct = item.get("contentType")
    guidelines = {}
    if isinstance(ct, dict) and ct.get("schema"):
        for field_def in ct["schema"]:
            if field_def.get("guidelines"):
                guidelines[field_def["name"]] = field_def["guidelines"]

    return json.dumps({"status": "ok", "item": item, "field_guidelines": guidelines})


def handle_cms_create_item(args: dict, **_kw) -> str:
    body = {
        "org": args["orgId"],
        "contentType": args["contentTypeId"],
        "data": args["data"],
        "workflow_status": "draft",
    }
    if args.get("slug"):
        body["slug"] = args["slug"]
    r = httpx.post(_api("/api/content-items"), headers=_headers(), json=body, timeout=10)
    r.raise_for_status()
    return json.dumps({"status": "ok", "item": r.json()["doc"]})


def handle_cms_update_item(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    r_check = httpx.get(_api(f"/api/content-items/{item_id}"), headers=_headers(), timeout=10)
    if r_check.status_code == 404:
        return json.dumps({"status": "not_found", "message": f"Content item {item_id} not found."})
    r_check.raise_for_status()
    existing = r_check.json()

    locked_by = existing.get("locked_by")
    caller = _kw.get("caller_email", "")
    if locked_by and locked_by != caller:
        return json.dumps({"status": "locked", "message": f"Item is locked by {locked_by}."})

    body: dict = {}
    if "data" in args:
        body["data"] = args["data"]
    if "slug" in args:
        body["slug"] = args["slug"]
    r = httpx.patch(_api(f"/api/content-items/{item_id}"), headers=_headers(), json=body, timeout=10)
    r.raise_for_status()
    return json.dumps({"status": "ok", "item": r.json()["doc"]})


def handle_cms_submit_for_approval(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    r_check = httpx.get(_api(f"/api/content-items/{item_id}"), headers=_headers(), timeout=10)
    if r_check.status_code == 404:
        return json.dumps({"status": "not_found"})
    r_check.raise_for_status()
    current = r_check.json().get("workflow_status", "draft")
    if current not in ("draft", "changes_requested"):
        return json.dumps({"status": "invalid_state", "message": f"Cannot submit from state '{current}'. Must be draft or changes_requested."})

    r = httpx.patch(
        _api(f"/api/content-items/{item_id}"),
        headers=_headers(),
        json={"workflow_status": "needs_approval"},
        timeout=10,
    )
    r.raise_for_status()
    return json.dumps({"status": "ok", "workflow_status": "needs_approval"})


def handle_cms_restore_version(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    version_id = args["versionId"]
    r = httpx.post(
        _api(f"/api/content-items/{item_id}/versions/{version_id}"),
        headers=_headers(),
        json={"restoreVersion": True},
        timeout=10,
    )
    r.raise_for_status()
    restored = httpx.get(_api(f"/api/content-items/{item_id}"), headers=_headers(), timeout=10)
    restored.raise_for_status()
    return json.dumps({"status": "ok", "item": restored.json()})


def handle_cms_lock_item(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    caller = _kw.get("caller_email", "")
    r_check = httpx.get(_api(f"/api/content-items/{item_id}"), headers=_headers(), timeout=10)
    r_check.raise_for_status()
    existing_lock = r_check.json().get("locked_by")
    if existing_lock and existing_lock == caller:
        return json.dumps({"status": "ok", "message": "Already locked by you."})
    if existing_lock:
        return json.dumps({"status": "locked", "message": f"Item is locked by {existing_lock}."})

    r = httpx.patch(_api(f"/api/content-items/{item_id}"), headers=_headers(), json={"locked_by": caller}, timeout=10)
    r.raise_for_status()
    return json.dumps({"status": "ok", "locked_by": caller})


def handle_cms_unlock_item(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    caller = _kw.get("caller_email", "")
    r_check = httpx.get(_api(f"/api/content-items/{item_id}"), headers=_headers(), timeout=10)
    r_check.raise_for_status()
    existing_lock = r_check.json().get("locked_by")
    if existing_lock and existing_lock != caller:
        return json.dumps({"status": "error", "message": f"Cannot unlock — locked by {existing_lock}."})

    r = httpx.patch(_api(f"/api/content-items/{item_id}"), headers=_headers(), json={"locked_by": None}, timeout=10)
    r.raise_for_status()
    return json.dumps({"status": "ok", "locked_by": None})


def handle_cms_add_to_schedule(args: dict, **_kw) -> str:
    schedule_name = args["scheduleName"]
    publish_at = args["publishAt"]
    content_type_id = args["contentTypeId"]

    r_search = httpx.get(
        _api("/api/schedules"),
        headers=_headers(),
        params={"where[name][equals]": schedule_name, "limit": 1},
        timeout=10,
    )
    r_search.raise_for_status()
    docs = r_search.json().get("docs", [])

    if docs:
        schedule = docs[0]
        existing_ids = [ct if isinstance(ct, int) else ct["id"] for ct in (schedule.get("contentTypes") or [])]
        if content_type_id not in existing_ids:
            existing_ids.append(content_type_id)
        r = httpx.patch(
            _api(f"/api/schedules/{schedule['id']}"),
            headers=_headers(),
            json={"publishAt": publish_at, "contentTypes": existing_ids},
            timeout=10,
        )
        r.raise_for_status()
        return json.dumps({"status": "ok", "schedule": r.json()["doc"]})
    else:
        r = httpx.post(
            _api("/api/schedules"),
            headers=_headers(),
            json={"name": schedule_name, "publishAt": publish_at, "contentTypes": [content_type_id]},
            timeout=10,
        )
        r.raise_for_status()
        return json.dumps({"status": "ok", "schedule": r.json()["doc"]})


# ---------------------------------------------------------------------------
# Admin mode tools (content type management)
# ---------------------------------------------------------------------------


def handle_cms_create_content_type(args: dict, **_kw) -> str:
    body = {
        "org": args["orgId"],
        "name": args["name"],
        "schema": args.get("schema", []),
        "status": "draft",
    }
    if args.get("slug"):
        body["slug"] = args["slug"]
    r = httpx.post(_api("/api/content-types"), headers=_headers(), json=body, timeout=10)
    r.raise_for_status()
    doc = r.json()["doc"]
    return json.dumps({"status": "ok", "contentType": doc, "slug": doc.get("slug")})


def handle_cms_update_content_type_schema(args: dict, **_kw) -> str:
    ct_id = args["contentTypeId"]
    r_check = httpx.get(_api(f"/api/content-types/{ct_id}"), headers=_headers(), timeout=10)
    r_check.raise_for_status()
    ct = r_check.json()
    if ct.get("status") == "committed":
        return json.dumps({"status": "error", "message": "Cannot overwrite schema on a committed content type. Use extend instead."})

    r = httpx.patch(_api(f"/api/content-types/{ct_id}"), headers=_headers(), json={"schema": args["schema"]}, timeout=10)
    r.raise_for_status()
    return json.dumps({"status": "ok", "contentType": r.json()["doc"]})


def handle_cms_commit_content_type(args: dict, **_kw) -> str:
    ct_id = args["contentTypeId"]
    r_check = httpx.get(_api(f"/api/content-types/{ct_id}"), headers=_headers(), timeout=10)
    r_check.raise_for_status()
    ct = r_check.json()
    if ct.get("status") == "committed":
        return json.dumps({"status": "ok", "message": "Already committed."})

    merged_schema = ct.get("schema", [])
    proposed = ct.get("proposed_fields") or []
    if proposed:
        merged_schema = merged_schema + proposed

    r = httpx.patch(
        _api(f"/api/content-types/{ct_id}"),
        headers=_headers(),
        json={"status": "committed", "schema": merged_schema, "proposed_fields": None},
        timeout=10,
    )
    r.raise_for_status()
    return json.dumps({"status": "ok", "contentType": r.json()["doc"]})


def handle_cms_extend_content_type_schema(args: dict, **_kw) -> str:
    ct_id = args["contentTypeId"]
    r_check = httpx.get(_api(f"/api/content-types/{ct_id}"), headers=_headers(), timeout=10)
    r_check.raise_for_status()
    ct = r_check.json()
    if ct.get("status") != "committed":
        return json.dumps({"status": "error", "message": "Can only extend committed content types. Use update_schema for draft types."})

    r = httpx.patch(
        _api(f"/api/content-types/{ct_id}"),
        headers=_headers(),
        json={"proposed_fields": args["proposedFields"]},
        timeout=10,
    )
    r.raise_for_status()
    return json.dumps({"status": "ok", "contentType": r.json()["doc"]})


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

CMS_TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "cms_get_item",
            "description": "Fetch a single content item with its full data, content type schema, and per-field guidelines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "integer", "description": "Payload ID of the content item."},
                },
                "required": ["itemId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_create_item",
            "description": "Create a new content item in draft state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "orgId": {"type": "integer", "description": "Payload ID of the org."},
                    "contentTypeId": {"type": "integer", "description": "Payload ID of the content type."},
                    "data": {"type": "object", "description": "Content field values as key-value pairs."},
                    "slug": {"type": "string", "description": "Optional URL-safe slug."},
                },
                "required": ["orgId", "contentTypeId", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_update_item",
            "description": "Update fields on an existing content item. Only send the fields that changed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "integer", "description": "Payload ID of the content item."},
                    "data": {"type": "object", "description": "Updated content field values."},
                    "slug": {"type": "string", "description": "Optional updated slug."},
                },
                "required": ["itemId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_submit_for_approval",
            "description": "Submit a draft or changes-requested item for approval. Moves workflow_status to needs_approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "integer", "description": "Payload ID of the content item."},
                },
                "required": ["itemId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_restore_version",
            "description": "Restore a content item to a previous version. Creates a new version at the top of the history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "integer", "description": "Payload ID of the content item."},
                    "versionId": {"type": "integer", "description": "Payload version ID to restore."},
                },
                "required": ["itemId", "versionId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_lock_item",
            "description": "Lock a content item for editing. Prevents other users from editing simultaneously.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "integer", "description": "Payload ID of the content item."},
                },
                "required": ["itemId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_unlock_item",
            "description": "Unlock a content item after editing is complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "integer", "description": "Payload ID of the content item."},
                },
                "required": ["itemId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_add_to_schedule",
            "description": "Add a content type to a named publish schedule. Creates the schedule if it doesn't exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scheduleName": {"type": "string", "description": "Name of the schedule (e.g. 'Q2 Launch')."},
                    "publishAt": {"type": "string", "description": "ISO 8601 date-time for the scheduled publish."},
                    "contentTypeId": {"type": "integer", "description": "Payload ID of the content type to add."},
                },
                "required": ["scheduleName", "publishAt", "contentTypeId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_create_content_type",
            "description": "Create a new content type (collection) in draft status. Admin only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "orgId": {"type": "integer", "description": "Payload ID of the org."},
                    "name": {"type": "string", "description": "Human-readable name (e.g. 'Job Listings')."},
                    "slug": {"type": "string", "description": "Optional URL-safe slug."},
                    "schema": {
                        "type": "array",
                        "description": "Field definitions: [{name, type, required, ui, guidelines}].",
                        "items": {"type": "object"},
                    },
                },
                "required": ["orgId", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_update_content_type_schema",
            "description": "Overwrite the schema on a draft content type. Rejected for committed types.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contentTypeId": {"type": "integer", "description": "Payload ID of the content type."},
                    "schema": {
                        "type": "array",
                        "description": "Full replacement schema: [{name, type, required, ui, guidelines}].",
                        "items": {"type": "object"},
                    },
                },
                "required": ["contentTypeId", "schema"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_commit_content_type",
            "description": "Commit a draft content type to production. Merges any proposed_fields into the schema.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contentTypeId": {"type": "integer", "description": "Payload ID of the content type."},
                },
                "required": ["contentTypeId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_extend_content_type_schema",
            "description": "Propose new field additions to a committed content type. Existing committed fields are untouched.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contentTypeId": {"type": "integer", "description": "Payload ID of the content type."},
                    "proposedFields": {
                        "type": "array",
                        "description": "Proposed new fields: [{name, type, required, ui, guidelines}].",
                        "items": {"type": "object"},
                    },
                },
                "required": ["contentTypeId", "proposedFields"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Handler dispatch map
# ---------------------------------------------------------------------------

CMS_TOOL_HANDLERS: dict[str, callable] = {
    "cms_get_item": handle_cms_get_item,
    "cms_create_item": handle_cms_create_item,
    "cms_update_item": handle_cms_update_item,
    "cms_submit_for_approval": handle_cms_submit_for_approval,
    "cms_restore_version": handle_cms_restore_version,
    "cms_lock_item": handle_cms_lock_item,
    "cms_unlock_item": handle_cms_unlock_item,
    "cms_add_to_schedule": handle_cms_add_to_schedule,
    "cms_create_content_type": handle_cms_create_content_type,
    "cms_update_content_type_schema": handle_cms_update_content_type_schema,
    "cms_commit_content_type": handle_cms_commit_content_type,
    "cms_extend_content_type_schema": handle_cms_extend_content_type_schema,
}


# ---------------------------------------------------------------------------
# Mode-specific tool subsets
# ---------------------------------------------------------------------------

_EDITING_TOOL_NAMES = {
    "cms_get_item", "cms_create_item", "cms_update_item",
    "cms_submit_for_approval", "cms_restore_version",
    "cms_lock_item", "cms_unlock_item", "cms_add_to_schedule",
}

_SCHEDULING_TOOL_NAMES = {
    "cms_add_to_schedule", "cms_get_item",
}

_ADMIN_TOOL_NAMES = {
    "cms_create_content_type", "cms_update_content_type_schema",
    "cms_commit_content_type", "cms_extend_content_type_schema",
}

CMS_EDITING_TOOLS = [t for t in CMS_TOOL_DEFINITIONS if t["function"]["name"] in _EDITING_TOOL_NAMES]
CMS_EDITING_HANDLERS = {k: v for k, v in CMS_TOOL_HANDLERS.items() if k in _EDITING_TOOL_NAMES}

CMS_SCHEDULING_TOOLS = [t for t in CMS_TOOL_DEFINITIONS if t["function"]["name"] in _SCHEDULING_TOOL_NAMES]
CMS_SCHEDULING_HANDLERS = {k: v for k, v in CMS_TOOL_HANDLERS.items() if k in _SCHEDULING_TOOL_NAMES}

CMS_ADMIN_TOOLS = [t for t in CMS_TOOL_DEFINITIONS if t["function"]["name"] in _ADMIN_TOOL_NAMES]
CMS_ADMIN_HANDLERS = {k: v for k, v in CMS_TOOL_HANDLERS.items() if k in _ADMIN_TOOL_NAMES}
