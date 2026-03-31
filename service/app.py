"""FastAPI agent service — chat endpoint with OpenAI tool-calling."""

import json
import logging
import os
from datetime import date

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query

load_dotenv(interpolate=False)
from pydantic import BaseModel
from openai import OpenAI

from .prompts import VENDOR_AGENT_SYSTEM_PROMPT
from .tools import TOOL_DEFINITIONS, TOOL_HANDLERS
from . import pg_client
from .auth import get_verified_user

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Haderach Agent Service", root_path="/agent/api")

ADMIN_ROLES = {"admin"}
FINANCE_ADMIN_ROLES = {"finance_admin"}


def _get_caller_roles(caller: dict) -> tuple[str, set[str]]:
    """Load the caller's roles. Returns (email, roles set)."""
    email = caller.get("email", "")
    if not email:
        raise HTTPException(status_code=403, detail="No email in token")
    user = pg_client.get_user(email.strip().lower())
    if not user:
        raise HTTPException(status_code=403, detail="User doc not found")
    return email, set(user.get("roles", []))


def require_admin(caller: dict) -> str:
    """Verify the caller has the admin role. Returns the caller email."""
    email, roles = _get_caller_roles(caller)
    if not ADMIN_ROLES.intersection(roles):
        raise HTTPException(status_code=403, detail="Requires admin role")
    return email


def _resolve_caller_access(caller: dict) -> set[str] | None:
    """Resolve the caller's effective vendor set for REST endpoint filtering.

    Returns None for finance_admin (full access) or a set of allowed vendor
    IDs for restricted users. Empty set means no vendor access.
    """
    email = caller.get("email", "")
    if not email:
        return set()
    ctx = pg_client.get_user_access_context(email)
    if not ctx:
        return set()
    if "finance_admin" in ctx.get("roles", []):
        return None
    return set(pg_client.resolve_effective_vendor_ids(
        ctx.get("allowed_departments", []),
        ctx.get("allowed_vendor_ids", []),
        ctx.get("denied_vendor_ids", []),
    ))

MAX_TOOL_RESULT_CHARS = 20_000


def _truncate_tool_result(result: str) -> str:
    """Cap tool-result strings so they don't bloat the conversation context."""
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result
    truncated = result[:MAX_TOOL_RESULT_CHARS]
    return (
        truncated
        + f"\n\n[TRUNCATED — output was {len(result):,} chars, "
        f"showing first {MAX_TOOL_RESULT_CHARS:,}. "
        "Summarize what you have and tell the user if data was cut off.]"
    )

client: OpenAI | None = None
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def get_openai_client() -> OpenAI:
    global client
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        client = OpenAI(api_key=api_key)
    return client


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    context: dict | None = None


class PendingAction(BaseModel):
    type: str
    vendor_id: str
    vendor_name: str
    proposed_updates: dict | None = None
    display_fields: list[dict] | None = None


class Disambiguation(BaseModel):
    candidates: list[dict]
    original_args: dict | None = None


class ChatResponse(BaseModel):
    reply: str
    tool_calls_executed: list[str]
    pending_actions: list[PendingAction] = []
    disambiguation: Disambiguation | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.delete("/vendors/{vendor_id}")
def delete_vendor(vendor_id: str, caller: dict = Depends(get_verified_user)):
    vendor = pg_client.get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail=f"Vendor '{vendor_id}' not found")
    if vendor.get("sourceSystem") != "manual":
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{vendor.get('name')}' is synced from {vendor.get('sourceSystem')} and can't be deleted — "
                "it would be re-created on the next nightly sync."
            ),
        )
    pg_client.delete_vendor(vendor_id)
    return {"ok": True, "deleted": vendor_id}


class CreateUserRequest(BaseModel):
    email: str
    firstName: str = ""
    lastName: str = ""
    roles: list[str] = []


class UpdateUserRequest(BaseModel):
    roles: list[str] | None = None
    firstName: str | None = None
    lastName: str | None = None
    allowedDepartments: list[str] | None = None
    allowedVendorIds: list[str] | None = None
    deniedVendorIds: list[str] | None = None


@app.get("/users")
def list_users(
    role: list[str] | None = Query(default=None),
    caller: dict = Depends(get_verified_user),
):
    return pg_client.list_users(role if role else None)


@app.get("/me")
def get_current_user(caller: dict = Depends(get_verified_user)):
    email = caller.get("email", "").strip().lower()
    if not email:
        raise HTTPException(status_code=403, detail="No email in token")
    user = pg_client.get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail="User doc not found")
    return user


@app.get("/users/{email}")
def get_user(email: str, caller: dict = Depends(get_verified_user)):
    user = pg_client.get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{email}' not found")
    return user


@app.post("/users", status_code=201)
def create_user(req: CreateUserRequest, caller: dict = Depends(get_verified_user)):
    require_admin(caller)
    try:
        return pg_client.create_user(req.email, req.firstName, req.lastName, req.roles)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/users/{email}")
def update_user(email: str, req: UpdateUserRequest, caller: dict = Depends(get_verified_user)):
    _email, caller_roles = _get_caller_roles(caller)

    admin_fields = req.roles is not None or req.firstName is not None or req.lastName is not None
    access_fields = (
        req.allowedDepartments is not None
        or req.allowedVendorIds is not None
        or req.deniedVendorIds is not None
    )

    if admin_fields and not ADMIN_ROLES.intersection(caller_roles):
        raise HTTPException(status_code=403, detail="Requires admin role to modify roles/name")
    if access_fields and not FINANCE_ADMIN_ROLES.intersection(caller_roles):
        raise HTTPException(status_code=403, detail="Requires finance_admin role to modify vendor access")

    try:
        return pg_client.update_user(
            email,
            roles=req.roles,
            first_name=req.firstName,
            last_name=req.lastName,
            allowed_departments=req.allowedDepartments,
            allowed_vendor_ids=req.allowedVendorIds,
            denied_vendor_ids=req.deniedVendorIds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/users/{email}")
def delete_user_endpoint(email: str, caller: dict = Depends(get_verified_user)):
    require_admin(caller)
    if not pg_client.delete_user(email):
        raise HTTPException(status_code=404, detail=f"User '{email}' not found")
    return {"ok": True, "deleted": email}


class UpdateAppRequest(BaseModel):
    label: str | None = None
    granting_roles: list[str] | None = None
    sort_order: int | None = None


@app.get("/apps")
def list_apps(caller: dict = Depends(get_verified_user)):
    return pg_client.list_apps()


@app.patch("/apps/{app_id}")
def update_app(app_id: str, req: UpdateAppRequest, caller: dict = Depends(get_verified_user)):
    require_admin(caller)
    try:
        return pg_client.update_app(
            app_id,
            label=req.label,
            granting_roles=req.granting_roles,
            sort_order=req.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/departments")
def list_departments(caller: dict = Depends(get_verified_user)):
    return pg_client.list_departments()


@app.get("/vendors")
def list_vendors(caller: dict = Depends(get_verified_user)):
    vendors = pg_client.list_vendors()
    allowed = _resolve_caller_access(caller)
    if allowed is None:
        return vendors
    return [v for v in vendors if v.get("id") in allowed]


_VENDOR_FIELD_MAP: dict[str, str] = {
    "departmentId": "department_id",
    "ownerId": "owner_id",
    "secondaryOwnerId": "secondary_owner_id",
    "paymentMethod": "payment_method",
    "billingFrequency": "billing_frequency",
    "accountType": "account_type",
    "track1099": "track_1099",
    "spendType": "spend_type",
    "contractStartDate": "contract_start",
    "contractEndDate": "contract_end",
    "contractLengthMonths": "contract_months",
    "autoRenew": "auto_renew",
    "renewalRate": "renewal_rate",
    "renewalNoticeDays": "renewal_notice",
    "terminationTerms": "termination_terms",
}


def _map_vendor_fields(updates: dict) -> dict:
    """Translate camelCase frontend keys to snake_case DB column names."""
    mapped: dict = {}
    for key, value in updates.items():
        db_key = _VENDOR_FIELD_MAP.get(key, key)
        mapped[db_key] = value
    return mapped


@app.patch("/vendors/{vendor_id}")
def update_vendor(vendor_id: str, updates: dict, caller: dict = Depends(get_verified_user)):
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        result = pg_client.update_vendor(vendor_id, _map_vendor_fields(updates))
        return {"ok": True, "vendor": result}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/spend")
def get_spend(
    vendor_ids: list[str] | None = Query(default=None, alias="vendor_ids"),
    from_month: str = Query(..., alias="from"),
    to_month: str = Query(..., alias="to"),
    caller: dict = Depends(get_verified_user),
):
    allowed = _resolve_caller_access(caller)
    if vendor_ids is None or len(vendor_ids) == 0:
        if allowed is not None:
            vendor_ids = list(allowed)
        else:
            vendor_ids = [v["id"] for v in pg_client.list_vendors()]
    elif allowed is not None:
        vendor_ids = [v for v in vendor_ids if v in allowed]
    data = pg_client.query_spend_by_vendor_ids(vendor_ids, from_month, to_month)
    return {"data": data}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, caller: dict = Depends(get_verified_user)):
    openai_client = get_openai_client()
    caller_email = caller.get("email", "")
    logger.info("Chat request from %s", caller_email)

    today = date.today().isoformat()
    system_prompt = f"Today's date is {today}.\n\n{VENDOR_AGENT_SYSTEM_PROMPT}"
    messages = [{"role": "system", "content": system_prompt}]
    for m in req.messages:
        messages.append({"role": m.role, "content": m.content})

    tool_calls_executed: list[str] = []
    pending_actions: list[PendingAction] = []
    disambiguation: Disambiguation | None = None
    max_rounds = 10

    for _ in range(max_rounds):
        try:
            response = openai_client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
            )
        except Exception as exc:
            logger.error("OpenAI API error: %s", exc)
            raise HTTPException(status_code=502, detail="OpenAI API error") from exc

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            messages.append(choice.message.model_dump())

            for tc in choice.message.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)
                logger.info("Tool call: %s(%s)", fn_name, fn_args)

                handler = TOOL_HANDLERS.get(fn_name)
                if handler is None:
                    result = json.dumps({"ok": False, "error": f"Unknown tool: {fn_name}"})
                else:
                    result = handler(fn_args, caller_email=caller_email)

                tool_calls_executed.append(fn_name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _truncate_tool_result(result),
                })

                parsed = json.loads(result)
                if parsed.get("action") in ("confirm_delete", "open_edit", "confirm_edit"):
                    vendor = parsed["vendor"]
                    pending_actions.append(PendingAction(
                        type=parsed["action"],
                        vendor_id=vendor["id"],
                        vendor_name=vendor["name"],
                        proposed_updates=parsed.get("proposed_updates"),
                        display_fields=parsed.get("display_fields"),
                    ))
                elif parsed.get("status") == "ambiguous":
                    field_args = {k: v for k, v in fn_args.items() if k != "identifier" and v is not None}
                    disambiguation = Disambiguation(
                        candidates=parsed.get("candidates", []),
                        original_args=field_args if field_args else None,
                    )

            continue

        reply = choice.message.content or ""
        return ChatResponse(reply=reply, tool_calls_executed=tool_calls_executed, pending_actions=pending_actions, disambiguation=disambiguation)

    return ChatResponse(
        reply="I hit the maximum number of tool-call rounds. Please try again.",
        tool_calls_executed=tool_calls_executed,
        pending_actions=pending_actions,
        disambiguation=disambiguation,
    )
