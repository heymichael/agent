"""Tests for the CSV bulk edit infrastructure and endpoints.

Covers: CsvColumnSpec, TableCsvProfile, generic validation/resolution
pipeline, tool handlers, and the batch-update endpoint.
"""

from unittest.mock import patch, MagicMock

import pytest

pytestmark = pytest.mark.vendor_management
from fastapi.testclient import TestClient

from service.app import app, get_verified_user
from service.auth import get_caller_enabled_apps
from service import pg_client
from service.tools import (
    CsvColumnSpec,
    TableCsvProfile,
    VENDOR_CSV_PROFILE,
    generate_edit_csv,
    parse_csv,
    validate_csv_columns,
    validate_csv_ids,
    validate_csv_values,
    resolve_csv_updates,
    process_csv_upload,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

SAMPLE_VENDORS = [
    {
        "id": "aaa-111", "name": "Acme Corp", "department": "Engineering",
        "owner": "alice@example.com", "secondaryOwner": None,
        "billingFrequency": "monthly",
        "purpose": "Cloud hosting", "spendType": None,
        "contractStartDate": "2025-01-01", "contractEndDate": None,
        "autoRenew": True,
    },
    {
        "id": "bbb-222", "name": "Beta Inc", "department": "Marketing",
        "owner": "bob@example.com", "secondaryOwner": None,
        "billingFrequency": "annual",
        "purpose": None, "spendType": None,
        "contractStartDate": None, "contractEndDate": None,
        "autoRenew": False,
    },
]

SAMPLE_DEPARTMENTS = [
    {"id": "dept-1", "name": "Engineering"},
    {"id": "dept-2", "name": "Marketing"},
    {"id": "dept-3", "name": "Finance"},
]

SAMPLE_USERS = [
    {"id": "u-1", "email": "alice@example.com", "fullName": "Alice Smith"},
    {"id": "u-2", "email": "bob@example.com", "fullName": "Bob Jones"},
]


GOOD_UUID_1 = "a0000000-0000-0000-0000-000000000001"
GOOD_UUID_2 = "a0000000-0000-0000-0000-000000000002"
BAD_UUID = "b0000000-0000-0000-0000-00000000dead"


def _tiny_profile():
    """A minimal profile for unit-testing the generic pipeline."""
    return TableCsvProfile(
        table="widgets",
        columns=[
            CsvColumnSpec("name", "name", "text"),
            CsvColumnSpec("color", "color", "enum", valid_values=["red", "blue", "green"]),
            CsvColumnSpec("active", "is_active", "bool"),
            CsvColumnSpec("builtOn", "built_on", "date"),
        ],
        id_check_fn=lambda ids: {i: i != BAD_UUID for i in ids},
        pk_key="widget_id",
    )


# ── TableCsvProfile ─────────────────────────────────────────────────────

class TestTableCsvProfile:
    """Profile metadata must accurately describe table columns and primary key layout."""

    def test_csv_headers_includes_pk_first(self):
        """The primary-key column must always appear first in generated CSV headers."""
        p = _tiny_profile()
        assert p.csv_headers[0] == "id"
        assert "name" in p.csv_headers
        assert "color" in p.csv_headers

    def test_get_spec_returns_matching_column(self):
        """Looking up a known column name must return the correct spec with type metadata."""
        p = _tiny_profile()
        spec = p.get_spec("color")
        assert spec is not None
        assert spec.db_name == "color"
        assert spec.col_type == "enum"

    def test_get_spec_returns_none_for_unknown(self):
        """Looking up an unrecognized column name must return None, not raise."""
        p = _tiny_profile()
        assert p.get_spec("nonexistent") is None


# ── generate_edit_csv ────────────────────────────────────────────────────

class TestGenerateEditCsv:
    """Generated CSV text must faithfully represent profile headers and record values."""

    def test_generates_csv_with_headers_and_rows(self):
        """Output CSV must contain the profile header row followed by one data row per record."""
        p = _tiny_profile()
        records = [
            {"id": "w1", "name": "Gadget", "color": "red", "active": True, "builtOn": "2025-01-01"},
        ]
        csv_str = generate_edit_csv(p, records)
        lines = [l.strip() for l in csv_str.strip().splitlines()]
        assert lines[0] == "id,name,color,active,builtOn"
        assert "w1" in lines[1]
        assert "Gadget" in lines[1]

    def test_ignores_extra_fields(self):
        """Fields not defined in the profile must never appear in the generated CSV."""
        p = _tiny_profile()
        records = [{"id": "w1", "name": "X", "extra_field": "ignored"}]
        csv_str = generate_edit_csv(p, records)
        assert "extra_field" not in csv_str


# ── parse_csv ────────────────────────────────────────────────────────────

class TestParseCsv:
    """Raw CSV text must be parsed into structured headers and row dicts."""

    def test_parses_headers_and_rows(self):
        """Well-formed CSV must yield the correct header list and row dictionaries."""
        content = "id,name,color\nw1,Gadget,red\nw2,Widget,blue\n"
        headers, rows = parse_csv(content)
        assert headers == ["id", "name", "color"]
        assert len(rows) == 2
        assert rows[0]["name"] == "Gadget"

    def test_empty_csv_returns_empty_rows(self):
        """A header-only CSV (no data rows) must return an empty row list, not error."""
        content = "id,name\n"
        headers, rows = parse_csv(content)
        assert headers == ["id", "name"]
        assert rows == []


# ── validate_csv_columns ─────────────────────────────────────────────────

class TestValidateCsvColumns:
    """CSV column headers must match the profile schema before any row-level processing."""

    def test_valid_columns_pass(self):
        """Headers that exist in the profile must produce zero validation errors."""
        p = _tiny_profile()
        errors = validate_csv_columns(p, ["id", "name", "color"])
        assert errors == []

    def test_unknown_column_rejected(self):
        """A header not defined in the profile must be rejected with a column-level error."""
        p = _tiny_profile()
        errors = validate_csv_columns(p, ["id", "name", "bogus"])
        assert len(errors) == 1
        assert errors[0]["column"] == "bogus"

    def test_missing_pk_rejected(self):
        """Omitting the primary-key column from headers must produce a validation error."""
        p = _tiny_profile()
        errors = validate_csv_columns(p, ["name", "color"])
        assert any(e["column"] == "id" for e in errors)


# ── validate_csv_ids ─────────────────────────────────────────────────────

class TestValidateCsvIds:
    """Row IDs in uploaded CSVs must reference existing database records."""

    def test_existing_ids_pass(self):
        """IDs that exist in the database must produce zero validation errors."""
        p = _tiny_profile()
        rows = [{"id": GOOD_UUID_1}, {"id": GOOD_UUID_2}]
        errors = validate_csv_ids(p, rows)
        assert errors == []

    def test_missing_id_flagged(self):
        """An ID not found in the database must be flagged with its row number."""
        p = _tiny_profile()
        rows = [{"id": GOOD_UUID_1}, {"id": BAD_UUID}]
        errors = validate_csv_ids(p, rows)
        assert len(errors) == 1
        assert errors[0]["value"] == BAD_UUID
        assert errors[0]["row"] == 3


# ── validate_csv_values ──────────────────────────────────────────────────

class TestValidateCsvValues:
    """Cell values must conform to the column type declared in the profile."""

    def test_valid_enum_passes(self):
        """A value in the allowed enum set must pass validation."""
        p = _tiny_profile()
        rows = [{"id": "w1", "color": "red"}]
        errors = validate_csv_values(p, rows, ["id", "color"])
        assert errors == []

    def test_invalid_enum_rejected(self):
        """A value outside the allowed enum set must be rejected."""
        p = _tiny_profile()
        rows = [{"id": "w1", "color": "purple"}]
        errors = validate_csv_values(p, rows, ["id", "color"])
        assert len(errors) == 1
        assert "purple" in errors[0]["message"]

    def test_valid_bool_passes(self):
        """All accepted boolean representations (true/false/yes/no/1/0) must pass."""
        p = _tiny_profile()
        for val in ("true", "false", "yes", "no", "1", "0"):
            errors = validate_csv_values(p, [{"id": "w1", "active": val}], ["id", "active"])
            assert errors == [], f"Failed for bool value: {val}"

    def test_invalid_bool_rejected(self):
        """A non-boolean string in a bool column must be rejected."""
        p = _tiny_profile()
        rows = [{"id": "w1", "active": "maybe"}]
        errors = validate_csv_values(p, rows, ["id", "active"])
        assert len(errors) == 1

    def test_valid_date_passes(self):
        """An ISO-8601 date string in a date column must pass validation."""
        p = _tiny_profile()
        rows = [{"id": "w1", "builtOn": "2025-06-15"}]
        errors = validate_csv_values(p, rows, ["id", "builtOn"])
        assert errors == []

    def test_invalid_date_rejected(self):
        """A non-date string in a date column must be rejected."""
        p = _tiny_profile()
        rows = [{"id": "w1", "builtOn": "not-a-date"}]
        errors = validate_csv_values(p, rows, ["id", "builtOn"])
        assert len(errors) == 1

    def test_empty_values_skipped(self):
        """Empty cell values must be silently skipped, not treated as invalid."""
        p = _tiny_profile()
        rows = [{"id": "w1", "color": "", "active": ""}]
        errors = validate_csv_values(p, rows, ["id", "color", "active"])
        assert errors == []


# ── resolve_csv_updates ──────────────────────────────────────────────────

class TestResolveCsvUpdates:
    """Validated CSV rows must resolve into correctly-typed update dicts keyed by the profile PK."""

    def test_resolves_enum_value(self):
        """Enum cell values must pass through as-is in the resolved changes dict."""
        p = _tiny_profile()
        rows = [{"id": "w1", "color": "red"}]
        updates = resolve_csv_updates(p, rows, ["id", "color"])
        assert len(updates) == 1
        assert updates[0]["widget_id"] == "w1"
        assert updates[0]["changes"]["color"] == "red"

    def test_resolves_bool_true(self):
        """Truthy boolean strings must resolve to Python True."""
        p = _tiny_profile()
        rows = [{"id": "w1", "active": "yes"}]
        updates = resolve_csv_updates(p, rows, ["id", "active"])
        assert updates[0]["changes"]["is_active"] is True

    def test_resolves_bool_false(self):
        """Falsy boolean strings must resolve to Python False."""
        p = _tiny_profile()
        rows = [{"id": "w1", "active": "false"}]
        updates = resolve_csv_updates(p, rows, ["id", "active"])
        assert updates[0]["changes"]["is_active"] is False

    def test_resolves_date_as_string(self):
        """Date values must resolve as ISO-8601 strings, not date objects."""
        p = _tiny_profile()
        rows = [{"id": "w1", "builtOn": "2025-06-15"}]
        updates = resolve_csv_updates(p, rows, ["id", "builtOn"])
        assert updates[0]["changes"]["built_on"] == "2025-06-15"

    def test_empty_values_produce_no_changes(self):
        """Rows where every editable cell is empty must be excluded from the update list."""
        p = _tiny_profile()
        rows = [{"id": "w1", "color": "", "name": ""}]
        updates = resolve_csv_updates(p, rows, ["id", "color", "name"])
        assert updates == []

    def test_uses_pk_key_from_profile(self):
        """The resolved update dict must use the profile's pk_key, not a hardcoded key."""
        p = _tiny_profile()
        rows = [{"id": "w1", "name": "Updated"}]
        updates = resolve_csv_updates(p, rows, ["id", "name"])
        assert "widget_id" in updates[0]


# ── process_csv_upload (full pipeline) ───────────────────────────────────

class TestProcessCsvUpload:
    """The upload pipeline must reject invalid CSVs at the earliest possible stage."""

    def test_empty_csv_returns_error(self):
        """A CSV with headers but no data rows must be rejected, not silently succeed."""
        p = _tiny_profile()
        result = process_csv_upload(p, "id,name\n")
        assert result["ok"] is False
        assert "empty" in result["error"].lower()

    def test_unknown_column_stops_at_column_check(self):
        """Unrecognized columns must halt the pipeline at the column_check stage."""
        p = _tiny_profile()
        result = process_csv_upload(p, "id,name,bogus\nw1,X,Y\n")
        assert result["ok"] is False
        assert result["stage"] == "column_check"

    def test_missing_pk_stops_at_column_check(self):
        """A CSV missing the primary-key column must halt at column_check."""
        p = _tiny_profile()
        result = process_csv_upload(p, "name,color\nX,red\n")
        assert result["ok"] is False
        assert result["stage"] == "column_check"

    def test_bad_id_stops_at_id_check(self):
        """A row referencing a nonexistent ID must halt the pipeline at id_check."""
        p = _tiny_profile()
        result = process_csv_upload(p, f"id,name\n{BAD_UUID},X\n")
        assert result["ok"] is False
        assert result["stage"] == "id_check"

    def test_invalid_value_stops_at_value_check(self):
        """A cell with an invalid typed value must halt the pipeline at value_check."""
        p = _tiny_profile()
        result = process_csv_upload(p, f"id,color\n{GOOD_UUID_1},purple\n")
        assert result["ok"] is False
        assert result["stage"] == "value_check"

    @patch("service.tools.pg_client.list_vendors", return_value=[
        {"id": GOOD_UUID_1, "name": "Widget A", "color": "blue", "is_active": True},
        {"id": GOOD_UUID_2, "name": "Widget B", "color": "red", "is_active": True},
    ])
    def test_valid_csv_returns_confirm_action(self, _mock):
        """A fully valid CSV must reach the confirm stage with an accurate change summary."""
        p = _tiny_profile()
        csv = f"id,color,active\n{GOOD_UUID_1},red,true\n{GOOD_UUID_2},blue,false\n"
        result = process_csv_upload(p, csv)
        assert result["ok"] is True
        assert result["action"] == "confirm_csv_batch"
        assert result["summary"]["vendor_count"] == 2

    def test_no_changes_detected(self):
        """A CSV with IDs but no editable values must report no changes, not error."""
        p = _tiny_profile()
        csv = f"id\n{GOOD_UUID_1}\n"
        result = process_csv_upload(p, csv)
        assert result["ok"] is True
        assert "No changes" in result["message"]


# ── VENDOR_CSV_PROFILE ───────────────────────────────────────────────────

class TestVendorCsvProfile:
    """The production vendor profile must declare the expected columns and types."""

    def test_has_id_as_first_header(self):
        """The vendor profile must list 'id' as its first CSV header."""
        assert VENDOR_CSV_PROFILE.csv_headers[0] == "id"

    def test_has_all_expected_columns(self):
        """Core vendor fields must be present in the profile's header set."""
        headers = set(VENDOR_CSV_PROFILE.csv_headers)
        for col in ("name", "department", "owner", "billingFrequency", "purpose"):
            assert col in headers, f"Missing expected column: {col}"

    def test_department_is_fk_type(self):
        """The department column must be typed as a foreign key mapping to department_id."""
        spec = VENDOR_CSV_PROFILE.get_spec("department")
        assert spec.col_type == "fk"
        assert spec.db_name == "department_id"

    def test_billingFrequency_is_enum_type(self):
        """billingFrequency must be an enum column that includes 'monthly' as a valid value."""
        spec = VENDOR_CSV_PROFILE.get_spec("billingFrequency")
        assert spec.col_type == "enum"
        assert "monthly" in spec.valid_values


# ── Vendor tool handlers ────────────────────────────────────────────────

class TestGenerateVendorEditCsv:
    """The generate-vendor-edit-csv tool must produce correct, filterable CSV exports."""

    @patch.object(pg_client, "list_vendors", return_value=SAMPLE_VENDORS)
    def test_generates_csv_with_all_vendors(self, mock_list):
        """An unfiltered call must include every vendor row in the exported CSV."""
        import json
        from service.tools import execute_generate_vendor_edit_csv

        result = json.loads(execute_generate_vendor_edit_csv({}))
        assert result["ok"] is True
        assert result["row_count"] == 2
        assert "id" in result["columns"]
        assert result["csv_filename"].startswith("vendors-edit")

    @patch.object(pg_client, "list_departments", return_value=SAMPLE_DEPARTMENTS)
    @patch.object(pg_client, "list_vendors", return_value=SAMPLE_VENDORS)
    def test_filters_by_department(self, mock_list, mock_depts):
        """Filtering by department must restrict the export to vendors in that department."""
        import json
        from service.tools import execute_generate_vendor_edit_csv

        result = json.loads(execute_generate_vendor_edit_csv({"departments": ["Engineering"]}))
        assert result["ok"] is True
        assert result["row_count"] == 1

    @patch.object(pg_client, "list_vendors", return_value=SAMPLE_VENDORS)
    def test_filters_by_vendor_names(self, mock_list):
        """Filtering by vendor name must restrict the export to matching vendors only."""
        import json
        from service.tools import execute_generate_vendor_edit_csv

        result = json.loads(execute_generate_vendor_edit_csv({"vendor_names": ["Acme Corp"]}))
        assert result["ok"] is True
        assert result["row_count"] == 1

    @patch.object(pg_client, "list_vendors", return_value=SAMPLE_VENDORS)
    def test_no_match_returns_error(self, mock_list):
        """A filter that matches zero vendors must return an error, not an empty CSV."""
        import json
        from service.tools import execute_generate_vendor_edit_csv

        result = json.loads(execute_generate_vendor_edit_csv({"vendor_names": ["Nonexistent"]}))
        assert result["ok"] is False


# ── Batch update endpoint ────────────────────────────────────────────────

def _make_client(email="test@example.com", active_org_slug="arcade"):
    app.dependency_overrides[get_verified_user] = lambda: {
        "email": email,
        "active_org_slug": active_org_slug,
    }
    app.dependency_overrides[get_caller_enabled_apps] = lambda: [
        "expenses", "vendors", "vendor_administration", "system_administration",
    ]
    return TestClient(app)


class TestBatchUpdateEndpoint:
    """The batch-update endpoint must apply valid updates and reject malformed requests."""

    def teardown_method(self):
        app.dependency_overrides.clear()

    @patch.object(pg_client, "batch_update_vendors", return_value=3)
    def test_successful_batch_update(self, mock_batch):
        """A well-formed update list must be forwarded to the database and return the affected count."""
        client = _make_client()
        updates = [
            {"vendor_id": "aaa", "changes": {"department_id": "dept-1"}},
            {"vendor_id": "bbb", "changes": {"department_id": "dept-2"}},
            {"vendor_id": "ccc", "changes": {"payment_method": "ACH"}},
        ]
        resp = client.post("/vendors/batch-update", json={"updates": updates})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["updated"] == 3
        mock_batch.assert_called_once_with(updates, "arcade")

    def test_empty_updates_returns_400(self):
        """An empty update list must be rejected with 400, not silently no-op."""
        client = _make_client()
        resp = client.post("/vendors/batch-update", json={"updates": []})
        assert resp.status_code == 400

    @patch.object(pg_client, "batch_update_vendors", side_effect=ValueError("Vendor 'bad' not found"))
    def test_bad_vendor_id_returns_400(self, mock_batch):
        """A vendor ID not found in the database must produce a 400 error."""
        client = _make_client()
        updates = [{"vendor_id": "bad", "changes": {"name": "X"}}]
        resp = client.post("/vendors/batch-update", json={"updates": updates})
        assert resp.status_code == 400
