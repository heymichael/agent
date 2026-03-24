"""FastAPI agent service — chat endpoint with OpenAI tool-calling."""

import json
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

load_dotenv()
from pydantic import BaseModel
from openai import OpenAI

from .prompts import VENDOR_AGENT_SYSTEM_PROMPT
from .tools import TOOL_DEFINITIONS, TOOL_HANDLERS
from . import firestore_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Haderach Agent Service", root_path="/agent/api")

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


class ChatResponse(BaseModel):
    reply: str
    tool_calls_executed: list[str]
    pending_action: PendingAction | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.delete("/vendors/{vendor_id}")
def delete_vendor(vendor_id: str):
    deleted = firestore_client.delete_vendor(vendor_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Vendor '{vendor_id}' not found")
    return {"ok": True, "deleted": vendor_id}


@app.get("/users")
def list_users(role: str):
    return firestore_client.list_users_by_role(role)


@app.patch("/vendors/{vendor_id}")
def update_vendor(vendor_id: str, updates: dict):
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        result = firestore_client.update_vendor(vendor_id, updates)
        return {"ok": True, "vendor": result}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    openai_client = get_openai_client()

    messages = [{"role": "system", "content": VENDOR_AGENT_SYSTEM_PROMPT}]
    for m in req.messages:
        messages.append({"role": m.role, "content": m.content})

    tool_calls_executed: list[str] = []
    pending_action: PendingAction | None = None
    max_rounds = 5

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
                    result = handler(fn_args)

                tool_calls_executed.append(fn_name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

                parsed = json.loads(result)
                if parsed.get("action") in ("confirm_delete", "open_edit"):
                    vendor = parsed["vendor"]
                    pending_action = PendingAction(
                        type=parsed["action"],
                        vendor_id=vendor["id"],
                        vendor_name=vendor["name"],
                    )

            continue

        reply = choice.message.content or ""
        return ChatResponse(reply=reply, tool_calls_executed=tool_calls_executed, pending_action=pending_action)

    return ChatResponse(
        reply="I hit the maximum number of tool-call rounds. Please try again.",
        tool_calls_executed=tool_calls_executed,
        pending_action=pending_action,
    )
