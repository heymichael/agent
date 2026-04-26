"""CMS tool definitions and Payload REST API handlers.

All handlers call the Payload CMS REST API using CMS_API_URL and
CMS_API_KEY. CMS_API_URL is the base of the CMS mount — locally
"http://localhost:3000/cms", in production "https://haderach.ai/cms".
The "/cms" suffix matches the Next.js `basePath` set in
haderach-cms/next.config.ts and the Firebase Hosting `/cms/api/**`
rewrite. The `_api()` helper concatenates `/api/<resource>` onto this
base, so callers pass paths like "/api/orgs" or "/api/content-items".

Tools are grouped by agent mode:

- Editing: get, create, update, lock/unlock, submit_for_approval,
  restore_version, add_to_schedule
- Scheduling: add_to_schedule, get_item (read-only confirmation)
- Admin (schema design): create_content_type, update_content_type_schema,
  set_active_content_type

Multi-org gating (task 254 Phase 5)
-----------------------------------

Every handler derives its tenant from the caller's `active_org_slug`
contextvar (populated once per request in `service/app.py:chat()` and
in the `/cms/*` REST passthroughs). Tools no longer take `orgId` as a
required argument; the two write tools that historically did keep
`orgId` as an optional schema arg purely for backward-compat
validation: if a model emits one and it disagrees with the resolved
caller org, the handler errors out instead of silently writing into
the wrong tenant.

By-ID reads/writes (`get_item`, `update_item`, `lock`, `unlock`,
`submit_for_approval`, `restore_version`, and the three content-type
admin tools) re-fetch with `depth=1` and verify `doc.org.slug ==
caller_slug`. Mismatches return `{"status": "not_found"}` so existence
of cross-tenant items is not leaked to the caller.
"""

import contextvars
import json
import logging
import os
import threading

import httpx

from . import tools as tools_module

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Active content type tracking (task 290)
#
# Within a single /chat request, multiple tool calls may reference the same
# content type. This contextvar tracks which content type the agent is
# currently working on. Handlers set it as a side effect of successful
# create/update operations, and cms_set_active_content_type explicitly
# switches it.
#
# NOTE: This is request-scoped (single turn). For true cross-turn persistence,
# the active content type would need to be stored in session metadata.
# ---------------------------------------------------------------------------

_active_content_type_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "active_content_type_id", default=None
)


def set_active_content_type_id(ct_id: int | None) -> None:
    """Set the active content type for the current request."""
    _active_content_type_id.set(ct_id)


def get_active_content_type_id() -> int | None:
    """Get the active content type for the current request."""
    return _active_content_type_id.get()

CMS_API_URL = os.environ.get("CMS_API_URL", "")
_cms_key_ref = os.environ.get("CMS_API_KEY", "")
CMS_API_KEY = open(_cms_key_ref).read().strip() if _cms_key_ref and os.path.isfile(_cms_key_ref) else _cms_key_ref


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"users API-Key {CMS_API_KEY}",
        "Content-Type": "application/json",
    }


def _api(path: str) -> str:
    return f"{CMS_API_URL}{path}"


# ---------------------------------------------------------------------------
# Slug -> Payload-numeric-id resolver (task 254 Phase 5)
#
# The mapping from org slug to Payload's auto-incremented numeric id is
# essentially static (orgs are added by a manual playbook step, never
# renamed in place). Cache process-wide; refresh on miss. Lock guards
# concurrent first-time misses across worker threads, but the happy
# path (cache hit) is lock-free.
# ---------------------------------------------------------------------------

_org_id_cache: dict[str, int] = {}
_org_id_cache_lock = threading.Lock()


def _fetch_payload_org_id(slug: str) -> int:
    r = httpx.get(
        _api("/api/orgs"),
        headers=_headers(),
        params={"where[slug][equals]": slug, "limit": 1, "depth": 0},
        timeout=10,
    )
    r.raise_for_status()
    docs = r.json().get("docs", [])
    if not docs:
        raise ValueError(f"No Payload org found for slug '{slug}'")
    return int(docs[0]["id"])


def resolve_payload_org_id(slug: str) -> int:
    """Resolve an org slug to its Payload numeric id, cached process-wide."""
    cached = _org_id_cache.get(slug)
    if cached is not None:
        return cached
    with _org_id_cache_lock:
        cached = _org_id_cache.get(slug)
        if cached is not None:
            return cached
        org_id = _fetch_payload_org_id(slug)
        _org_id_cache[slug] = org_id
        return org_id


def _clear_org_id_cache() -> None:
    """Reset the resolver cache. Test-only helper."""
    with _org_id_cache_lock:
        _org_id_cache.clear()


def _require_caller_org_slug() -> str:
    slug = tools_module.get_caller_org_slug()
    if not slug:
        raise RuntimeError(
            "CMS tool invoked without an active org slug. "
            "Caller context (tools.set_caller_org_slug) must be set "
            "before any /chat or /cms/* request reaches a handler."
        )
    return slug


def _caller_payload_org_id() -> int:
    return resolve_payload_org_id(_require_caller_org_slug())


def _doc_org_slug(doc: dict) -> str | None:
    """Extract the nested org slug from a Payload doc fetched at depth>=1.

    Returns None if the relationship was returned as a bare id (depth=0).
    """
    org = doc.get("org")
    if isinstance(org, dict):
        return org.get("slug")
    return None


def _belongs_to_caller(doc: dict) -> bool:
    """True iff the fetched doc belongs to the caller's active org.

    Caller MUST have fetched the doc at depth>=1 so the org relationship
    is hydrated. Treats a bare-id org (depth=0) as a programmer error
    rather than silently passing — that would be a tenant-leak hazard.
    """
    doc_slug = _doc_org_slug(doc)
    if doc_slug is None:
        raise RuntimeError(
            "Cross-tenant guard called on a doc without nested org. "
            "Fetch with depth>=1 before calling _belongs_to_caller."
        )
    return doc_slug == _require_caller_org_slug()


def _not_found(item_id: int | str) -> str:
    return json.dumps({"status": "not_found", "message": f"Content item {item_id} not found."})


def _ct_not_found(ct_id: int | str) -> str:
    return json.dumps({"status": "not_found", "message": f"Content type {ct_id} not found."})


# ---------------------------------------------------------------------------
# Schema field validation (task 290)
# ---------------------------------------------------------------------------

V1_FIELD_TYPES = {"text", "richtext", "number", "date", "boolean", "select", "url", "email"}
FUTURE_FIELD_TYPES = {"image", "media", "relationship"}
_DEFAULT_GUIDELINES = "Provide content following the established voice and style."


def _validate_field(field: dict) -> str | None:
    """Return error message if field is invalid, None if valid."""
    name = field.get("name")
    if not name:
        return "Field is missing a 'name'."

    ftype = field.get("type")
    if not ftype:
        return f"Field '{name}' is missing a 'type'."

    if ftype not in V1_FIELD_TYPES:
        if ftype in FUTURE_FIELD_TYPES:
            return f"Field '{name}': '{ftype}' is not supported in V1. This field type is planned for a future release."
        return f"Field '{name}': Unknown type '{ftype}'. Supported: {', '.join(sorted(V1_FIELD_TYPES))}."

    if ftype == "select":
        options = field.get("options")
        if not options:
            return f"Field '{name}': Select fields require an 'options' array."
        if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
            return f"Field '{name}': 'options' must be an array of strings."

    return None


def _validate_schema(schema: list[dict]) -> list[str]:
    """Validate all fields. Returns list of error messages (empty if valid)."""
    return [err for field in schema if (err := _validate_field(field))]


def _ensure_richtext_guidelines(schema: list[dict]) -> tuple[list[dict], list[str]]:
    """Add default guidelines to richtext fields that lack them.

    Returns (schema, list_of_field_names_that_got_defaults).
    """
    generated_for = []
    for field in schema:
        if field.get("type") == "richtext" and not field.get("guidelines"):
            field["guidelines"] = _DEFAULT_GUIDELINES
            generated_for.append(field["name"])
    return schema, generated_for


def _apply_required_defaults(schema: list[dict]) -> None:
    """Set required=True on fields that don't specify it."""
    for field in schema:
        if "required" not in field:
            field["required"] = True


def _slugify(text: str) -> str:
    """Convert text to snake_case slug."""
    import re
    # Replace spaces and hyphens with underscores
    slug = re.sub(r"[\s\-]+", "_", text.strip())
    # Remove non-alphanumeric chars except underscore
    slug = re.sub(r"[^\w]", "", slug)
    # Convert to lowercase
    slug = slug.lower()
    # Collapse multiple underscores
    slug = re.sub(r"_+", "_", slug)
    # Strip leading/trailing underscores
    return slug.strip("_")


def _normalize_field_names(schema: list[dict]) -> list[str]:
    """Ensure each field has both name (slug) and label.
    
    - If label is present but name is missing, auto-generate name from label
    - If name is present but label is missing, copy name to label
    - Returns list of error messages for fields missing both
    """
    errors = []
    for field in schema:
        label = field.get("label")
        name = field.get("name")
        
        if label and not name:
            # Auto-generate slug from label
            field["name"] = _slugify(label)
        elif name and not label:
            # Use name as label (backward compat)
            field["label"] = name
        elif not name and not label:
            errors.append("Field missing both 'name' and 'label'")
    
    return errors


def _validate_org_id_arg(args: dict) -> str | None:
    """Reject `orgId` if present and pointing at a different tenant.

    Backward-compat shim for the two write tools (`cms_create_item`,
    `cms_create_content_type`) that historically required `orgId`. New
    callers should omit it entirely; the handler resolves from caller
    context. If a model still emits an `orgId`, validate it matches the
    caller's resolved id rather than silently writing into the wrong
    tenant.

    Returns a JSON error string on mismatch, or None on pass.
    """
    if "orgId" not in args or args["orgId"] is None:
        return None
    try:
        supplied = int(args["orgId"])
    except (TypeError, ValueError):
        return json.dumps({
            "status": "error",
            "message": f"orgId must be an integer, got {args['orgId']!r}.",
        })
    expected = _caller_payload_org_id()
    if supplied != expected:
        return json.dumps({
            "status": "error",
            "message": (
                f"orgId {supplied} does not match the caller's active org "
                f"(slug='{_require_caller_org_slug()}', id={expected}). "
                f"Omit orgId — it is now derived from caller context."
            ),
        })
    return None


# ---------------------------------------------------------------------------
# Editing mode tools
# ---------------------------------------------------------------------------


def _fetch_item(item_id: int | str) -> dict | None:
    """Fetch a content item at depth=1 with cross-tenant guard.

    Returns the item dict on success, or None on Payload 404 / cross-tenant
    mismatch (caller should translate None to a `not_found` response so
    cross-tenant existence is not leaked).
    """
    r = httpx.get(_api(f"/api/content-items/{item_id}"), headers=_headers(), params={"depth": 1}, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    item = r.json()
    if not _belongs_to_caller(item):
        return None
    return item


def _fetch_content_type(ct_id: int | str) -> dict | None:
    r = httpx.get(_api(f"/api/content-types/{ct_id}"), headers=_headers(), params={"depth": 1}, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    ct = r.json()
    if not _belongs_to_caller(ct):
        return None
    return ct


def handle_cms_get_item(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    item = _fetch_item(item_id)
    if item is None:
        return _not_found(item_id)

    ct = item.get("contentType")
    guidelines = {}
    if isinstance(ct, dict) and ct.get("schema"):
        schema = ct["schema"]
        fields = schema.get("fields", schema) if isinstance(schema, dict) else schema
        for field_def in fields:
            if field_def.get("guidelines"):
                guidelines[field_def["name"]] = field_def["guidelines"]

    return json.dumps({"status": "ok", "item": item, "field_guidelines": guidelines})


def handle_cms_create_item(args: dict, **_kw) -> str:
    mismatch = _validate_org_id_arg(args)
    if mismatch:
        return mismatch
    body = {
        "org": _caller_payload_org_id(),
        "contentType": args["contentTypeId"],
        "data": args["data"],
        "workflow_status": "draft",
        "_status": "published",
    }
    if args.get("slug"):
        body["slug"] = args["slug"]
    r = httpx.post(_api("/api/content-items"), headers=_headers(), json=body, timeout=10)
    r.raise_for_status()
    return json.dumps({"status": "ok", "item": r.json()["doc"]})


def handle_cms_update_item(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    existing = _fetch_item(item_id)
    if existing is None:
        return _not_found(item_id)

    locked_by = existing.get("locked_by")
    caller = _kw.get("caller_email", "")
    if locked_by and locked_by != caller:
        return json.dumps({"status": "locked", "message": f"Item is locked by {locked_by}."})

    body: dict = {"_status": "published"}
    if "data" in args:
        body["data"] = args["data"]
    else:
        content_fields = {k: v for k, v in args.items() if k not in ("itemId", "slug")}
        if content_fields:
            existing_data = existing.get("data") or {}
            body["data"] = {**existing_data, **content_fields}
    if "slug" in args:
        body["slug"] = args["slug"]
    r = httpx.patch(_api(f"/api/content-items/{item_id}"), headers=_headers(), json=body, timeout=10)
    r.raise_for_status()
    return json.dumps({"status": "ok", "item": r.json()["doc"]})


def handle_cms_submit_for_approval(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    existing = _fetch_item(item_id)
    if existing is None:
        return _not_found(item_id)
    current = existing.get("workflow_status", "draft")
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
    # Verify the parent item is in the caller's tenant before touching versions.
    if _fetch_item(item_id) is None:
        return _not_found(item_id)

    r = httpx.post(
        _api(f"/api/content-items/versions/{version_id}"),
        headers=_headers(),
        json={},
        timeout=10,
    )
    r.raise_for_status()
    # Payload restore resets _status to draft; re-publish so the item stays visible
    httpx.patch(
        _api(f"/api/content-items/{item_id}"),
        headers=_headers(),
        json={"_status": "published"},
        timeout=10,
    )
    restored = httpx.get(_api(f"/api/content-items/{item_id}"), headers=_headers(), timeout=10)
    restored.raise_for_status()
    return json.dumps({"status": "ok", "item": restored.json()})


def handle_cms_lock_item(args: dict, **_kw) -> str:
    item_id = args["itemId"]
    caller = _kw.get("caller_email", "")
    existing = _fetch_item(item_id)
    if existing is None:
        return _not_found(item_id)
    existing_lock = existing.get("locked_by")
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
    existing = _fetch_item(item_id)
    if existing is None:
        return _not_found(item_id)
    existing_lock = existing.get("locked_by")
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
    mismatch = _validate_org_id_arg(args)
    if mismatch:
        return mismatch

    schema = args.get("schema", [])

    # Normalize field names (auto-generate slug from label if needed)
    name_errors = _normalize_field_names(schema)
    if name_errors:
        return json.dumps({"status": "error", "message": "Field naming errors.", "errors": name_errors})

    # Validate field types
    errors = _validate_schema(schema)
    if errors:
        return json.dumps({"status": "error", "message": "Schema validation failed.", "errors": errors})

    # Apply defaults and auto-generate guidelines
    _apply_required_defaults(schema)
    schema, generated_guidelines = _ensure_richtext_guidelines(schema)

    body = {
        "org": _caller_payload_org_id(),
        "name": args["name"],
        "schema": schema,
        "status": "draft",
    }
    if args.get("slug"):
        body["slug"] = args["slug"]
    r = httpx.post(_api("/api/content-types"), headers=_headers(), json=body, timeout=10)
    r.raise_for_status()
    doc = r.json()["doc"]

    # Set as active content type for this request
    set_active_content_type_id(doc["id"])

    response = {"status": "ok", "contentType": doc, "slug": doc.get("slug")}
    if generated_guidelines:
        response["guidelines_generated_for"] = generated_guidelines
    return json.dumps(response)


def handle_cms_update_content_type_schema(args: dict, **_kw) -> str:
    ct_id = args.get("contentTypeId") or get_active_content_type_id()
    if ct_id is None:
        return json.dumps({"status": "error", "message": "Missing contentTypeId and no active content type. Specify which content type to update."})

    schema = args.get("schema")
    if schema is None:
        return json.dumps({
            "status": "error",
            "message": "Missing required parameter: schema. You must provide the full replacement schema array.",
        })

    ct = _fetch_content_type(ct_id)
    if ct is None:
        return _ct_not_found(ct_id)
    if ct.get("status") == "committed":
        return json.dumps({"status": "error", "message": "Cannot overwrite schema on a committed content type."})

    # Normalize field names (auto-generate slug from label if needed)
    name_errors = _normalize_field_names(schema)
    if name_errors:
        return json.dumps({"status": "error", "message": "Field naming errors.", "errors": name_errors})

    # Validate field types
    errors = _validate_schema(schema)
    if errors:
        return json.dumps({"status": "error", "message": "Schema validation failed.", "errors": errors})

    # Apply defaults and auto-generate guidelines
    _apply_required_defaults(schema)
    schema, generated_guidelines = _ensure_richtext_guidelines(schema)

    r = httpx.patch(_api(f"/api/content-types/{ct_id}"), headers=_headers(), json={"schema": schema}, timeout=10)
    r.raise_for_status()
    doc = r.json()["doc"]

    # Set as active content type for this request
    set_active_content_type_id(doc["id"])

    response = {"status": "ok", "contentType": doc}
    if generated_guidelines:
        response["guidelines_generated_for"] = generated_guidelines
    return json.dumps(response)


def handle_cms_commit_content_type(args: dict, **_kw) -> str:
    ct_id = args["contentTypeId"]
    ct = _fetch_content_type(ct_id)
    if ct is None:
        return _ct_not_found(ct_id)
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
    ct = _fetch_content_type(ct_id)
    if ct is None:
        return _ct_not_found(ct_id)
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


def handle_cms_set_active_content_type(args: dict, **_kw) -> str:
    """Set which content type the agent is currently working on."""
    ct_id = args["contentTypeId"]
    ct = _fetch_content_type(ct_id)
    if ct is None:
        return _ct_not_found(ct_id)

    # Set as active content type for this request
    set_active_content_type_id(ct_id)

    return json.dumps({
        "status": "ok",
        "activeContentType": {
            "id": ct_id,
            "name": ct.get("name"),
            "status": ct.get("status"),
        },
    })


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
            "description": "Create a new content item in draft state. Org is derived from the caller's active org — do not pass orgId.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contentTypeId": {"type": "integer", "description": "Payload ID of the content type."},
                    "data": {"type": "object", "description": "Content field values as key-value pairs."},
                    "slug": {"type": "string", "description": "Optional URL-safe slug."},
                },
                "required": ["contentTypeId", "data"],
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
            "description": "Create a new content type (collection) in draft status. Admin only. Org is derived from the caller's active org — do not pass orgId.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable name (e.g. 'Job Listings')."},
                    "slug": {"type": "string", "description": "Optional URL-safe slug."},
                    "schema": {
                        "type": "array",
                        "description": "Field definitions: [{name, label, type, required, guidelines?, options?}]. name=snake_case key, label=display text. Include options[] for select fields.",
                        "items": {"type": "object"},
                    },
                },
                "required": ["name"],
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
                        "description": "Full replacement schema: [{name, label, type, required, guidelines?, options?}]. name=snake_case key, label=display text. Include options[] for select fields.",
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
                        "description": "Proposed new fields: [{name, type, required, guidelines}].",
                        "items": {"type": "object"},
                    },
                },
                "required": ["contentTypeId", "proposedFields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cms_set_active_content_type",
            "description": "Set which content type the agent is currently working on. Call when switching context between content types.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contentTypeId": {"type": "integer", "description": "Payload ID of the content type to make active."},
                },
                "required": ["contentTypeId"],
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
    "cms_set_active_content_type": handle_cms_set_active_content_type,
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
    "cms_set_active_content_type",
}

CMS_EDITING_TOOLS = [t for t in CMS_TOOL_DEFINITIONS if t["function"]["name"] in _EDITING_TOOL_NAMES]
CMS_EDITING_HANDLERS = {k: v for k, v in CMS_TOOL_HANDLERS.items() if k in _EDITING_TOOL_NAMES}

CMS_SCHEDULING_TOOLS = [t for t in CMS_TOOL_DEFINITIONS if t["function"]["name"] in _SCHEDULING_TOOL_NAMES]
CMS_SCHEDULING_HANDLERS = {k: v for k, v in CMS_TOOL_HANDLERS.items() if k in _SCHEDULING_TOOL_NAMES}

CMS_ADMIN_TOOLS = [t for t in CMS_TOOL_DEFINITIONS if t["function"]["name"] in _ADMIN_TOOL_NAMES]
CMS_ADMIN_HANDLERS = {k: v for k, v in CMS_TOOL_HANDLERS.items() if k in _ADMIN_TOOL_NAMES}
