"""MCP protocol entry point for the feedback review server.

Run with:
    python -c "from dotenv import load_dotenv; load_dotenv(interpolate=False); \
               from mcp_server.feedback_server import main; main()"

Exposes feedback review tools over stdio transport for consumption
by Cursor or any MCP-compatible client.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .feedback_tools import (
    handle_list_agent_feedback,
    handle_list_site_feedback,
    handle_get_agent_feedback_with_context,
    handle_get_site_feedback,
    handle_get_chat_session,
    handle_list_chat_sessions,
    handle_count_uncollected,
    handle_mark_collected,
)

mcp = FastMCP("haderach-feedback-review")


@mcp.tool()
async def list_agent_feedback(
    uncollected_only: bool = True,
    limit: int = 20,
    since: str | None = None,
) -> dict:
    """List recent agent feedback entries (thumbs up/down on chat messages).

    Args:
        uncollected_only: Only show feedback not yet marked as collected.
            Defaults to true.
        limit: Maximum number of entries to return. Defaults to 20.
        since: ISO date string — only return feedback created on or after
            this date (e.g. "2026-04-01").
    """
    return handle_list_agent_feedback({
        "uncollected_only": uncollected_only,
        "limit": limit,
        "since": since,
    })


@mcp.tool()
async def list_site_feedback(
    uncollected_only: bool = True,
    limit: int = 20,
    since: str | None = None,
    app_id: str | None = None,
) -> dict:
    """List recent site feedback entries (free-text feedback from any app).

    Args:
        uncollected_only: Only show feedback not yet marked as collected.
            Defaults to true.
        limit: Maximum number of entries to return. Defaults to 20.
        since: ISO date string — only return feedback created on or after
            this date.
        app_id: Filter to feedback from a specific app (e.g. "vendors").
    """
    return handle_list_site_feedback({
        "uncollected_only": uncollected_only,
        "limit": limit,
        "since": since,
        "app_id": app_id,
    })


@mcp.tool()
async def get_agent_feedback_with_context(feedback_id: str) -> dict:
    """Retrieve a single agent feedback entry with its full chat session.

    Returns the feedback (signal, user comment, message_seq) plus the
    complete chat session (app_context, user email, full messages array)
    with the rated message highlighted via an ``_is_rated`` flag. One call
    gives the complete picture for reviewing a thumbs-down.

    Args:
        feedback_id: UUID of the agent_feedback row.
    """
    return handle_get_agent_feedback_with_context({"feedback_id": feedback_id})


@mcp.tool()
async def get_site_feedback(feedback_id: str) -> dict:
    """Retrieve a single site feedback entry by ID.

    Returns user email, app_id, feedback_text, open_panes snapshot,
    created_at, and collected status.

    Args:
        feedback_id: UUID of the site_feedback row.
    """
    return handle_get_site_feedback({"feedback_id": feedback_id})


@mcp.tool()
async def get_chat_session(session_id: str) -> dict:
    """Retrieve a single chat session by ID.

    Returns app_context, created_at, user email, and the full messages
    array. Useful for browsing sessions independently of feedback.

    Args:
        session_id: UUID of the chat_sessions row.
    """
    return handle_get_chat_session({"session_id": session_id})


@mcp.tool()
async def list_chat_sessions(
    limit: int = 20,
    since: str | None = None,
    app_context: str | None = None,
    user_email: str | None = None,
) -> dict:
    """List recent chat sessions with summary info.

    Args:
        limit: Maximum number of sessions to return. Defaults to 20.
        since: ISO date string — only return sessions created on or after
            this date.
        app_context: Filter to sessions from a specific app context
            (e.g. "vendors", "expenses").
        user_email: Filter to sessions from a specific user.
    """
    return handle_list_chat_sessions({
        "limit": limit,
        "since": since,
        "app_context": app_context,
        "user_email": user_email,
    })


@mcp.tool()
async def count_uncollected_feedback() -> dict:
    """Count uncollected feedback by type.

    Returns ``{ "agent": <int>, "site": <int> }``. Quick triage check to
    see if there's anything new to review.
    """
    return handle_count_uncollected({})


@mcp.tool()
async def mark_feedback_collected(
    type: str,
    ids: list[str],
) -> dict:
    """Mark one or more feedback items as collected (reviewed).

    Updates the ``collected`` flag so reviewed items don't resurface in
    uncollected-only queries.

    Args:
        type: Feedback type — "agent" or "site".
        ids: List of feedback UUIDs to mark as collected.
    """
    return handle_mark_collected({"type": type, "ids": ids})


def main() -> None:
    mcp.run(transport="stdio")
