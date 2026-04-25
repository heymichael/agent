"""FastAPI agent service — chat endpoint with OpenAI tool-calling."""

import contextvars
import html
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date

from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(interpolate=False)
from pydantic import BaseModel
from openai import OpenAI

from .prompts import (
    EXPENSE_ANALYTICS_PROMPT, VENDOR_MANAGEMENT_PROMPT, build_table_prompt,
    CMS_GUIDE_PROMPT, CMS_EDITING_PROMPT, CMS_SCHEDULING_PROMPT, CMS_ADMIN_PROMPT,
)
from . import tools as tools_module
from .tools import (
    EXPENSE_TOOL_DEFINITIONS, EXPENSE_TOOL_HANDLERS,
    VENDOR_TOOL_DEFINITIONS, VENDOR_TOOL_HANDLERS,
)
from .cms_tools import (
    CMS_EDITING_TOOLS, CMS_EDITING_HANDLERS,
    CMS_SCHEDULING_TOOLS, CMS_SCHEDULING_HANDLERS,
    CMS_ADMIN_TOOLS, CMS_ADMIN_HANDLERS,
)
from . import pg_client
from .auth import get_verified_user, require_app, warm_firebase_public_keys
from .qbo_auth import get_authorization_url, exchange_code_for_tokens

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        pg_client.warm_connection_pool()
        logger.info("Postgres pool warmed")
    except Exception:
        logger.exception("Failed to warm Postgres pool at startup")

    try:
        get_openai_client()
        logger.info("OpenAI client initialized")
    except Exception:
        logger.exception("Failed to initialize OpenAI client at startup")

    try:
        warm_firebase_public_keys()
        logger.info("Firebase public keys warmed")
    except Exception:
        logger.exception("Failed to warm Firebase public keys at startup")

    try:
        from .tools import TableConfig, TABLE_CONFIGS
        _snake_to_camel = {v: k for k, v in _VENDOR_FIELD_MAP.items()}
        _snake_to_camel.update({
            "source_system": "sourceSystem",
            "source_system_id": "sourceSystemId",
            "created_at": "createdAt",
            "modified_at": "modifiedAt",
            "synced_at": "lastSyncedAt",
            "secondary_owner": "secondaryOwner",
        })
        TABLE_CONFIGS["vendors"] = TableConfig.from_table(
            db_table="vendor_display_v",
            camel_map=_snake_to_camel,
            default_columns=["accountType", "department", "owner"],
            column_groups={
                "contract columns": [
                    "contractStartDate", "contractEndDate",
                    "contractLengthMonths", "autoRenew",
                ],
                "payment columns": ["paymentMethod", "billingFrequency"],
                "ownership columns": ["owner", "secondaryOwner", "department"],
                "sync columns": [
                    "sourceSystem", "sourceSystemId", "lastSyncedAt",
                ],
            },
            pinned="name",
        )
        logger.info("TABLE_CONFIGS populated (%d tables)", len(TABLE_CONFIGS))
    except Exception:
        logger.exception("Failed to populate TABLE_CONFIGS at startup")

    try:
        yield
    finally:
        pg_client.close_pool()


app = FastAPI(title="Haderach Agent Service", root_path="/agent/api", lifespan=lifespan)

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


def _require_active_org(caller: dict) -> str:
    """Return the caller's active org slug or raise 400 `Active-Org-Required`.

    Phase 2 leaves `caller["active_org_slug"]` as None only for
    zero-membership users (every other path either auto-defaults the
    single membership or raises in `get_verified_user`). Endpoints that
    operate on org-scoped data must reject None — there is no safe
    default once data scoping is in effect.
    """
    slug = caller.get("active_org_slug")
    if not slug:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "Active-Org-Required",
                "message": "No active org resolved for this caller.",
            },
        )
    return slug


def _resolve_caller_access(caller: dict, org_slug: str) -> set[str] | None:
    """Resolve the caller's effective vendor set for REST endpoint filtering.

    Returns None for finance_admin (full access *within the active org*)
    or a set of allowed vendor IDs for restricted users. Empty set means
    no vendor access. Contractor vendors without an explicit grant are
    excluded.

    The finance_admin bypass returns None — the *endpoint* then queries
    `pg_client.list_vendors(org_slug)` (or equivalent) so the active-org
    filter applies even for finance_admin. The bypass only skips the
    per-user vendor ACL, never the org filter.
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
        org_slug,
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
        api_key_ref = os.getenv("OPENAI_API_KEY")
        if not api_key_ref:
            raise RuntimeError("OPENAI_API_KEY is not set")
        if os.path.isfile(api_key_ref):
            with open(api_key_ref) as f:
                api_key = f.read().strip()
        else:
            api_key = api_key_ref
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
    view_columns: list[str] | None = None
    table_filters: list[dict] | None = None
    table: str | None = None


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
    usage: dict | None = None


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
    token_usage: dict | None = None


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
    token_usage: dict = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "model": MODEL,
    }

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
            return AgentResult(
                reply="Sorry, I encountered an error contacting the AI service.",
                token_usage=token_usage,
            )

        if response.usage:
            token_usage["prompt_tokens"] += response.usage.prompt_tokens
            token_usage["completion_tokens"] += response.usage.completion_tokens
            token_usage["total_tokens"] += response.usage.total_tokens

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
                table_payload = parsed.get("table")
                if isinstance(table_payload, dict):
                    parsed.pop("table")
                else:
                    table_payload = None
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
                elif parsed.get("action") == "set_columns":
                    pending_actions.append(PendingAction(
                        type="set_columns",
                        table=parsed.get("table"),
                        view_columns=parsed.get("view_columns"),
                    ))
                elif parsed.get("action") == "set_filters":
                    pending_actions.append(PendingAction(
                        type="set_filters",
                        table=parsed.get("table"),
                        table_filters=parsed.get("table_filters"),
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
            token_usage=token_usage,
        )

    return AgentResult(
        reply="I hit the maximum number of tool-call rounds. Please try again.",
        tool_calls_executed=tool_calls_executed,
        pending_actions=pending_actions,
        disambiguation=disambiguation,
        downloads=downloads,
        tables=tables,
        tool_messages=tool_messages,
        token_usage=token_usage,
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
    url, state = get_authorization_url(redirect_uri=absolute_redirect)
    response = RedirectResponse(url)
    is_https = base.startswith("https")
    response.set_cookie(
        "qbo_oauth_state", state,
        max_age=600, httponly=True, samesite="lax",
        secure=is_https,
    )
    return response


@app.get("/qbo/callback")
def qbo_callback(
    request: Request,
    code: str = "",
    realmId: str = "",
    state: str = "",
    error: str = "",
):
    """Handle the OAuth2 callback from Intuit and render a friendly result page."""

    def _render_result_page(
        *,
        title: str,
        message: str,
        status_code: int,
        refresh_token: str = "",
        realm_id: str = "",
        cta_href: str = "/integrations/quickbooks/connect",
        cta_label: str = "Return to Haderach",
    ) -> HTMLResponse:
        safe_title = html.escape(title)
        safe_message = html.escape(message)
        safe_realm_id = html.escape(realm_id)
        safe_refresh = html.escape(refresh_token)
        safe_cta_href = html.escape(cta_href)
        safe_cta_label = html.escape(cta_label)
        token_section = (
            "<p><strong>Refresh token (copy once):</strong></p>"
            f"<textarea readonly>{safe_refresh}</textarea>"
            "<p class=\"muted\">"
            "Save this in Secret Manager under VENDOR_QBO_CREDENTIALS "
            "as refresh_token. QuickBooks may rotate this token."
            "</p>"
        ) if safe_refresh else ""
        realm_section = (
            f"<p><strong>realm_id:</strong> <code>{safe_realm_id}</code></p>"
            if safe_realm_id else ""
        )
        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    body {{
      margin: 0;
      font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: #0b0d10;
      color: #f5f7fa;
    }}
    .wrap {{
      max-width: 720px;
      margin: 8vh auto;
      padding: 0 16px;
    }}
    .card {{
      background: #141922;
      border: 1px solid #2a3140;
      border-radius: 12px;
      padding: 20px;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 1.5rem;
    }}
    p {{
      margin: 10px 0;
      line-height: 1.45;
    }}
    textarea {{
      width: 100%;
      min-height: 130px;
      box-sizing: border-box;
      border-radius: 8px;
      border: 1px solid #3a4559;
      background: #0f141d;
      color: #f5f7fa;
      padding: 10px;
      font: 12px ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .muted {{
      color: #aab2c2;
      font-size: 0.95rem;
    }}
    code {{
      font: 12px ui-monospace, SFMono-Regular, Menlo, monospace;
      background: #0f141d;
      border: 1px solid #3a4559;
      border-radius: 6px;
      padding: 2px 6px;
    }}
    a {{
      color: #8cc5ff;
    }}
    .actions {{
      margin-top: 16px;
    }}
    .button {{
      display: inline-block;
      background: #8cc5ff;
      color: #08111f;
      text-decoration: none;
      border-radius: 8px;
      padding: 10px 14px;
      font-weight: 600;
    }}
    .button:hover {{
      background: #a6d4ff;
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="card">
      <h1>{safe_title}</h1>
      <p>{safe_message}</p>
      {realm_section}
      {token_section}
      <p class="muted">
        You can close this tab, or continue in Haderach.
      </p>
      <div class="actions">
        <a class="button" href="{safe_cta_href}">{safe_cta_label}</a>
      </div>
    </section>
  </main>
</body>
</html>
"""
        return HTMLResponse(content=body, status_code=status_code)

    def _clear_state_cookie(resp: HTMLResponse) -> HTMLResponse:
        resp.delete_cookie("qbo_oauth_state")
        return resp

    if error:
        return _clear_state_cookie(_render_result_page(
            title="QuickBooks connection failed",
            message=f"Intuit returned an OAuth error: {error}",
            status_code=400,
            cta_label="Try connecting again",
        ))
    if not code:
        return _clear_state_cookie(_render_result_page(
            title="QuickBooks connection failed",
            message="Missing authorization code in callback URL.",
            status_code=400,
            cta_label="Try connecting again",
        ))

    expected_state = request.cookies.get("qbo_oauth_state", "")
    if not expected_state or not state or expected_state != state:
        return _clear_state_cookie(_render_result_page(
            title="QuickBooks connection failed",
            message="CSRF validation failed — the state parameter does not match. "
                    "Please start the connection flow again.",
            status_code=403,
            cta_label="Try connecting again",
        ))

    redirect_uri = str(app.url_path_for("qbo_callback"))
    base = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    absolute_redirect = f"{base}{redirect_uri}"

    try:
        token_data = exchange_code_for_tokens(code=code, redirect_uri=absolute_redirect)
    except Exception as exc:
        logger.exception("QBO OAuth token exchange failed")
        return _clear_state_cookie(_render_result_page(
            title="QuickBooks connection failed",
            message=f"Token exchange failed: {exc}",
            status_code=502,
            cta_label="Try connecting again",
        ))
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in")
    refresh_expires_in = token_data.get("x_refresh_token_expires_in")

    logger.info(
        "QBO OAuth complete — realmId=%s, access expires in %ss, refresh expires in %ss",
        realmId, expires_in, refresh_expires_in,
    )

    return _clear_state_cookie(_render_result_page(
        title="QuickBooks connected",
        message=(
            "OAuth completed successfully. Copy the refresh token below and "
            "save it in VENDOR_QBO_CREDENTIALS (and update realm_id if needed)."
        ),
        status_code=200,
        refresh_token=refresh_token,
        realm_id=realmId,
    ))


@app.delete("/vendors/{vendor_id}")
def delete_vendor(
    vendor_id: str,
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("vendors")),
):
    vendor = pg_client.get_vendor(vendor_id, org_slug)
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
    pg_client.delete_vendor(vendor_id, org_slug)
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
    org_slug: str = Depends(require_app("system_administration")),
):
    return pg_client.list_users(org_slug, role if role else None)


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
def get_user(
    email: str,
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("system_administration")),
):
    user = pg_client.get_user_in_org(email, org_slug)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{email}' not found")
    return user


@app.post("/users", status_code=201)
def create_user(
    req: CreateUserRequest,
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("system_administration")),
):
    require_admin(caller)
    try:
        return pg_client.create_user(req.email, req.firstName, req.lastName, req.roles, org_slug)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/users/{email}")
def update_user(
    email: str,
    req: UpdateUserRequest,
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("system_administration")),
):
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
            org_slug,
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
def delete_user_endpoint(
    email: str,
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("system_administration")),
):
    require_admin(caller)
    if not pg_client.delete_user(email, org_slug):
        raise HTTPException(status_code=404, detail=f"User '{email}' not found")
    return {"ok": True, "deleted": email}


class UpdateAppRequest(BaseModel):
    label: str | None = None
    granting_roles: list[str] | None = None
    sort_order: int | None = None


@app.get("/apps")
def list_apps(caller: dict = Depends(get_verified_user)):
    org_slug = _require_active_org(caller)
    return pg_client.list_apps(org_slug)


@app.patch("/apps/{app_id}")
def update_app(
    app_id: str,
    req: UpdateAppRequest,
    caller: dict = Depends(get_verified_user),
    _org_slug: str = Depends(require_app("system_administration")),
):
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
def list_vendors(
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("vendors")),
):
    vendors = pg_client.list_vendors(org_slug)
    allowed = _resolve_caller_access(caller, org_slug)
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
def update_vendor(
    vendor_id: str,
    updates: dict,
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("vendors")),
):
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        result = pg_client.update_vendor(vendor_id, _map_vendor_fields(updates), org_slug)
        return {"ok": True, "vendor": result}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class BatchUpdateRequest(BaseModel):
    updates: list[dict]


@app.post("/vendors/batch-update")
def batch_update_vendors(
    req: BatchUpdateRequest,
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("vendors")),
):
    if not req.updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    try:
        count = pg_client.batch_update_vendors(req.updates, org_slug)
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
    org_slug: str = Depends(require_app("vendor_administration")),
):
    email = require_finance_admin(caller)
    actor_id = pg_client.get_user_id_by_email(email)
    if not actor_id:
        raise HTTPException(status_code=403, detail="User not found")
    try:
        vendor = pg_client.set_vendor_is_contractor(vendor_id, req.is_contractor, actor_id, org_slug)
        return {"ok": True, "vendor": vendor}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/vendors/contractors")
def list_contractor_vendors(
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("vendor_administration")),
):
    require_finance_admin(caller)
    return pg_client.list_contractor_vendors(org_slug)


@app.get("/vendors/{vendor_id}/access")
def list_vendor_access(
    vendor_id: str,
    caller: dict = Depends(get_verified_user),
    org_slug: str = Depends(require_app("vendor_administration")),
):
    require_finance_admin(caller)
    vendor = pg_client.get_vendor(vendor_id, org_slug)
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
    org_slug: str = Depends(require_app("vendor_administration")),
):
    email = require_finance_admin(caller)
    actor_id = pg_client.get_user_id_by_email(email)
    if not actor_id:
        raise HTTPException(status_code=403, detail="User not found")
    vendor = pg_client.get_vendor(vendor_id, org_slug)
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
    org_slug: str = Depends(require_app("vendor_administration")),
):
    require_finance_admin(caller)
    vendor = pg_client.get_vendor(vendor_id, org_slug)
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
    org_slug: str = Depends(require_app("vendors")),
):
    allowed = _resolve_caller_access(caller, org_slug)
    if vendor_ids is None or len(vendor_ids) == 0:
        if allowed is not None:
            vendor_ids = list(allowed)
        else:
            vendor_ids = [v["id"] for v in pg_client.list_vendors(org_slug)]
    elif allowed is not None:
        vendor_ids = [v for v in vendor_ids if v in allowed]
    data = pg_client.query_spend_by_vendor_ids(vendor_ids, from_month, to_month, org_slug)
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
    if inner_result.token_usage:
        response["token_usage"] = inner_result.token_usage
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


def _resolve_cms_mode(mode: str, context: dict | None = None) -> tuple[str, list[dict], dict]:
    """Dispatch CMS domain by mode → (prompt, tools, handlers)."""
    if mode == "editing":
        prompt = CMS_EDITING_PROMPT
        ctx = context or {}
        item_id = ctx.get("itemId")
        ct_slug = ctx.get("contentTypeSlug")
        if item_id or ct_slug:
            parts = []
            if item_id:
                parts.append(f"itemId: {item_id}")
            if ct_slug:
                parts.append(f"contentTypeSlug: {ct_slug}")
            prompt += f"\n\n## Current context\n\n{', '.join(parts)}"
        return prompt, CMS_EDITING_TOOLS, CMS_EDITING_HANDLERS
    if mode == "scheduling":
        return CMS_SCHEDULING_PROMPT, CMS_SCHEDULING_TOOLS, CMS_SCHEDULING_HANDLERS
    if mode == "admin":
        return CMS_ADMIN_PROMPT, CMS_ADMIN_TOOLS, CMS_ADMIN_HANDLERS
    # browse, approval, admin-permissions → guide-only (no tools)
    return CMS_GUIDE_PROMPT, [], {}


def _resolve_domain(app_context: str, has_csv: bool, table_view: dict | None = None, context: dict | None = None) -> tuple[str, list[dict], dict]:
    """Return (system_prompt_body, tool_definitions, tool_handlers) for a domain."""
    if app_context == "cms":
        mode = (context or {}).get("mode", "browse")
        return _resolve_cms_mode(mode, context)

    if app_context == "expenses":
        return EXPENSE_ANALYTICS_PROMPT, EXPENSE_TOOL_DEFINITIONS, EXPENSE_TOOL_HANDLERS

    tools = VENDOR_TOOL_DEFINITIONS if has_csv else [
        t for t in VENDOR_TOOL_DEFINITIONS
        if t["function"]["name"] != "process_vendor_csv"
    ]
    handlers = {**VENDOR_TOOL_HANDLERS, "ask_expense_agent": _execute_ask_expense_agent}
    prompt = VENDOR_MANAGEMENT_PROMPT + "\n\n" + build_table_prompt(["vendors"], table_view=table_view)
    return prompt, tools, handlers


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

    # Phase 3 of multi-org tenancy (task 254). Pin the caller's active
    # org slug to a contextvar so every tool handler under run_agent_loop
    # — including the sub-agent invoked via ask_expense_agent — scopes
    # SQL queries to this tenant. Sub-agents run inside the same Python
    # task and inherit the contextvar automatically.
    tools_module.set_caller_org_slug(caller.get("active_org_slug"))

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
    table_view = (req.context or {}).get("tableView")
    prompt_body, active_tools, active_handlers = _resolve_domain(app_context, has_csv_attachment, table_view=table_view, context=req.context)
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
        usage=result.token_usage,
    )


class FeedbackRequest(BaseModel):
    session_id: str
    message_seq: int
    signal: bool
    comment: str | None = None


class CmsItemPatchRequest(BaseModel):
    data: dict | None = None
    slug: str | None = None
    workflow_status: str | None = None
    workflow_comment: str | None = None


class CmsItemCreateRequest(BaseModel):
    contentTypeId: int | str
    data: dict = {}


@app.post("/cms/items")
def create_cms_item(
    req: CmsItemCreateRequest,
    caller: dict = Depends(get_verified_user),
    _org_slug: str = Depends(require_app("site")),
):
    from .cms_tools import handle_cms_create_item
    # Phase 5 task 254: pin caller's active org slug into the tools contextvar so
    # the CMS handler can resolve the Payload org id without orgId being passed.
    tools_module.set_caller_org_slug(_org_slug)
    result = json.loads(handle_cms_create_item({
        "contentTypeId": req.contentTypeId,
        "data": req.data,
    }))
    return result


@app.patch("/cms/items/{item_id}")
def patch_cms_item(
    item_id: int,
    req: CmsItemPatchRequest,
    caller: dict = Depends(get_verified_user),
    _org_slug: str = Depends(require_app("site")),
):
    from .cms_tools import handle_cms_update_item, _api, _headers, _fetch_item
    import httpx
    tools_module.set_caller_org_slug(_org_slug)
    caller_email = caller.get("email", "")

    if req.workflow_status is not None or req.workflow_comment is not None:
        # Phase 5 cross-tenant guard: confirm the item belongs to the caller
        # before applying a workflow transition direct to Payload.
        existing = _fetch_item(item_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Content item {item_id} not found.")
        body: dict = {"_status": "published"}
        if req.workflow_status is not None:
            if req.workflow_status == "draft":
                current_status = existing.get("workflow_status", "draft")
                if current_status != "live":
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "Invalid-Workflow-Transition",
                            "status": "invalid_state",
                            "message": f"Cannot deactivate from state '{current_status}'. Must be live.",
                        },
                    )
                body["workflow_status"] = "draft"
                # Deactivation returns the item to an editable state.
                body["locked_by"] = None
            else:
                body["workflow_status"] = req.workflow_status
        if req.workflow_comment is not None:
            body["workflow_comment"] = req.workflow_comment
        r = httpx.patch(_api(f"/api/content-items/{item_id}"), headers=_headers(), json=body, timeout=10)
        r.raise_for_status()
        return {"status": "ok", "item": r.json().get("doc", r.json())}

    args: dict = {"itemId": item_id}
    if req.data is not None:
        args["data"] = req.data
    if req.slug is not None:
        args["slug"] = req.slug
    result = json.loads(handle_cms_update_item(args, caller_email=caller_email))
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result["message"])
    if result.get("status") == "locked":
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@app.get("/cms/items/{item_id}/versions")
def list_cms_item_versions(
    item_id: int,
    caller: dict = Depends(get_verified_user),
    _org_slug: str = Depends(require_app("site")),
):
    from .cms_tools import _api, _headers, _fetch_item
    import httpx
    tools_module.set_caller_org_slug(_org_slug)
    # Phase 5 cross-tenant guard: confirm the parent item belongs to the
    # caller before exposing its version history.
    if _fetch_item(item_id) is None:
        raise HTTPException(status_code=404, detail=f"Content item {item_id} not found.")
    r = httpx.get(
        _api(f"/api/content-items/versions"),
        headers=_headers(),
        params={"where[parent][equals]": item_id, "sort": "-updatedAt", "limit": 50, "depth": 0},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    docs = [
        {
            "id": d["id"],
            "updatedAt": d.get("updatedAt", ""),
            "data": (d.get("version") or {}).get("data", {}),
            "status": (d.get("version") or {}).get("_status", ""),
            "workflowStatus": (d.get("version") or {}).get("workflow_status", ""),
        }
        for d in data.get("docs", [])
    ]
    return {"docs": docs, "totalDocs": data.get("totalDocs", 0)}


@app.post("/cms/items/{item_id}/versions/{version_id}/restore")
def restore_cms_item_version(
    item_id: int,
    version_id: int,
    caller: dict = Depends(get_verified_user),
    _org_slug: str = Depends(require_app("site")),
):
    from .cms_tools import handle_cms_restore_version
    tools_module.set_caller_org_slug(_org_slug)
    caller_email = caller.get("email", "")
    result = json.loads(handle_cms_restore_version({"itemId": item_id, "versionId": version_id}, caller_email=caller_email))
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("message", "Not found"))
    return result


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
