"""Unit tests for CMS domain routing — exported tool subsets and _resolve_domain dispatch.

Tests the exported constants and handler maps directly (no HTTP, no LLM).
All assertions are structural: correct tool counts, correct key sets, correct
mode→(prompt, tools, handlers) tuples from _resolve_domain.
"""

import pytest

from service.cms_tools import (
    CMS_EDITING_TOOLS,
    CMS_EDITING_HANDLERS,
    CMS_SCHEDULING_TOOLS,
    CMS_SCHEDULING_HANDLERS,
    CMS_ADMIN_TOOLS,
    CMS_ADMIN_HANDLERS,
)
from service.app import _resolve_domain

pytestmark = pytest.mark.cms


# ---------------------------------------------------------------------------
# Tool subset membership
# ---------------------------------------------------------------------------


class TestCmsEditingTools:
    EXPECTED = {
        "cms_get_item",
        "cms_create_item",
        "cms_update_item",
        "cms_submit_for_approval",
        "cms_restore_version",
        "cms_lock_item",
        "cms_unlock_item",
        "cms_add_to_schedule",
    }

    def test_contains_exactly_eight_tools(self):
        assert len(CMS_EDITING_TOOLS) == 8

    def test_tool_names_match_expected_set(self):
        names = {t["function"]["name"] for t in CMS_EDITING_TOOLS}
        assert names == self.EXPECTED

    def test_handler_keys_match_tool_names(self):
        tool_names = {t["function"]["name"] for t in CMS_EDITING_TOOLS}
        assert set(CMS_EDITING_HANDLERS.keys()) == tool_names

    def test_all_handlers_are_callable(self):
        for name, fn in CMS_EDITING_HANDLERS.items():
            assert callable(fn), f"Handler for {name!r} is not callable"


class TestCmsSchedulingTools:
    EXPECTED = {"cms_add_to_schedule", "cms_get_item"}

    def test_contains_exactly_two_tools(self):
        assert len(CMS_SCHEDULING_TOOLS) == 2

    def test_tool_names_match_expected_set(self):
        names = {t["function"]["name"] for t in CMS_SCHEDULING_TOOLS}
        assert names == self.EXPECTED

    def test_handler_keys_match_tool_names(self):
        tool_names = {t["function"]["name"] for t in CMS_SCHEDULING_TOOLS}
        assert set(CMS_SCHEDULING_HANDLERS.keys()) == tool_names


class TestCmsAdminTools:
    EXPECTED = {
        "cms_create_content_type",
        "cms_update_content_type_schema",
        "cms_set_active_content_type",
    }

    def test_contains_exactly_three_tools(self):
        assert len(CMS_ADMIN_TOOLS) == 3

    def test_tool_names_match_expected_set(self):
        names = {t["function"]["name"] for t in CMS_ADMIN_TOOLS}
        assert names == self.EXPECTED

    def test_handler_keys_match_tool_names(self):
        tool_names = {t["function"]["name"] for t in CMS_ADMIN_TOOLS}
        assert set(CMS_ADMIN_HANDLERS.keys()) == tool_names


# ---------------------------------------------------------------------------
# _resolve_domain dispatch for the CMS app
# ---------------------------------------------------------------------------


def _resolve_cms(mode, context_extra=None):
    """Call _resolve_domain with app='cms' and given mode."""
    context = {"mode": mode}
    if context_extra:
        context.update(context_extra)
    return _resolve_domain("cms", has_csv=False, context=context)


class TestResolveDomainCms:
    def test_editing_mode_returns_editing_tools(self):
        _, tools, handlers = _resolve_cms("editing")
        names = {t["function"]["name"] for t in tools}
        assert names == {t["function"]["name"] for t in CMS_EDITING_TOOLS}
        assert set(handlers.keys()) == set(CMS_EDITING_HANDLERS.keys())

    def test_scheduling_mode_returns_scheduling_tools(self):
        _, tools, handlers = _resolve_cms("scheduling")
        names = {t["function"]["name"] for t in tools}
        assert names == {t["function"]["name"] for t in CMS_SCHEDULING_TOOLS}
        assert set(handlers.keys()) == set(CMS_SCHEDULING_HANDLERS.keys())

    def test_admin_mode_returns_admin_tools(self):
        _, tools, handlers = _resolve_cms("admin")
        names = {t["function"]["name"] for t in tools}
        assert names == {t["function"]["name"] for t in CMS_ADMIN_TOOLS}
        assert set(handlers.keys()) == set(CMS_ADMIN_HANDLERS.keys())

    @pytest.mark.parametrize("mode", ["browse", "approval", "admin-permissions"])
    def test_guide_only_modes_return_empty_tools(self, mode):
        """Guide-only modes yield no tools and no handlers."""
        _, tools, handlers = _resolve_cms(mode)
        assert tools == []
        assert handlers == {}

    def test_unknown_mode_falls_back_to_empty_tools(self):
        """Unrecognised mode defaults to browse (guide-only: empty tools)."""
        _, tools, handlers = _resolve_cms("totally-unknown-mode")
        assert tools == []
        assert handlers == {}

    def test_missing_mode_falls_back_to_empty_tools(self):
        """Missing mode key in context defaults to browse."""
        _, tools, handlers = _resolve_domain("cms", has_csv=False, context={})
        assert tools == []
        assert handlers == {}

    def test_none_context_falls_back_to_empty_tools(self):
        """None context defaults to browse."""
        _, tools, handlers = _resolve_domain("cms", has_csv=False, context=None)
        assert tools == []
        assert handlers == {}

    def test_editing_mode_with_item_context_appends_to_prompt(self):
        """itemId in context is appended to the editing prompt body."""
        prompt, _, _ = _resolve_cms("editing", context_extra={"itemId": 99})
        assert "99" in prompt

    def test_editing_mode_with_ct_slug_appends_to_prompt(self):
        """contentTypeSlug in context is appended to the editing prompt body."""
        prompt, _, _ = _resolve_cms("editing", context_extra={"contentTypeSlug": "job-listings"})
        assert "job-listings" in prompt


# ---------------------------------------------------------------------------
# Tool definition schema sanity checks
# ---------------------------------------------------------------------------


class TestToolDefinitionSchema:
    """Each tool definition must conform to the OpenAI function-calling schema shape."""

    @pytest.mark.parametrize("tool", CMS_EDITING_TOOLS + CMS_SCHEDULING_TOOLS + CMS_ADMIN_TOOLS)
    def test_tool_has_required_shape(self, tool):
        assert tool["type"] == "function"
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert "required" in params
