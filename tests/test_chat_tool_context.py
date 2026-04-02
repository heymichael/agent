"""Tests for tool-call history round-trip in /chat endpoint.

Verifies that tool_call and tool_result messages are:
1. Returned in the response as `tool_messages`
2. Accepted from the frontend and threaded into the OpenAI messages array
3. Persisted in the chat session alongside regular messages
"""

from unittest.mock import patch, MagicMock, call

import json
import pytest
from fastapi.testclient import TestClient

from service.app import app, get_verified_user
from service import pg_client


def _make_client(email="test@example.com"):
    app.dependency_overrides[get_verified_user] = lambda: {"email": email}
    return TestClient(app)


def _teardown():
    app.dependency_overrides.clear()


def _mock_openai_tool_then_text(tool_name, tool_args, tool_call_id, final_text):
    """Build a mock OpenAI client that first returns a tool_call, then a text reply.

    Simulates a two-round tool loop: round 1 triggers a tool call,
    round 2 returns the final text reply.
    """
    mock_client = MagicMock()

    tc = MagicMock()
    tc.id = tool_call_id
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(tool_args)

    tool_choice = MagicMock()
    tool_choice.finish_reason = "tool_calls"
    tool_choice.message.tool_calls = [tc]
    tool_choice.message.model_dump.return_value = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
            }
        ],
    }

    text_choice = MagicMock()
    text_choice.finish_reason = "stop"
    text_choice.message.tool_calls = None
    text_choice.message.content = final_text

    tool_resp = MagicMock()
    tool_resp.choices = [tool_choice]
    text_resp = MagicMock()
    text_resp.choices = [text_choice]

    mock_client.chat.completions.create.side_effect = [tool_resp, text_resp]
    return mock_client


TOOL_RESULT = json.dumps({"ok": True, "count": 14})


class TestToolMessagesReturned:
    """Verify tool_messages appear in the /chat response when tools are called."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "upsert_chat_session")
    @patch.object(pg_client, "get_user_id_by_email", return_value="uid-1")
    @patch("service.app.get_openai_client")
    @patch("service.app.TOOL_HANDLERS", {"vendor_count": lambda args, caller_email: TOOL_RESULT})
    def test_response_includes_tool_messages(self, mock_get_client, mock_uid, mock_upsert):
        mock_get_client.return_value = _mock_openai_tool_then_text(
            "vendor_count", {"filters": {}}, "call_abc", "There are 14 vendors.",
        )
        client = _make_client()

        resp = client.post("/chat", json={
            "messages": [{"role": "user", "content": "How many vendors?"}],
            "context": {"app": "vendors"},
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["reply"] == "There are 14 vendors."
        assert len(data["tool_messages"]) == 2

        assistant_tm = data["tool_messages"][0]
        assert assistant_tm["role"] == "assistant"
        assert assistant_tm["tool_calls"][0]["id"] == "call_abc"
        assert assistant_tm["tool_calls"][0]["function"]["name"] == "vendor_count"

        tool_result_tm = data["tool_messages"][1]
        assert tool_result_tm["role"] == "tool"
        assert tool_result_tm["tool_call_id"] == "call_abc"
        result_content = json.loads(tool_result_tm["content"])
        assert result_content["count"] == 14

    @patch.object(pg_client, "upsert_chat_session")
    @patch.object(pg_client, "get_user_id_by_email", return_value="uid-1")
    @patch("service.app.get_openai_client")
    def test_no_tool_messages_when_no_tools_called(self, mock_get_client, mock_uid, mock_upsert):
        """When the model replies directly, tool_messages should be empty."""
        text_choice = MagicMock()
        text_choice.finish_reason = "stop"
        text_choice.message.tool_calls = None
        text_choice.message.content = "Hello!"
        text_resp = MagicMock()
        text_resp.choices = [text_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = text_resp
        mock_get_client.return_value = mock_client

        client = _make_client()
        resp = client.post("/chat", json={
            "messages": [{"role": "user", "content": "Hi"}],
            "context": {"app": "vendors"},
        })

        assert resp.status_code == 200
        assert resp.json()["tool_messages"] == []


class TestToolMessagesReplayed:
    """Verify that incoming tool messages are threaded into the OpenAI messages array."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "upsert_chat_session")
    @patch.object(pg_client, "get_user_id_by_email", return_value="uid-1")
    @patch("service.app.get_openai_client")
    def test_tool_fields_passed_to_openai(self, mock_get_client, mock_uid, mock_upsert):
        """When the frontend replays tool messages, they appear in the OpenAI call."""
        text_choice = MagicMock()
        text_choice.finish_reason = "stop"
        text_choice.message.tool_calls = None
        text_choice.message.content = "Here are the 14 vendors."
        text_resp = MagicMock()
        text_resp.choices = [text_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = text_resp
        mock_get_client.return_value = mock_client

        client = _make_client()
        resp = client.post("/chat", json={
            "messages": [
                {"role": "user", "content": "How many vendors in Engineering?"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "vendor_count",
                                "arguments": '{"filters": {"department": "Engineering"}}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_abc",
                    "content": '{"ok": true, "count": 14}',
                },
                {"role": "assistant", "content": "There are 14 vendors in Engineering."},
                {"role": "user", "content": "Can you give me the list?"},
            ],
            "context": {"app": "vendors"},
        })

        assert resp.status_code == 200

        create_call = mock_client.chat.completions.create.call_args
        openai_messages = create_call.kwargs["messages"]

        assert openai_messages[0]["role"] == "system"

        assert openai_messages[1] == {"role": "user", "content": "How many vendors in Engineering?"}

        assistant_msg = openai_messages[2]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["tool_calls"][0]["id"] == "call_abc"
        assert "content" not in assistant_msg

        tool_msg = openai_messages[3]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_abc"
        assert tool_msg["content"] == '{"ok": true, "count": 14}'

        assert openai_messages[4] == {"role": "assistant", "content": "There are 14 vendors in Engineering."}
        assert openai_messages[5] == {"role": "user", "content": "Can you give me the list?"}


class TestSessionPersistenceWithToolMessages:
    """Verify tool messages are included in persisted session data."""

    def teardown_method(self):
        _teardown()

    @patch.object(pg_client, "upsert_chat_session")
    @patch.object(pg_client, "get_user_id_by_email", return_value="uid-1")
    @patch("service.app.get_openai_client")
    @patch("service.app.TOOL_HANDLERS", {"vendor_count": lambda args, caller_email: TOOL_RESULT})
    def test_persisted_messages_include_tool_history(self, mock_get_client, mock_uid, mock_upsert):
        mock_get_client.return_value = _mock_openai_tool_then_text(
            "vendor_count", {"filters": {}}, "call_xyz", "14 vendors total.",
        )
        client = _make_client()

        resp = client.post("/chat", json={
            "messages": [{"role": "user", "content": "Count vendors"}],
            "context": {"app": "vendors"},
        })

        assert resp.status_code == 200
        mock_upsert.assert_called_once()
        persisted_msgs = mock_upsert.call_args.args[3]

        assert persisted_msgs[0] == {"role": "user", "content": "Count vendors"}

        assert persisted_msgs[1]["role"] == "assistant"
        assert persisted_msgs[1]["tool_calls"][0]["id"] == "call_xyz"

        assert persisted_msgs[2]["role"] == "tool"
        assert persisted_msgs[2]["tool_call_id"] == "call_xyz"

        assert persisted_msgs[3] == {"role": "assistant", "content": "14 vendors total."}

    @patch.object(pg_client, "upsert_chat_session")
    @patch.object(pg_client, "get_user_id_by_email", return_value="uid-1")
    @patch("service.app.get_openai_client")
    def test_replayed_tool_fields_persist_on_next_turn(self, mock_get_client, mock_uid, mock_upsert):
        """Tool fields from prior turns survive a second round-trip."""
        text_choice = MagicMock()
        text_choice.finish_reason = "stop"
        text_choice.message.tool_calls = None
        text_choice.message.content = "Here is the list."
        text_resp = MagicMock()
        text_resp.choices = [text_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = text_resp
        mock_get_client.return_value = mock_client

        client = _make_client()
        resp = client.post("/chat", json={
            "messages": [
                {"role": "user", "content": "How many?"},
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "vendor_count", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": '{"count": 5}'},
                {"role": "assistant", "content": "5 vendors."},
                {"role": "user", "content": "Show me the list"},
            ],
            "context": {"app": "vendors"},
        })

        assert resp.status_code == 200
        persisted_msgs = mock_upsert.call_args.args[3]

        tool_call_entry = next(m for m in persisted_msgs if m.get("tool_calls"))
        assert tool_call_entry["tool_calls"][0]["id"] == "call_1"

        tool_result_entry = next(m for m in persisted_msgs if m.get("tool_call_id"))
        assert tool_result_entry["tool_call_id"] == "call_1"
