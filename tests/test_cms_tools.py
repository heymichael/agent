"""Unit tests for CMS tool handlers (service/cms_tools.py).

All Payload REST calls are mocked via unittest.mock.patch on httpx.
No running agent, no Firebase auth, no Postgres required.
Each handler group is self-contained.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from service.cms_tools import (
    handle_cms_get_item,
    handle_cms_create_item,
    handle_cms_update_item,
    handle_cms_submit_for_approval,
    handle_cms_restore_version,
    handle_cms_lock_item,
    handle_cms_unlock_item,
    handle_cms_add_to_schedule,
    handle_cms_create_content_type,
    handle_cms_update_content_type_schema,
    handle_cms_commit_content_type,
    handle_cms_extend_content_type_schema,
)

pytestmark = pytest.mark.cms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resp(status_code: int = 200, body: dict | None = None) -> MagicMock:
    """Return a mock httpx response."""
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body or {}
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return r


# ---------------------------------------------------------------------------
# handle_cms_get_item
# ---------------------------------------------------------------------------


class TestHandleCmsGetItem:
    def test_happy_path_extracts_guidelines_flat_schema(self):
        """Guidelines are extracted from a flat list schema."""
        item = {
            "id": 1,
            "contentType": {
                "schema": [
                    {"name": "title", "type": "text", "guidelines": "Keep it short."},
                    {"name": "body", "type": "richtext"},
                ]
            },
        }
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, item)):
            result = json.loads(handle_cms_get_item({"itemId": 1}))

        assert result["status"] == "ok"
        assert result["item"] == item
        assert result["field_guidelines"] == {"title": "Keep it short."}

    def test_happy_path_extracts_guidelines_dict_schema(self):
        """Schema stored as {'fields': [...]} shape is also handled."""
        item = {
            "id": 2,
            "contentType": {
                "schema": {
                    "fields": [
                        {"name": "summary", "type": "text", "guidelines": "One sentence."},
                    ]
                }
            },
        }
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, item)):
            result = json.loads(handle_cms_get_item({"itemId": 2}))

        assert result["status"] == "ok"
        assert result["field_guidelines"] == {"summary": "One sentence."}

    def test_404_returns_not_found(self):
        """404 from Payload returns {status: not_found} without raising."""
        with patch("service.cms_tools.httpx.get", return_value=_resp(404)):
            result = json.loads(handle_cms_get_item({"itemId": 999}))

        assert result["status"] == "not_found"

    def test_no_guidelines_returns_empty_dict(self):
        """Items with no schema guidelines return an empty field_guidelines dict."""
        item = {"id": 3, "contentType": {"schema": [{"name": "title", "type": "text"}]}}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, item)):
            result = json.loads(handle_cms_get_item({"itemId": 3}))

        assert result["field_guidelines"] == {}


# ---------------------------------------------------------------------------
# handle_cms_create_item
# ---------------------------------------------------------------------------


class TestHandleCmsCreateItem:
    def test_happy_path_includes_required_fields(self):
        """POST body always includes workflow_status: draft and _status: published."""
        doc = {"id": 10, "workflow_status": "draft"}
        with patch("service.cms_tools.httpx.post", return_value=_resp(201, {"doc": doc})) as mock_post:
            result = json.loads(
                handle_cms_create_item({"orgId": 1, "contentTypeId": 2, "data": {"title": "Hello"}})
            )

        assert result["status"] == "ok"
        assert result["item"] == doc
        body = mock_post.call_args.kwargs["json"]
        assert body["workflow_status"] == "draft"
        assert body["_status"] == "published"

    def test_optional_slug_included_when_provided(self):
        """Slug is included in the POST body when passed as an arg."""
        doc = {"id": 11}
        with patch("service.cms_tools.httpx.post", return_value=_resp(201, {"doc": doc})) as mock_post:
            handle_cms_create_item({"orgId": 1, "contentTypeId": 2, "data": {}, "slug": "my-slug"})

        body = mock_post.call_args.kwargs["json"]
        assert body["slug"] == "my-slug"

    def test_slug_omitted_when_not_provided(self):
        """Slug key is absent from the POST body when not passed."""
        doc = {"id": 12}
        with patch("service.cms_tools.httpx.post", return_value=_resp(201, {"doc": doc})) as mock_post:
            handle_cms_create_item({"orgId": 1, "contentTypeId": 2, "data": {}})

        body = mock_post.call_args.kwargs["json"]
        assert "slug" not in body


# ---------------------------------------------------------------------------
# handle_cms_update_item
# ---------------------------------------------------------------------------


class TestHandleCmsUpdateItem:
    def _existing(self, locked_by=None, data=None):
        return {"id": 5, "locked_by": locked_by, "data": data or {"title": "Old"}}

    def test_happy_path_with_data_key(self):
        """PATCH body is {_status, data: args['data']} — does not merge with existing."""
        existing = self._existing()
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": existing})) as mock_patch:
            handle_cms_update_item({"itemId": 5, "data": {"title": "New"}})

        body = mock_patch.call_args.kwargs["json"]
        assert body["_status"] == "published"
        assert body["data"] == {"title": "New"}

    def test_happy_path_without_data_key_merges_with_existing(self):
        """Top-level fields are merged into existing.data and sent under data key."""
        existing = self._existing(data={"title": "Old", "body": "Keep"})
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": existing})) as mock_patch:
            handle_cms_update_item({"itemId": 5, "title": "Updated"})

        body = mock_patch.call_args.kwargs["json"]
        assert body["data"]["title"] == "Updated"
        assert body["data"]["body"] == "Keep"

    def test_locked_by_other_user_returns_locked_no_patch(self):
        """Returns {status: locked} and issues no PATCH when item is locked by someone else."""
        existing = self._existing(locked_by="other@example.com")
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch") as mock_patch:
            result = json.loads(
                handle_cms_update_item({"itemId": 5, "data": {}}, caller_email="me@example.com")
            )

        assert result["status"] == "locked"
        mock_patch.assert_not_called()

    def test_locked_by_null_proceeds_normally(self):
        """Proceeds normally when locked_by is null."""
        existing = self._existing(locked_by=None)
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": existing})) as mock_patch:
            result = json.loads(
                handle_cms_update_item({"itemId": 5, "data": {}}, caller_email="me@example.com")
            )

        assert result["status"] == "ok"
        mock_patch.assert_called_once()

    def test_item_not_found_returns_not_found_no_patch(self):
        """Returns {status: not_found} and issues no PATCH when item is missing."""
        with patch("service.cms_tools.httpx.get", return_value=_resp(404)), \
             patch("service.cms_tools.httpx.patch") as mock_patch:
            result = json.loads(handle_cms_update_item({"itemId": 999, "data": {}}))

        assert result["status"] == "not_found"
        mock_patch.assert_not_called()


# ---------------------------------------------------------------------------
# handle_cms_submit_for_approval
# ---------------------------------------------------------------------------


class TestHandleCmsSubmitForApproval:
    def _item(self, workflow_status):
        return {"id": 7, "workflow_status": workflow_status}

    def test_from_draft_patches_needs_approval(self):
        """Draft → needs_approval: PATCH is issued with correct workflow_status."""
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, self._item("draft"))), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {})) as mock_patch:
            result = json.loads(handle_cms_submit_for_approval({"itemId": 7}))

        assert result["status"] == "ok"
        assert result["workflow_status"] == "needs_approval"
        body = mock_patch.call_args.kwargs["json"]
        assert body["workflow_status"] == "needs_approval"

    def test_from_changes_requested_patches_needs_approval(self):
        """changes_requested → needs_approval also succeeds."""
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, self._item("changes_requested"))), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {})):
            result = json.loads(handle_cms_submit_for_approval({"itemId": 7}))

        assert result["status"] == "ok"

    @pytest.mark.parametrize("state", ["needs_approval", "approved", "live", "scheduled"])
    def test_invalid_states_return_invalid_state_no_patch(self, state):
        """Returns {status: invalid_state} for states that cannot be submitted."""
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, self._item(state))), \
             patch("service.cms_tools.httpx.patch") as mock_patch:
            result = json.loads(handle_cms_submit_for_approval({"itemId": 7}))

        assert result["status"] == "invalid_state"
        mock_patch.assert_not_called()

    def test_item_not_found(self):
        with patch("service.cms_tools.httpx.get", return_value=_resp(404)):
            result = json.loads(handle_cms_submit_for_approval({"itemId": 999}))

        assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# handle_cms_restore_version
# ---------------------------------------------------------------------------


class TestHandleCmsRestoreVersion:
    def test_restore_posts_to_version_url_then_republishes(self):
        """POST goes to /versions/{versionId}, then PATCH re-publishes, then GET fetches."""
        restored_item = {"id": 3, "_status": "published"}
        calls = []

        def mock_post(url, **kw):
            calls.append(("post", url))
            return _resp(200, {})

        def mock_patch(url, **kw):
            calls.append(("patch", url))
            return _resp(200, {})

        def mock_get(url, **kw):
            calls.append(("get", url))
            return _resp(200, restored_item)

        with patch("service.cms_tools.httpx.post", side_effect=mock_post), \
             patch("service.cms_tools.httpx.patch", side_effect=mock_patch), \
             patch("service.cms_tools.httpx.get", side_effect=mock_get):
            result = json.loads(handle_cms_restore_version({"itemId": 3, "versionId": 42}))

        assert result["status"] == "ok"
        assert result["item"] == restored_item

        # POST must go to the version URL
        post_url = calls[0][1]
        assert "versions/42" in post_url

        # PATCH must re-publish the item
        patch_url = calls[1][1]
        assert "content-items/3" in patch_url

        # Final GET fetches the updated item
        get_url = calls[2][1]
        assert "content-items/3" in get_url


# ---------------------------------------------------------------------------
# handle_cms_lock_item
# ---------------------------------------------------------------------------


class TestHandleCmsLockItem:
    def test_unlocked_item_patches_locked_by_caller(self):
        """Unlocked item: PATCH sets locked_by to caller_email."""
        existing = {"id": 1, "locked_by": None}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": existing})) as mock_patch:
            result = json.loads(handle_cms_lock_item({"itemId": 1}, caller_email="me@example.com"))

        assert result["status"] == "ok"
        body = mock_patch.call_args.kwargs["json"]
        assert body["locked_by"] == "me@example.com"

    def test_already_locked_by_caller_is_idempotent(self):
        """Already locked by the caller: returns ok with message, no PATCH issued."""
        existing = {"id": 1, "locked_by": "me@example.com"}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch") as mock_patch:
            result = json.loads(handle_cms_lock_item({"itemId": 1}, caller_email="me@example.com"))

        assert result["status"] == "ok"
        assert "Already locked by you" in result["message"]
        mock_patch.assert_not_called()

    def test_locked_by_different_user_returns_locked_no_patch(self):
        """Locked by another user: returns {status: locked}, no PATCH issued."""
        existing = {"id": 1, "locked_by": "other@example.com"}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch") as mock_patch:
            result = json.loads(handle_cms_lock_item({"itemId": 1}, caller_email="me@example.com"))

        assert result["status"] == "locked"
        mock_patch.assert_not_called()


# ---------------------------------------------------------------------------
# handle_cms_unlock_item
# ---------------------------------------------------------------------------


class TestHandleCmsUnlockItem:
    def test_locked_by_caller_patches_null(self):
        """Caller unlocks their own lock: PATCH sets locked_by to null."""
        existing = {"id": 2, "locked_by": "me@example.com"}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": existing})) as mock_patch:
            result = json.loads(handle_cms_unlock_item({"itemId": 2}, caller_email="me@example.com"))

        assert result["status"] == "ok"
        body = mock_patch.call_args.kwargs["json"]
        assert body["locked_by"] is None

    def test_locked_by_different_user_returns_error_no_patch(self):
        """Locked by another user: returns {status: error}, no PATCH issued."""
        existing = {"id": 2, "locked_by": "other@example.com"}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch") as mock_patch:
            result = json.loads(handle_cms_unlock_item({"itemId": 2}, caller_email="me@example.com"))

        assert result["status"] == "error"
        mock_patch.assert_not_called()

    def test_already_unlocked_patches_null(self):
        """Already unlocked (locked_by null): PATCH still clears it — no guard needed."""
        existing = {"id": 2, "locked_by": None}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, existing)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": existing})) as mock_patch:
            result = json.loads(handle_cms_unlock_item({"itemId": 2}, caller_email="me@example.com"))

        assert result["status"] == "ok"
        mock_patch.assert_called_once()


# ---------------------------------------------------------------------------
# handle_cms_add_to_schedule
# ---------------------------------------------------------------------------


class TestHandleCmsAddToSchedule:
    def _args(self):
        return {"scheduleName": "Q2 Launch", "publishAt": "2026-06-01T09:00:00Z", "contentTypeId": 5}

    def test_schedule_not_found_creates_new(self):
        """No existing schedule → POST creates a new one with all fields."""
        new_schedule = {"id": 10, "name": "Q2 Launch", "contentTypes": [5]}
        search_resp = _resp(200, {"docs": []})
        post_resp = _resp(201, {"doc": new_schedule})

        with patch("service.cms_tools.httpx.get", return_value=search_resp), \
             patch("service.cms_tools.httpx.post", return_value=post_resp) as mock_post:
            result = json.loads(handle_cms_add_to_schedule(self._args()))

        assert result["status"] == "ok"
        body = mock_post.call_args.kwargs["json"]
        assert body["name"] == "Q2 Launch"
        assert body["contentTypes"] == [5]

    def test_schedule_found_ct_not_in_list_appends(self):
        """Existing schedule without this contentTypeId: PATCH appends it."""
        existing = {"id": 10, "contentTypes": [3]}
        search_resp = _resp(200, {"docs": [existing]})
        patch_resp = _resp(200, {"doc": {**existing, "contentTypes": [3, 5]}})

        with patch("service.cms_tools.httpx.get", return_value=search_resp), \
             patch("service.cms_tools.httpx.patch", return_value=patch_resp) as mock_patch:
            result = json.loads(handle_cms_add_to_schedule(self._args()))

        assert result["status"] == "ok"
        body = mock_patch.call_args.kwargs["json"]
        assert 5 in body["contentTypes"]
        assert 3 in body["contentTypes"]

    def test_schedule_found_ct_already_in_list_no_duplicate(self):
        """contentTypeId already present: PATCH does not duplicate it."""
        existing = {"id": 10, "contentTypes": [5]}
        search_resp = _resp(200, {"docs": [existing]})
        patch_resp = _resp(200, {"doc": existing})

        with patch("service.cms_tools.httpx.get", return_value=search_resp), \
             patch("service.cms_tools.httpx.patch", return_value=patch_resp) as mock_patch:
            result = json.loads(handle_cms_add_to_schedule(self._args()))

        assert result["status"] == "ok"
        body = mock_patch.call_args.kwargs["json"]
        assert body["contentTypes"].count(5) == 1
        assert body["publishAt"] == "2026-06-01T09:00:00Z"

    def test_schedule_found_ct_as_dict_in_list(self):
        """contentTypes list may contain dicts with 'id' key (Payload depth=1 response)."""
        existing = {"id": 10, "contentTypes": [{"id": 5, "name": "Job Listings"}]}
        search_resp = _resp(200, {"docs": [existing]})
        patch_resp = _resp(200, {"doc": existing})

        with patch("service.cms_tools.httpx.get", return_value=search_resp), \
             patch("service.cms_tools.httpx.patch", return_value=patch_resp) as mock_patch:
            result = json.loads(handle_cms_add_to_schedule(self._args()))

        assert result["status"] == "ok"
        # Should not duplicate — existing_ids derived from dict["id"]
        body = mock_patch.call_args.kwargs["json"]
        assert body["contentTypes"].count(5) == 1


# ---------------------------------------------------------------------------
# handle_cms_create_content_type
# ---------------------------------------------------------------------------


class TestHandleCmsCreateContentType:
    def test_happy_path_includes_draft_status(self):
        """POST body always includes status: draft."""
        doc = {"id": 20, "name": "Articles", "slug": "articles", "status": "draft"}
        with patch("service.cms_tools.httpx.post", return_value=_resp(201, {"doc": doc})) as mock_post:
            result = json.loads(
                handle_cms_create_content_type({"orgId": 1, "name": "Articles"})
            )

        assert result["status"] == "ok"
        assert result["contentType"] == doc
        assert result["slug"] == "articles"
        body = mock_post.call_args.kwargs["json"]
        assert body["status"] == "draft"

    def test_optional_slug_and_schema_included(self):
        """Slug and schema are included when provided."""
        doc = {"id": 21, "slug": "articles", "status": "draft"}
        schema = [{"name": "title", "type": "text"}]
        with patch("service.cms_tools.httpx.post", return_value=_resp(201, {"doc": doc})) as mock_post:
            handle_cms_create_content_type({"orgId": 1, "name": "Articles", "slug": "articles", "schema": schema})

        body = mock_post.call_args.kwargs["json"]
        assert body["slug"] == "articles"
        assert body["schema"] == schema

    def test_schema_defaults_to_empty_list(self):
        """Schema defaults to [] when not provided."""
        doc = {"id": 22, "status": "draft"}
        with patch("service.cms_tools.httpx.post", return_value=_resp(201, {"doc": doc})) as mock_post:
            handle_cms_create_content_type({"orgId": 1, "name": "Articles"})

        body = mock_post.call_args.kwargs["json"]
        assert body["schema"] == []


# ---------------------------------------------------------------------------
# handle_cms_update_content_type_schema
# ---------------------------------------------------------------------------


class TestHandleCmsUpdateContentTypeSchema:
    def test_draft_ct_patches_schema(self):
        """Draft content type: PATCH sends full replacement schema."""
        ct = {"id": 30, "status": "draft", "schema": []}
        new_schema = [{"name": "title", "type": "text"}]
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, ct)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": ct})) as mock_patch:
            result = json.loads(
                handle_cms_update_content_type_schema({"contentTypeId": 30, "schema": new_schema})
            )

        assert result["status"] == "ok"
        body = mock_patch.call_args.kwargs["json"]
        assert body["schema"] == new_schema

    def test_committed_ct_returns_error_no_patch(self):
        """Committed content type: returns {status: error}, no PATCH issued."""
        ct = {"id": 31, "status": "committed"}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, ct)), \
             patch("service.cms_tools.httpx.patch") as mock_patch:
            result = json.loads(
                handle_cms_update_content_type_schema({"contentTypeId": 31, "schema": []})
            )

        assert result["status"] == "error"
        mock_patch.assert_not_called()


# ---------------------------------------------------------------------------
# handle_cms_commit_content_type
# ---------------------------------------------------------------------------


class TestHandleCmsCommitContentType:
    def test_draft_no_proposed_fields_commits(self):
        """Draft with no proposed_fields: PATCH sets status committed, schema unchanged."""
        ct = {"id": 40, "status": "draft", "schema": [{"name": "title"}], "proposed_fields": None}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, ct)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": ct})) as mock_patch:
            result = json.loads(handle_cms_commit_content_type({"contentTypeId": 40}))

        assert result["status"] == "ok"
        body = mock_patch.call_args.kwargs["json"]
        assert body["status"] == "committed"
        assert body["schema"] == [{"name": "title"}]
        assert body["proposed_fields"] is None

    def test_draft_with_proposed_fields_merges_then_commits(self):
        """Draft with proposed_fields: merged schema is sent, proposed_fields cleared."""
        existing_schema = [{"name": "title"}]
        proposed = [{"name": "summary"}]
        ct = {"id": 41, "status": "draft", "schema": existing_schema, "proposed_fields": proposed}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, ct)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": ct})) as mock_patch:
            handle_cms_commit_content_type({"contentTypeId": 41})

        body = mock_patch.call_args.kwargs["json"]
        assert body["schema"] == existing_schema + proposed
        assert body["proposed_fields"] is None

    def test_already_committed_is_idempotent(self):
        """Already committed: returns ok with message, no PATCH issued."""
        ct = {"id": 42, "status": "committed"}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, ct)), \
             patch("service.cms_tools.httpx.patch") as mock_patch:
            result = json.loads(handle_cms_commit_content_type({"contentTypeId": 42}))

        assert result["status"] == "ok"
        assert "Already committed" in result["message"]
        mock_patch.assert_not_called()


# ---------------------------------------------------------------------------
# handle_cms_extend_content_type_schema
# ---------------------------------------------------------------------------


class TestHandleCmsExtendContentTypeSchema:
    def test_committed_ct_patches_proposed_fields_only(self):
        """Committed CT: PATCH sets proposed_fields only; committed schema not touched."""
        ct = {"id": 50, "status": "committed", "schema": [{"name": "title"}]}
        proposed = [{"name": "new_field"}]
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, ct)), \
             patch("service.cms_tools.httpx.patch", return_value=_resp(200, {"doc": ct})) as mock_patch:
            result = json.loads(
                handle_cms_extend_content_type_schema({"contentTypeId": 50, "proposedFields": proposed})
            )

        assert result["status"] == "ok"
        body = mock_patch.call_args.kwargs["json"]
        assert body == {"proposed_fields": proposed}
        assert "schema" not in body

    def test_draft_ct_returns_error_no_patch(self):
        """Draft CT: returns {status: error}, no PATCH issued."""
        ct = {"id": 51, "status": "draft"}
        with patch("service.cms_tools.httpx.get", return_value=_resp(200, ct)), \
             patch("service.cms_tools.httpx.patch") as mock_patch:
            result = json.loads(
                handle_cms_extend_content_type_schema({"contentTypeId": 51, "proposedFields": []})
            )

        assert result["status"] == "error"
        mock_patch.assert_not_called()
