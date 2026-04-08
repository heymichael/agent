"""FastAPI agent service — chat endpoint with OpenAI tool-calling."""

import contextvars
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import date

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(interpolate=False)
from pydantic import BaseModel
from openai import OpenAI

from .prompts import EXPENSE_ANALYTICS_PROMPT, VENDOR_MANAGEMENT_PROMPT
from .tools import (
    EXPENSE_TOOL_DEFINITIONS, EXPENSE_TOOL_HANDLERS,
    VENDOR_TOOL_DEFINITIONS, VENDOR_TOOL_HANDLERS,
)
from . import pg_client
from .auth import get_verified_user
from .qbo_auth import get_authorization_url, exchange_code_for_tokens

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Haderach Agent Service", root_path="/agent/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://docs.haderach.ai"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

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


def require_finance_admin(caller: dict) -> str:
    """Verify the caller has the finance_admin role. Returns the caller email."""
    email, roles = _get_caller_roles(caller)
    if not FINANCE_ADMIN_ROLES.intersection(roles):
        raise HTTPException(status_code=403, detail="Requires finance_admin role")
    return email


def _resolve_caller_access(caller: dict) -> set[str] | None:
    """Resolve the caller's effective vendor set for REST endpoint filtering.

    Returns None for finance_admin (full access) or a set of allowed vendor
    IDs for restricted users. Empty set means no vendor access.
    Contractor vendors without an explicit grant are excluded.
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
        user_id=ctx.get("user_id"),
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
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


class Attachment(BaseModel):
    filename: str
    content: str
    mime: str = "text/csv"


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    context: dict | None = None
    attachments: list[Attachment] | None = None
    session_id: str | None = None


_request_attachments: contextvars.ContextVar[list[dict]] = contextvars.ContextVar(
    "request_attachments", default=[],
)


def get_request_attachments() -> list[dict]:
    """Read attachments stored for the current request (used by tool handlers)."""
    return _request_attachments.get()


class PendingAction(BaseModel):
    type: str
    vendor_id: str | None = None
    vendor_name: str | None = None
    proposed_updates: dict | None = None
    display_fields: list[dict] | None = None
    updates: list[dict] | None = None
    summary: dict | None = None


class Disambiguation(BaseModel):
    candidates: list[dict]
    original_args: dict | None = None


class Download(BaseModel):
    filename: str
    content: str
    mime: str = "text/csv"


class TablePayload(BaseModel):
    metric: str
    columns: list[str]
    rows: list[list]
    filename: str
    filters: dict[str, str] = {}


class ChatResponse(BaseModel):
    reply: str
    tool_calls_executed: list[str]
    pending_actions: list[PendingAction] = []
    disambiguation: Disambiguation | None = None
    downloads: list[Download] = []
    tables: list[TablePayload] = []
    session_id: str | None = None
    tool_messages: list[dict] = []


@dataclass
class AgentResult:
    """Pure output of a single agent loop — no HTTP or session concerns."""
    reply: str
    tool_calls_executed: list[str] = field(default_factory=list)
    pending_actions: list[PendingAction] = field(default_factory=list)
    disambiguation: Disambiguation | None = None
    downloads: list[Download] = field(default_factory=list)
    tables: list[TablePayload] = field(default_factory=list)
    tool_messages: list[dict] = field(default_factory=list)


def run_agent_loop(
    *,
    openai_client: OpenAI,
    system_prompt: str,
    messages_in: list[dict],
    tools: list[dict],
    tool_handlers: dict[str, callable],
    caller_email: str,
    max_rounds: int = 10,
) -> AgentResult:
    """Run the LLM tool-calling loop to completion.

    This is the pure orchestration core — no HTTP framework types, no
    ContextVar side-effects, no session persistence.  Both the /chat
    HTTP handler and the ask_expense_agent sub-agent tool call this.
    """
    messages = [{"role": "system", "content": system_prompt}] + list(messages_in)

    tool_calls_executed: list[str] = []
    pending_actions: list[PendingAction] = []
    disambiguation: Disambiguation | None = None
    downloads: list[Download] = []
    tables: list[TablePayload] = []
    tool_messages: list[dict] = []

    for _ in range(max_rounds):
        try:
            response = openai_client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception:
            logger.exception("OpenAI API error in agent loop")
            return AgentResult(reply="Sorry, I encountered an error contacting the AI service.")

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            assistant_tool_msg = choice.message.model_dump()
            messages.append(assistant_tool_msg)
            tool_messages.append(assistant_tool_msg)

            for tc in choice.message.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)
                logger.info("Tool call: %s(%s)", fn_name, fn_args)

                handler = tool_handlers.get(fn_name)
                if handler is None:
                    result = json.dumps({"ok": False, "error": f"Unknown tool: {fn_name}"})
                else:
                    result = handler(fn_args, caller_email=caller_email)

                tool_calls_executed.append(fn_name)

                parsed = json.loads(result)
                csv_content = parsed.pop("csv", None)
                csv_filename = parsed.pop("csv_filename", None)
                if csv_content and csv_filename:
                    downloads = [d for d in downloads if d.filename != csv_filename]
                    downloads.append(Download(filename=csv_filename, content=csv_content))

                tables_list = parsed.pop("tables", None)
                table_payload = parsed.pop("table", None)
                if tables_list:
                    for tp in tables_list:
                        tables.append(TablePayload(**tp))
                    parsed["_table_rendered"] = True
                elif table_payload:
                    tables.append(TablePayload(**table_payload))
                    parsed["_table_rendered"] = True

                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _truncate_tool_result(json.dumps(parsed)),
                }
                messages.append(tool_result_msg)
                tool_messages.append(tool_result_msg)

                if parsed.get("action") == "confirm_csv_batch":
                    pending_actions.append(PendingAction(
                        type="confirm_csv_batch",
                        updates=parsed.get("updates"),
                        summary=parsed.get("summary"),
                    ))
                elif parsed.get("action") in ("confirm_delete", "open_edit", "confirm_edit"):
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

        return AgentResult(
            reply=choice.message.content or "",
            tool_calls_executed=tool_calls_executed,
            pending_actions=pending_actions,
            disambiguation=disambiguation,
            downloads=downloads,
            tables=tables,
            tool_messages=tool_messages,
        )

    return AgentResult(
        reply="I hit the maximum number of tool-call rounds. Please try again.",
        tool_calls_executed=tool_calls_executed,
        pending_actions=pending_actions,
        disambiguation=disambiguation,
        downloads=downloads,
        tables=tables,
        tool_messages=tool_messages,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/branding")
def get_branding_config():
    row = pg_client.get_branding()
    if not row:
        return {"logoSvg": None, "lockupSvg": None, "lockupMode": "none"}
    return row


# ---------------------------------------------------------------------------
# QuickBooks OAuth2
# ---------------------------------------------------------------------------


@app.get("/qbo/auth")
def qbo_auth_start(caller: dict = Depends(get_verified_user)):
    """Redirect the user to Intuit's authorization page."""
    require_admin(caller)
    from starlette.responses import RedirectResponse

    redirect_uri = str(app.url_path_for("qbo_callback"))
    base = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    absolute_redirect = f"{base}{redirect_uri}"
    url = get_authorization_url(redirect_uri=absolute_redirect)
    return RedirectResponse(url)


@app.get("/qbo/callback")
def qbo_callback(code: str = "", realmId: str = "", state: str = "", error: str = ""):
    """Handle the OAuth2 callback from Intuit. Exchanges the code for tokens."""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error from Intuit: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    redirect_uri = str(app.url_path_for("qbo_callback"))
    base = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    absolute_redirect = f"{base}{redirect_uri}"

    token_data = exchange_code_for_tokens(code=code, redirect_uri=absolute_redirect)
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in")
    refresh_expires_in = token_data.get("x_refresh_token_expires_in")

    logger.info(
        "QBO OAuth complete — realmId=%s, access expires in %ss, refresh expires in %ss",
        realmId, expires_in, refresh_expires_in,
    )

    return {
        "ok": True,
        "realm_id": realmId,
        "refresh_token": refresh_token,
        "message": (
            "Copy the refresh_token into your VENDOR_QBO_CREDENTIALS secret "
            "(and update realm_id if it changed). "
            "This page will not show the token again."
        ),
    }


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


class BatchUpdateRequest(BaseModel):
    updates: list[dict]


@app.post("/vendors/batch-update")
def batch_update_vendors(req: BatchUpdateRequest, caller: dict = Depends(get_verified_user)):
    if not req.updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    try:
        count = pg_client.batch_update_vendors(req.updates)
        return {"ok": True, "updated": count}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Contractor management (finance_admin only)
# ---------------------------------------------------------------------------


class SetContractorRequest(BaseModel):
    is_contractor: bool


@app.patch("/vendors/{vendor_id}/contractor")
def set_vendor_contractor(
    vendor_id: str,
    req: SetContractorRequest,
    caller: dict = Depends(get_verified_user),
):
    email = require_finance_admin(caller)
    actor_id = pg_client.get_user_id_by_email(email)
    if not actor_id:
        raise HTTPException(status_code=403, detail="User not found")
    try:
        vendor = pg_client.set_vendor_is_contractor(vendor_id, req.is_contractor, actor_id)
        return {"ok": True, "vendor": vendor}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/vendors/contractors")
def list_contractor_vendors(caller: dict = Depends(get_verified_user)):
    require_finance_admin(caller)
    return pg_client.list_contractor_vendors()


@app.get("/vendors/{vendor_id}/access")
def list_vendor_access(vendor_id: str, caller: dict = Depends(get_verified_user)):
    require_finance_admin(caller)
    vendor = pg_client.get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail=f"Vendor '{vendor_id}' not found")
    return pg_client.list_contractor_access(vendor_id)


class GrantAccessRequest(BaseModel):
    user_email: str


@app.post("/vendors/{vendor_id}/access", status_code=201)
def grant_vendor_access(
    vendor_id: str,
    req: GrantAccessRequest,
    caller: dict = Depends(get_verified_user),
):
    email = require_finance_admin(caller)
    actor_id = pg_client.get_user_id_by_email(email)
    if not actor_id:
        raise HTTPException(status_code=403, detail="User not found")
    vendor = pg_client.get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail=f"Vendor '{vendor_id}' not found")
    target_user_id = pg_client.get_user_id_by_email(req.user_email)
    if not target_user_id:
        raise HTTPException(status_code=404, detail=f"User '{req.user_email}' not found")
    pg_client.grant_contractor_access(target_user_id, vendor_id, actor_id)
    return {"ok": True}


@app.delete("/vendors/{vendor_id}/access/{user_email}")
def revoke_vendor_access(
    vendor_id: str,
    user_email: str,
    caller: dict = Depends(get_verified_user),
):
    require_finance_admin(caller)
    vendor = pg_client.get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail=f"Vendor '{vendor_id}' not found")
    target_user_id = pg_client.get_user_id_by_email(user_email)
    if not target_user_id:
        raise HTTPException(status_code=404, detail=f"User '{user_email}' not found")
    pg_client.revoke_contractor_access(target_user_id, vendor_id)
    return {"ok": True}


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


def _execute_ask_expense_agent(args: dict, caller_email: str = "") -> str:
    """Delegate a spend question to the expense analytics agent via sub-agent loop."""
    openai_client = get_openai_client()
    today = date.today().isoformat()
    inner_result = run_agent_loop(
        openai_client=openai_client,
        system_prompt=f"Today's date is {today}.\n\n{EXPENSE_ANALYTICS_PROMPT}",
        messages_in=[{"role": "user", "content": args["question"]}],
        tools=EXPENSE_TOOL_DEFINITIONS,
        tool_handlers=EXPENSE_TOOL_HANDLERS,
        caller_email=caller_email,
    )
    response: dict = {
        "status": "ok",
        "reply": inner_result.reply,
    }
    if inner_result.tables:
        serialized = [
            {
                "metric": t.metric,
                "columns": t.columns,
                "rows": t.rows,
                "filename": t.filename,
                "filters": t.filters,
            }
            for t in inner_result.tables
        ]
        response["table"] = serialized[0]
        if len(serialized) > 1:
            response["tables"] = serialized
    return json.dumps(response)


def _resolve_domain(app_context: str, has_csv: bool) -> tuple[str, list[dict], dict]:
    """Return (system_prompt_body, tool_definitions, tool_handlers) for a domain."""
    if app_context == "expenses":
        return EXPENSE_ANALYTICS_PROMPT, EXPENSE_TOOL_DEFINITIONS, EXPENSE_TOOL_HANDLERS

    tools = VENDOR_TOOL_DEFINITIONS if has_csv else [
        t for t in VENDOR_TOOL_DEFINITIONS
        if t["function"]["name"] != "process_vendor_csv"
    ]
    handlers = {**VENDOR_TOOL_HANDLERS, "ask_expense_agent": _execute_ask_expense_agent}
    return VENDOR_MANAGEMENT_PROMPT, tools, handlers


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, caller: dict = Depends(get_verified_user)):
    openai_client = get_openai_client()
    caller_email = caller.get("email", "")
    caller_user_id = pg_client.get_user_id_by_email(caller_email)
    session_id = req.session_id or str(uuid.uuid4())
    app_context = (req.context or {}).get("app", "vendors")
    logger.info("Chat request from %s (session %s, domain %s)", caller_email, session_id, app_context)

    att_dicts = [a.model_dump() for a in (req.attachments or [])]
    _request_attachments.set(att_dicts)

    today = date.today().isoformat()

    user_messages: list[dict] = []
    for m in req.messages:
        content = m.content
        if m == req.messages[-1] and m.role == "user" and att_dicts:
            summaries = []
            for att in att_dicts:
                lines = att["content"].splitlines()
                cols = lines[0] if lines else ""
                summaries.append(
                    f"[Attached: {att['filename']} — {len(lines) - 1} data rows | columns: {cols}]"
                )
            content = (content or "") + "\n\n" + "\n".join(summaries)
        msg: dict = {"role": m.role}
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
            msg["content"] = m.content or ""
        elif content is not None:
            msg["content"] = content
        if m.tool_calls:
            msg["tool_calls"] = m.tool_calls
        user_messages.append(msg)

    has_csv_attachment = any(
        a.get("filename", "").lower().endswith(".csv") for a in att_dicts
    )
    prompt_body, active_tools, active_handlers = _resolve_domain(app_context, has_csv_attachment)
    system_prompt = f"Today's date is {today}.\n\n{prompt_body}"

    result = run_agent_loop(
        openai_client=openai_client,
        system_prompt=system_prompt,
        messages_in=user_messages,
        tools=active_tools,
        tool_handlers=active_handlers,
        caller_email=caller_email,
    )

    all_msgs: list[dict] = []
    for m in req.messages:
        entry: dict = {"role": m.role, "content": m.content}
        if m.tool_calls:
            entry["tool_calls"] = m.tool_calls
        if m.tool_call_id:
            entry["tool_call_id"] = m.tool_call_id
        all_msgs.append(entry)
    all_msgs.extend(result.tool_messages)
    all_msgs.append({"role": "assistant", "content": result.reply})
    if caller_user_id:
        try:
            pg_client.upsert_chat_session(session_id, caller_user_id, app_context, all_msgs)
        except Exception:
            logger.exception("Failed to persist chat session %s", session_id)

    return ChatResponse(
        reply=result.reply,
        tool_calls_executed=result.tool_calls_executed,
        pending_actions=result.pending_actions,
        disambiguation=result.disambiguation,
        downloads=result.downloads,
        tables=result.tables,
        session_id=session_id,
        tool_messages=result.tool_messages,
    )


class FeedbackRequest(BaseModel):
    session_id: str
    message_seq: int
    signal: bool
    comment: str | None = None


@app.post("/feedback")
def submit_feedback(req: FeedbackRequest, caller: dict = Depends(get_verified_user)):
    caller_email = caller.get("email", "")
    caller_user_id = pg_client.get_user_id_by_email(caller_email)
    if not caller_user_id:
        raise HTTPException(status_code=403, detail="User not found")
    session = pg_client.get_chat_session(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")
    stored_messages = session.get("messages", [])
    if req.message_seq < 0 or req.message_seq >= len(stored_messages):
        raise HTTPException(
            status_code=400,
            detail=f"message_seq {req.message_seq} out of range (session has {len(stored_messages)} messages)",
        )
    pg_client.upsert_feedback(
        chat_session_id=req.session_id,
        message_seq=req.message_seq,
        user_id=caller_user_id,
        signal=req.signal,
        comment=req.comment,
    )
    return {"ok": True}


class SiteFeedbackRequest(BaseModel):
    app_id: str
    open_panes: dict | None = None
    feedback_text: str


@app.post("/feedback/site", status_code=201)
def submit_site_feedback(
    req: SiteFeedbackRequest, caller: dict = Depends(get_verified_user)
):
    caller_email = caller.get("email", "")
    caller_user_id = pg_client.get_user_id_by_email(caller_email)
    if not caller_user_id:
        raise HTTPException(status_code=403, detail="User not found")
    pg_client.insert_site_feedback(
        user_id=caller_user_id,
        app_id=req.app_id,
        open_panes=req.open_panes,
        feedback_text=req.feedback_text,
    )
    return {"ok": True}
