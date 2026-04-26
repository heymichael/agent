"""Unit tests for CMS admin/schema-design tools (task 290).

Tests field validation, guidelines auto-generation, required defaults,
and the reduced admin tool set.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from service import tools as tools_module
from service import cms_tools
from service.cms_tools import (
    _validate_field,
    _validate_schema,
    _ensure_richtext_guidelines,
    _apply_required_defaults,
    V1_FIELD_TYPES,
    FUTURE_FIELD_TYPES,
    CMS_ADMIN_TOOLS,
    CMS_TOOL_DEFINITIONS,
    handle_cms_create_content_type,
    handle_cms_update_content_type_schema,
    handle_cms_set_active_content_type,
    set_active_content_type_id,
    get_active_content_type_id,
)

pytestmark = pytest.mark.cms

CALLER_SLUG = "haderach"
CALLER_ORG_ID = 1


@pytest.fixture(autouse=True)
def _seed_cms_caller():
    """Pin caller org to haderach and pre-seed the resolver cache."""
    tools_module.set_caller_org_slug(CALLER_SLUG)
    cms_tools._org_id_cache[CALLER_SLUG] = CALLER_ORG_ID
    cms_tools.set_active_content_type_id(None)
    yield
    tools_module.set_caller_org_slug(None)
    cms_tools._clear_org_id_cache()
    cms_tools.set_active_content_type_id(None)


class TestFieldValidation:
    """Field-level validation for V1 field types."""

    def test_valid_text_field(self):
        """Text fields pass validation with name and type."""
        assert _validate_field({"name": "Title", "type": "text"}) is None

    def test_valid_richtext_field(self):
        """Richtext fields pass validation."""
        assert _validate_field({"name": "Body", "type": "richtext"}) is None

    def test_valid_number_field(self):
        """Number fields pass validation."""
        assert _validate_field({"name": "Count", "type": "number"}) is None

    def test_valid_date_field(self):
        """Date fields pass validation."""
        assert _validate_field({"name": "PublishDate", "type": "date"}) is None

    def test_valid_boolean_field(self):
        """Boolean fields pass validation."""
        assert _validate_field({"name": "Featured", "type": "boolean"}) is None

    def test_valid_url_field(self):
        """URL fields pass validation."""
        assert _validate_field({"name": "Website", "type": "url"}) is None

    def test_valid_email_field(self):
        """Email fields pass validation."""
        assert _validate_field({"name": "Contact", "type": "email"}) is None

    def test_valid_select_with_options(self):
        """Select fields pass validation when options array is present."""
        assert _validate_field({"name": "Status", "type": "select", "options": ["Draft", "Published"]}) is None

    def test_missing_name(self):
        """Fields without a name fail validation."""
        err = _validate_field({"type": "text"})
        assert err is not None
        assert "missing a 'name'" in err

    def test_missing_type(self):
        """Fields without a type fail validation."""
        err = _validate_field({"name": "Title"})
        assert err is not None
        assert "missing a 'type'" in err

    def test_unsupported_image_type(self):
        """Image type returns a helpful 'planned for future' message."""
        err = _validate_field({"name": "Photo", "type": "image"})
        assert err is not None
        assert "not supported in V1" in err
        assert "planned for a future release" in err

    def test_unsupported_media_type(self):
        """Media type returns a helpful 'planned for future' message."""
        err = _validate_field({"name": "Video", "type": "media"})
        assert err is not None
        assert "not supported in V1" in err

    def test_unsupported_relationship_type(self):
        """Relationship type returns a helpful 'planned for future' message."""
        err = _validate_field({"name": "Author", "type": "relationship"})
        assert err is not None
        assert "not supported in V1" in err

    def test_unknown_type(self):
        """Unknown field types fail with a list of supported types."""
        err = _validate_field({"name": "Foo", "type": "foobar"})
        assert err is not None
        assert "Unknown type" in err
        assert "Supported:" in err

    def test_select_without_options(self):
        """Select fields without options array fail validation."""
        err = _validate_field({"name": "Status", "type": "select"})
        assert err is not None
        assert "require an 'options' array" in err

    def test_select_with_non_string_options(self):
        """Select options must be strings, not numbers or objects."""
        err = _validate_field({"name": "Rating", "type": "select", "options": [1, 2, 3]})
        assert err is not None
        assert "array of strings" in err

    def test_select_with_mixed_options(self):
        """Select options must all be strings."""
        err = _validate_field({"name": "Status", "type": "select", "options": ["Draft", 2]})
        assert err is not None
        assert "array of strings" in err


class TestSchemaValidation:
    """Schema-level validation (multiple fields)."""

    def test_valid_schema(self):
        """Schema with all valid fields returns no errors."""
        schema = [
            {"name": "Title", "type": "text", "required": True},
            {"name": "Body", "type": "richtext"},
            {"name": "Status", "type": "select", "options": ["Draft", "Live"]},
        ]
        assert _validate_schema(schema) == []

    def test_multiple_errors_returned(self):
        """All field errors are returned, not just the first one."""
        schema = [
            {"type": "text"},  # missing name
            {"name": "Photo", "type": "image"},  # unsupported
            {"name": "Status", "type": "select"},  # missing options
        ]
        errors = _validate_schema(schema)
        assert len(errors) == 3

    def test_empty_schema_is_valid(self):
        """An empty schema is valid (no fields to validate)."""
        assert _validate_schema([]) == []


class TestGuidelinesAutogen:
    """Auto-generation of default guidelines for richtext fields."""

    def test_adds_default_for_richtext(self):
        """Richtext fields without guidelines get a default added."""
        schema = [{"name": "Bio", "type": "richtext"}]
        updated, generated = _ensure_richtext_guidelines(schema)
        assert "Bio" in generated
        assert updated[0]["guidelines"] is not None

    def test_preserves_explicit_guidelines(self):
        """Richtext fields with explicit guidelines keep them."""
        schema = [{"name": "Bio", "type": "richtext", "guidelines": "Custom guidelines"}]
        updated, generated = _ensure_richtext_guidelines(schema)
        assert generated == []
        assert updated[0]["guidelines"] == "Custom guidelines"

    def test_ignores_non_richtext_fields(self):
        """Non-richtext fields don't get guidelines added."""
        schema = [{"name": "Title", "type": "text"}]
        updated, generated = _ensure_richtext_guidelines(schema)
        assert generated == []
        assert "guidelines" not in updated[0]

    def test_multiple_richtext_fields(self):
        """All richtext fields without guidelines get defaults."""
        schema = [
            {"name": "Bio", "type": "richtext"},
            {"name": "Summary", "type": "richtext"},
            {"name": "Notes", "type": "richtext", "guidelines": "Existing"},
        ]
        updated, generated = _ensure_richtext_guidelines(schema)
        assert set(generated) == {"Bio", "Summary"}
        assert updated[2]["guidelines"] == "Existing"


class TestRequiredDefaults:
    """Required field defaults to True when not specified."""

    def test_adds_required_true(self):
        """Fields without required get it set to True."""
        schema = [{"name": "Title", "type": "text"}]
        _apply_required_defaults(schema)
        assert schema[0]["required"] is True

    def test_preserves_explicit_required_false(self):
        """Fields with explicit required=False keep it."""
        schema = [{"name": "Title", "type": "text", "required": False}]
        _apply_required_defaults(schema)
        assert schema[0]["required"] is False

    def test_preserves_explicit_required_true(self):
        """Fields with explicit required=True keep it."""
        schema = [{"name": "Title", "type": "text", "required": True}]
        _apply_required_defaults(schema)
        assert schema[0]["required"] is True


class TestToolSetReduction:
    """Admin tool set excludes commit and extend, includes set_active."""

    def test_commit_not_in_admin_tools(self):
        """cms_commit_content_type is not available to the agent in admin mode."""
        tool_names = {t["function"]["name"] for t in CMS_ADMIN_TOOLS}
        assert "cms_commit_content_type" not in tool_names

    def test_extend_not_in_admin_tools(self):
        """cms_extend_content_type_schema is not available to the agent in admin mode."""
        tool_names = {t["function"]["name"] for t in CMS_ADMIN_TOOLS}
        assert "cms_extend_content_type_schema" not in tool_names

    def test_set_active_in_admin_tools(self):
        """cms_set_active_content_type is available to the agent in admin mode."""
        tool_names = {t["function"]["name"] for t in CMS_ADMIN_TOOLS}
        assert "cms_set_active_content_type" in tool_names

    def test_create_in_admin_tools(self):
        """cms_create_content_type is available to the agent in admin mode."""
        tool_names = {t["function"]["name"] for t in CMS_ADMIN_TOOLS}
        assert "cms_create_content_type" in tool_names

    def test_update_in_admin_tools(self):
        """cms_update_content_type_schema is available to the agent in admin mode."""
        tool_names = {t["function"]["name"] for t in CMS_ADMIN_TOOLS}
        assert "cms_update_content_type_schema" in tool_names

    def test_commit_still_in_full_definitions(self):
        """cms_commit_content_type is still defined (for UI endpoint use)."""
        tool_names = {t["function"]["name"] for t in CMS_TOOL_DEFINITIONS}
        assert "cms_commit_content_type" in tool_names

    def test_extend_still_in_full_definitions(self):
        """cms_extend_content_type_schema is still defined (for future use)."""
        tool_names = {t["function"]["name"] for t in CMS_TOOL_DEFINITIONS}
        assert "cms_extend_content_type_schema" in tool_names


class TestActiveContentTypeTracking:
    """Context tracking for active content type within a request."""

    def test_set_and_get_active_content_type(self):
        """set_active_content_type_id and get_active_content_type_id work correctly."""
        assert get_active_content_type_id() is None
        set_active_content_type_id(42)
        assert get_active_content_type_id() == 42

    def test_reset_active_content_type(self):
        """Active content type can be reset to None."""
        set_active_content_type_id(42)
        set_active_content_type_id(None)
        assert get_active_content_type_id() is None


class TestCreateContentTypeHandler:
    """Integration tests for handle_cms_create_content_type."""

    def test_validation_error_returns_all_errors(self):
        """Schema validation errors are returned and block creation."""
        with patch("service.cms_tools.httpx.post") as mock_post:
            result = json.loads(handle_cms_create_content_type({
                "name": "Test",
                "schema": [
                    {"type": "text"},  # missing name
                    {"name": "Photo", "type": "image"},  # unsupported
                ],
            }))
            assert result["status"] == "error"
            assert "validation failed" in result["message"].lower()
            assert len(result["errors"]) == 2
            mock_post.assert_not_called()

    def test_successful_create_sets_active_context(self):
        """Successful creation sets the active content type context."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"doc": {"id": 99, "name": "Test", "slug": "test"}}
        mock_response.raise_for_status = MagicMock()

        with patch("service.cms_tools.httpx.post", return_value=mock_response):
            result = json.loads(handle_cms_create_content_type({
                "name": "Test",
                "schema": [{"name": "Title", "type": "text"}],
            }))
            assert result["status"] == "ok"
            assert get_active_content_type_id() == 99

    def test_guidelines_generated_for_richtext(self):
        """Richtext fields without guidelines get defaults and are reported."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"doc": {"id": 100, "name": "Test", "slug": "test"}}
        mock_response.raise_for_status = MagicMock()

        with patch("service.cms_tools.httpx.post", return_value=mock_response) as mock_post:
            result = json.loads(handle_cms_create_content_type({
                "name": "Test",
                "schema": [{"name": "Bio", "type": "richtext"}],
            }))
            assert result["status"] == "ok"
            assert "guidelines_generated_for" in result
            assert "Bio" in result["guidelines_generated_for"]


class TestUpdateContentTypeHandler:
    """Integration tests for handle_cms_update_content_type_schema."""

    def test_validation_error_blocks_update(self):
        """Schema validation errors block the update."""
        mock_fetch = MagicMock()
        mock_fetch.return_value = {"id": 50, "status": "draft", "org": {"slug": CALLER_SLUG}}

        with patch("service.cms_tools._fetch_content_type", mock_fetch):
            with patch("service.cms_tools.httpx.patch") as mock_patch:
                result = json.loads(handle_cms_update_content_type_schema({
                    "contentTypeId": 50,
                    "schema": [{"name": "Photo", "type": "image"}],
                }))
                assert result["status"] == "error"
                mock_patch.assert_not_called()

    def test_committed_type_cannot_be_updated(self):
        """Committed content types cannot have their schema overwritten."""
        mock_fetch = MagicMock()
        mock_fetch.return_value = {"id": 51, "status": "committed", "org": {"slug": CALLER_SLUG}}

        with patch("service.cms_tools._fetch_content_type", mock_fetch):
            result = json.loads(handle_cms_update_content_type_schema({
                "contentTypeId": 51,
                "schema": [{"name": "Title", "type": "text"}],
            }))
            assert result["status"] == "error"
            assert "committed" in result["message"].lower()


class TestSetActiveContentTypeHandler:
    """Integration tests for handle_cms_set_active_content_type."""

    def test_sets_active_context(self):
        """Handler sets the active content type context."""
        mock_fetch = MagicMock()
        mock_fetch.return_value = {"id": 60, "name": "Team Members", "status": "draft", "org": {"slug": CALLER_SLUG}}

        with patch("service.cms_tools._fetch_content_type", mock_fetch):
            result = json.loads(handle_cms_set_active_content_type({"contentTypeId": 60}))
            assert result["status"] == "ok"
            assert result["activeContentType"]["id"] == 60
            assert get_active_content_type_id() == 60

    def test_not_found_returns_error(self):
        """Handler returns not_found for non-existent content type."""
        with patch("service.cms_tools._fetch_content_type", return_value=None):
            result = json.loads(handle_cms_set_active_content_type({"contentTypeId": 999}))
            assert result["status"] == "not_found"
