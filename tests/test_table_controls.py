"""Unit tests for set_view_columns and set_table_filters tool handlers.

These test the handler functions directly (no LLM, no server) to validate
argument validation, error messages, and response shapes.
"""

import json

import pytest

from service.tools import (
    ColumnConfig,
    TableConfig,
    TABLE_CONFIGS,
    execute_set_view_columns,
    execute_set_table_filters,
)

pytestmark = pytest.mark.vendor_management


@pytest.fixture(autouse=True)
def _seed_table_config():
    """Populate TABLE_CONFIGS with a test entry for the duration of each test."""
    TABLE_CONFIGS["test_table"] = TableConfig(
        columns={
            "colA": ColumnConfig(label="Column A", col_type="categorical", db_name="col_a"),
            "colB": ColumnConfig(label="Column B", col_type="boolean", db_name="col_b"),
            "colC": ColumnConfig(label="Column C", col_type="date", db_name="col_c"),
            "colD": ColumnConfig(label="Column D", col_type="numeric", db_name="col_d"),
            "colE": ColumnConfig(label="Column E", col_type="text", db_name="col_e"),
        },
        default_columns=["colA", "colB"],
        column_groups={"group_ab": ["colA", "colB"]},
        pinned="name",
    )
    yield
    TABLE_CONFIGS.pop("test_table", None)


class TestSetViewColumns:
    """Unit tests for execute_set_view_columns."""

    def test_valid_columns(self):
        result = json.loads(execute_set_view_columns({"table": "test_table", "columns": ["colA", "colC"]}))
        assert result["action"] == "set_columns"
        assert result["table"] == "test_table"
        assert result["view_columns"] == ["colA", "colC"]

    def test_reset_returns_defaults(self):
        result = json.loads(execute_set_view_columns({"table": "test_table", "reset": True}))
        assert result["action"] == "set_columns"
        assert result["view_columns"] == ["colA", "colB"]

    def test_unknown_table(self):
        result = json.loads(execute_set_view_columns({"table": "nonexistent"}))
        assert result["ok"] is False
        assert "nonexistent" in result["error"]

    def test_invalid_column_key(self):
        result = json.loads(execute_set_view_columns({"table": "test_table", "columns": ["colA", "bogus"]}))
        assert result["ok"] is False
        assert "bogus" in str(result["error"])

    def test_no_columns_no_reset(self):
        result = json.loads(execute_set_view_columns({"table": "test_table"}))
        assert result["ok"] is False

    def test_deduplication(self):
        result = json.loads(execute_set_view_columns({"table": "test_table", "columns": ["colA", "colA", "colB"]}))
        assert result["view_columns"] == ["colA", "colB"]

    def test_filterable_columns_derived(self):
        """Filterable columns are auto-derived from col_type."""
        config = TABLE_CONFIGS["test_table"]
        assert config.filterable_columns == {"colA", "colB"}

    def test_valid_columns_derived(self):
        """Valid columns are auto-derived from columns keys."""
        config = TABLE_CONFIGS["test_table"]
        assert config.valid_columns == {"colA", "colB", "colC", "colD", "colE"}


class TestSetTableFilters:
    """Unit tests for execute_set_table_filters."""

    def test_valid_filter(self):
        result = json.loads(execute_set_table_filters({
            "table": "test_table",
            "filters": [{"column": "colA", "values": ["x", "y"]}],
        }))
        assert result["action"] == "set_filters"
        assert result["table"] == "test_table"
        assert len(result["table_filters"]) == 1

    def test_clear_returns_empty(self):
        result = json.loads(execute_set_table_filters({"table": "test_table", "clear": True}))
        assert result["action"] == "set_filters"
        assert result["table_filters"] == []

    def test_unknown_table(self):
        result = json.loads(execute_set_table_filters({"table": "nonexistent"}))
        assert result["ok"] is False

    def test_non_filterable_column(self):
        """Date columns should not be filterable via set-based filtering."""
        result = json.loads(execute_set_table_filters({
            "table": "test_table",
            "filters": [{"column": "colC", "values": ["2026-01-01"]}],
        }))
        assert result["ok"] is False
        assert "colC" in result["error"]

    def test_unknown_column(self):
        result = json.loads(execute_set_table_filters({
            "table": "test_table",
            "filters": [{"column": "bogus", "values": ["x"]}],
        }))
        assert result["ok"] is False

    def test_no_filters_no_clear(self):
        result = json.loads(execute_set_table_filters({"table": "test_table"}))
        assert result["ok"] is False

    def test_boolean_column_filterable(self):
        """Boolean columns should be filterable."""
        result = json.loads(execute_set_table_filters({
            "table": "test_table",
            "filters": [{"column": "colB", "values": ["true"]}],
        }))
        assert result["action"] == "set_filters"
