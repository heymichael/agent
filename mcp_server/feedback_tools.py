"""Feedback review tool handlers — read feedback and chat sessions from Postgres.

Each handler accepts a dict of parameters and returns a dict with a ``status``
field (``ok``, ``not_found``, or ``error``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from service.pg_client import (
    get_chat_session_detail,
    list_agent_feedback as pg_list_agent_feedback,
    list_site_feedback_entries as pg_list_site_feedback,
    get_agent_feedback_by_id,
    get_site_feedback_by_id,
    list_chat_sessions_summary as pg_list_chat_sessions,
    count_uncollected_feedback as pg_count_uncollected,
    mark_feedback_collected as pg_mark_collected,
)


def _serialize(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "hex"):
        return str(obj)
    return obj


def _clean(row: dict) -> dict:
    return {k: _serialize(v) for k, v in row.items()}


# -- Tool handlers -----------------------------------------------------------


def handle_list_agent_feedback(params: dict) -> dict:
    rows = pg_list_agent_feedback(
        uncollected_only=params.get("uncollected_only", True),
        limit=params.get("limit", 20),
        since=params.get("since"),
    )
    return {"status": "ok", "count": len(rows), "items": [_clean(r) for r in rows]}


def handle_list_site_feedback(params: dict) -> dict:
    rows = pg_list_site_feedback(
        uncollected_only=params.get("uncollected_only", True),
        limit=params.get("limit", 20),
        since=params.get("since"),
        app_id=params.get("app_id"),
    )
    return {"status": "ok", "count": len(rows), "items": [_clean(r) for r in rows]}


def handle_get_agent_feedback_with_context(params: dict) -> dict:
    feedback_id = params.get("feedback_id")
    if not feedback_id:
        return {"status": "error", "message": "feedback_id is required"}

    fb = get_agent_feedback_by_id(feedback_id)
    if not fb:
        return {"status": "not_found", "message": f"No agent feedback with id {feedback_id}"}

    session = None
    if fb.get("chat_session_id"):
        session = get_chat_session_detail(str(fb["chat_session_id"]))

    result: dict[str, Any] = {
        "status": "ok",
        "feedback": _clean(fb),
        "chat_session": None,
    }

    if session:
        cleaned = _clean(session)
        messages = cleaned.get("messages", [])
        msg_seq = fb.get("message_seq")
        if msg_seq is not None and isinstance(messages, list):
            for i, msg in enumerate(messages):
                if isinstance(msg, dict):
                    msg["_is_rated"] = i == msg_seq
        cleaned["messages"] = messages
        result["chat_session"] = cleaned

    return result


def handle_get_site_feedback(params: dict) -> dict:
    feedback_id = params.get("feedback_id")
    if not feedback_id:
        return {"status": "error", "message": "feedback_id is required"}

    fb = get_site_feedback_by_id(feedback_id)
    if not fb:
        return {"status": "not_found", "message": f"No site feedback with id {feedback_id}"}

    return {"status": "ok", "feedback": _clean(fb)}


def handle_get_chat_session(params: dict) -> dict:
    session_id = params.get("session_id")
    if not session_id:
        return {"status": "error", "message": "session_id is required"}

    session = get_chat_session_detail(session_id)
    if not session:
        return {"status": "not_found", "message": f"No chat session with id {session_id}"}

    return {"status": "ok", "session": _clean(session)}


def handle_list_chat_sessions(params: dict) -> dict:
    rows = pg_list_chat_sessions(
        limit=params.get("limit", 20),
        since=params.get("since"),
        app_context=params.get("app_context"),
        user_email=params.get("user_email"),
    )
    return {"status": "ok", "count": len(rows), "items": [_clean(r) for r in rows]}


def handle_count_uncollected(params: dict) -> dict:
    counts = pg_count_uncollected()
    return {"status": "ok", **counts}


def handle_mark_collected(params: dict) -> dict:
    fb_type = params.get("type")
    ids = params.get("ids", [])
    if fb_type not in ("agent", "site"):
        return {"status": "error", "message": "type must be 'agent' or 'site'"}
    if not ids:
        return {"status": "error", "message": "ids list is required"}

    updated = pg_mark_collected(fb_type, ids)
    return {"status": "ok", "updated": updated}
