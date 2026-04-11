"""Step definitions for table view control scenarios (column visibility + filters).

Covers C1-C10 (column visibility) and F1-F13 (row filtering) from task 63.
Most When/Then steps are inherited from the shared conftest. This file
contains table-control-specific When steps (context-aware chat, direct tool
calls) and Then assertions.
"""

import json
import os

from pytest_bdd import scenarios, when, then, parsers

from tests.conftest import chat, chat_with_context

scenarios("../features/table_controls.feature")


# ── When — context-aware chat (passes visibleColumns / dataPaneOpen) ──


@when(
    parsers.parse('the user says "{prompt}" with visible columns "{columns}"'),
    target_fixture="context",
)
def user_says_with_visible_columns(prompt, columns):
    visible = [c.strip() for c in columns.split(",")]
    table_view = {
        "visibleColumns": visible,
        "activeFilters": [],
        "dataPaneOpen": True,
    }
    return {"result": chat_with_context(prompt, table_view=table_view)}


@when(
    parsers.parse('the user says "{prompt}" with data pane open'),
    target_fixture="context",
)
def user_says_with_data_pane_open(prompt):
    table_view = {
        "visibleColumns": ["accountType", "department", "owner"],
        "activeFilters": [],
        "dataPaneOpen": True,
    }
    return {"result": chat_with_context(prompt, table_view=table_view)}


# ── When — direct tool handler calls (bypass LLM) ────────────────────


def _ensure_table_configs():
    """Populate TABLE_CONFIGS in the test process if not already loaded."""
    from service.tools import TABLE_CONFIGS
    if "vendors" not in TABLE_CONFIGS:
        from service.tools import TableConfig
        from service.app import _VENDOR_FIELD_MAP
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
                "sync columns": ["sourceSystem", "sourceSystemId", "lastSyncedAt"],
            },
            pinned="name",
        )


@when(
    parsers.parse('set_view_columns is called directly with table "{table}" and columns "{columns}"'),
    target_fixture="context",
)
def call_set_view_columns_directly(table, columns):
    _ensure_table_configs()
    from service.tools import execute_set_view_columns
    result_str = execute_set_view_columns(
        {"table": table, "columns": [c.strip() for c in columns.split(",")]},
    )
    return {"tool_result": json.loads(result_str)}


@when(
    parsers.parse('set_table_filters is called directly with column "{column}" and values "{values}"'),
    target_fixture="context",
)
def call_set_table_filters_directly(column, values):
    _ensure_table_configs()
    from service.tools import execute_set_table_filters
    result_str = execute_set_table_filters(
        {"table": "vendors", "filters": [{"column": column, "values": [v.strip() for v in values.split(",")]}]},
    )
    return {"tool_result": json.loads(result_str)}


@when(
    parsers.parse('set_table_filters is called directly with table "{table}"'),
    target_fixture="context",
)
def call_set_table_filters_unknown_table(table):
    from service.tools import execute_set_table_filters
    result_str = execute_set_table_filters(
        {"table": table, "filters": [{"column": "department", "values": ["IT"]}]},
    )
    return {"tool_result": json.loads(result_str)}


# ── Then — direct tool result assertions ──────────────────────────────


@then(parsers.parse('the tool result contains error "{fragment}"'))
def assert_tool_error(context, fragment):
    tool_result = context["tool_result"]
    assert tool_result.get("ok") is False, (
        f"Expected ok=False, got: {tool_result}"
    )
    error = tool_result.get("error", "")
    assert fragment.lower() in error.lower(), (
        f"Expected '{fragment}' in error, got: {error}"
    )


# ── Then — column visibility assertions ───────────────────────────────


@then(parsers.parse('the view_columns include "{column}"'))
def assert_view_columns_include(context, column):
    actions = context["result"].get("pending_actions", [])
    set_col_actions = [a for a in actions if a["type"] == "set_columns"]
    assert set_col_actions, (
        f"No set_columns action found. Actions: {actions}. "
        f"Reply: {context['result']['reply'][:300]}"
    )
    view_columns = set_col_actions[0].get("view_columns", [])
    assert column in view_columns, (
        f"Expected '{column}' in view_columns, got: {view_columns}"
    )


@then(parsers.parse('the view_columns do not include "{column}"'))
def assert_view_columns_exclude(context, column):
    actions = context["result"].get("pending_actions", [])
    set_col_actions = [a for a in actions if a["type"] == "set_columns"]
    assert set_col_actions, (
        f"No set_columns action found. Actions: {actions}. "
        f"Reply: {context['result']['reply'][:300]}"
    )
    view_columns = set_col_actions[0].get("view_columns", [])
    assert column not in view_columns, (
        f"Expected '{column}' NOT in view_columns, got: {view_columns}"
    )


@then(parsers.parse('the view_columns only contain keys from "{allowed_keys}"'))
def assert_view_columns_subset(context, allowed_keys):
    allowed = {k.strip() for k in allowed_keys.split(",")}
    actions = context["result"].get("pending_actions", [])
    set_col_actions = [a for a in actions if a["type"] == "set_columns"]
    assert set_col_actions, (
        f"No set_columns action found. Actions: {actions}. "
        f"Reply: {context['result']['reply'][:300]}"
    )
    view_columns = set(set_col_actions[0].get("view_columns", []))
    extra = view_columns - allowed
    assert not extra, (
        f"view_columns contain unexpected keys: {extra}. "
        f"Allowed: {allowed}, got: {view_columns}"
    )


@then(parsers.parse("the view_columns have at least {count:d} entries"))
def assert_view_columns_min_count(context, count):
    actions = context["result"].get("pending_actions", [])
    set_col_actions = [a for a in actions if a["type"] == "set_columns"]
    assert set_col_actions, (
        f"No set_columns action found. Actions: {actions}. "
        f"Reply: {context['result']['reply'][:300]}"
    )
    view_columns = set_col_actions[0].get("view_columns", [])
    assert len(view_columns) >= count, (
        f"Expected at least {count} columns, got {len(view_columns)}: {view_columns}"
    )


# ── Then — filter assertions ──────────────────────────────────────────


@then(parsers.parse('the table_filters target column "{column}"'))
def assert_filter_targets_column(context, column):
    actions = context["result"].get("pending_actions", [])
    filter_actions = [a for a in actions if a["type"] == "set_filters"]
    assert filter_actions, (
        f"No set_filters action found. Actions: {actions}. "
        f"Reply: {context['result']['reply'][:300]}"
    )
    filters = filter_actions[0].get("table_filters", [])
    columns_targeted = [f.get("column") for f in filters]
    assert column in columns_targeted, (
        f"Expected filter on '{column}', got filters targeting: {columns_targeted}"
    )


@then(parsers.parse('the table_filters column "{column}" has value "{value}"'))
def assert_filter_has_value(context, column, value):
    actions = context["result"].get("pending_actions", [])
    filter_actions = [a for a in actions if a["type"] == "set_filters"]
    assert filter_actions, (
        f"No set_filters action found. Actions: {actions}. "
        f"Reply: {context['result']['reply'][:300]}"
    )
    filters = filter_actions[0].get("table_filters", [])
    for f in filters:
        if f.get("column") == column:
            assert value in f.get("values", []), (
                f"Expected '{value}' in filter values for '{column}', "
                f"got: {f.get('values')}"
            )
            return
    assert False, (
        f"No filter found for column '{column}'. Filters: {filters}"
    )


@then("the table_filters are empty")
def assert_filters_empty(context):
    actions = context["result"].get("pending_actions", [])
    filter_actions = [a for a in actions if a["type"] == "set_filters"]
    assert filter_actions, (
        f"No set_filters action found. Actions: {actions}. "
        f"Reply: {context['result']['reply'][:300]}"
    )
    filters = filter_actions[0].get("table_filters", [])
    assert filters == [], (
        f"Expected empty table_filters for clear, got: {filters}"
    )


# ── Then — reply text assertions (error scenarios) ────────────────────


@then(parsers.parse('the reply mentions any of "{keywords}"'))
def assert_reply_mentions_any(context, keywords):
    reply = context["result"]["reply"].lower()
    keyword_list = [k.strip().lower() for k in keywords.split("|")]
    assert any(k in reply for k in keyword_list), (
        f"Expected one of {keyword_list} in reply, got: {reply[:500]}"
    )


@then(parsers.parse('the reply mentions "{keyword}" and does not return a set_filters action'))
def assert_reply_mentions_no_filter_action(context, keyword):
    result = context["result"]
    reply = result["reply"].lower()
    assert keyword.lower() in reply, (
        f"Expected '{keyword}' in reply, got: {reply[:500]}"
    )
    actions = result.get("pending_actions", [])
    filter_actions = [a for a in actions if a["type"] == "set_filters"]
    assert not filter_actions, (
        f"Expected no set_filters action for OR request, but got: {filter_actions}"
    )
