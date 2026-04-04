"""
End-to-end tests for the structured tabular data contract (task 143).

Verifies the full pipeline through the live LLM: tool selection, metric
parameter intent, table payload on ChatResponse, and prose summary
behaviour.

Requires:
  - Agent running on localhost:8080 with DEV_AUTH_EMAIL set
  - Cloud SQL Proxy on localhost:5433

No test data setup is needed — analytics queries run against real
vendor/spend data in the dev DB.

Test layers
-----------
  TestTablePayloadOnResponse  — table payload presence / absence
  TestMetricParameter         — LLM metric intent selection
  TestTableProseContract      — LLM prose doesn't reproduce table data
  TestExecutePythonNotOffered — execute_python no longer callable
"""

import re
import requests

BASE = "http://127.0.0.1:8080"
HEADERS = {"Content-Type": "application/json"}


def _chat(prompt: str):
    """Send a single-turn chat message and return the parsed response."""
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "context": {"app": "vendors"},
    }
    resp = requests.post(f"{BASE}/chat", json=payload, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _assert_table_shape(table: dict):
    """Verify a table payload has the required fields and valid types."""
    assert "metric" in table and isinstance(table["metric"], str), (
        f"Missing or invalid metric: {table.get('metric')}"
    )
    assert "columns" in table and isinstance(table["columns"], list), (
        f"Missing or invalid columns: {table.get('columns')}"
    )
    assert "rows" in table and isinstance(table["rows"], list), (
        f"Missing or invalid rows"
    )
    assert "filename" in table and table["filename"].endswith(".csv"), (
        f"Missing or invalid filename: {table.get('filename')}"
    )
    assert len(table["columns"]) >= 2, (
        f"Expected at least 2 columns, got {table['columns']}"
    )


# =========================================================================
# TABLE PAYLOAD ON RESPONSE
# =========================================================================

class TestTablePayloadOnResponse:
    """Verify tables[] appears on ChatResponse for tabular tools."""

    def test_spend_by_vendor_has_table(self):
        """Monthly spend for a vendor should produce a table payload."""
        result = _chat("Show me the monthly spend breakdown for AWS this year")
        tools = result.get("tool_calls_executed", [])
        accepted = {"spend_by_vendor", "spend_detail"}
        assert accepted.intersection(tools), (
            f"Expected one of {accepted}, got: {tools}. Reply: {result['reply'][:300]}"
        )
        tables = result.get("tables", [])
        assert len(tables) >= 1, (
            f"Expected at least 1 table, got {len(tables)}. Reply: {result['reply'][:300]}"
        )
        _assert_table_shape(tables[0])
        assert len(tables[0]["rows"]) >= 1, "Expected at least 1 data row"
        print(f"  PASS: vendor spend returned table with {len(tables[0]['rows'])} rows")

    def test_top_vendors_has_table(self):
        """Top N vendors query should produce a table payload."""
        result = _chat("What are the top 5 vendors by spend this year?")
        tools = result.get("tool_calls_executed", [])
        assert "top_vendors" in tools, (
            f"Expected top_vendors, got: {tools}. Reply: {result['reply'][:300]}"
        )
        tables = result.get("tables", [])
        assert len(tables) >= 1, (
            f"Expected at least 1 table. Reply: {result['reply'][:300]}"
        )
        _assert_table_shape(tables[0])
        assert tables[0]["columns"][0] == "Vendor", (
            f"First column should be 'Vendor', got: {tables[0]['columns']}"
        )
        assert len(tables[0]["rows"]) <= 5, (
            f"Asked for top 5, got {len(tables[0]['rows'])} rows"
        )
        print(f"  PASS: top_vendors returned table with {len(tables[0]['rows'])} rows")

    def test_spend_by_dimension_has_table(self):
        """Spend by dimension should produce a table payload."""
        result = _chat("Break down our spend by payment method this year")
        tools = result.get("tool_calls_executed", [])
        assert "spend_by_dimension" in tools, (
            f"Expected spend_by_dimension, got: {tools}. Reply: {result['reply'][:300]}"
        )
        tables = result.get("tables", [])
        assert len(tables) >= 1, (
            f"Expected at least 1 table. Reply: {result['reply'][:300]}"
        )
        _assert_table_shape(tables[0])
        assert len(tables[0]["columns"]) == 2, (
            f"Expected 2 columns, got: {tables[0]['columns']}"
        )
        print(f"  PASS: spend_by_dimension returned table with {len(tables[0]['rows'])} rows")

    def test_spend_total_has_no_table(self):
        """spend_total returns a single number — no table expected."""
        result = _chat("How much did we spend in total this year?")
        tools = result.get("tool_calls_executed", [])
        assert "spend_total" in tools, (
            f"Expected spend_total, got: {tools}. Reply: {result['reply'][:300]}"
        )
        tables = result.get("tables", [])
        assert len(tables) == 0, (
            f"spend_total should not produce a table, got {len(tables)}"
        )
        print(f"  PASS: spend_total returned no table")

    def test_vendor_lookup_has_no_table(self):
        """vendor_lookup returns a single profile — no table expected."""
        result = _chat("Look up the vendor AWS")
        tools = result.get("tool_calls_executed", [])
        assert "vendor_lookup" in tools, (
            f"Expected vendor_lookup, got: {tools}. Reply: {result['reply'][:300]}"
        )
        tables = result.get("tables", [])
        assert len(tables) == 0, (
            f"vendor_lookup should not produce a table, got {len(tables)}"
        )
        print(f"  PASS: vendor_lookup returned no table")


# =========================================================================
# METRIC PARAMETER INTENT SELECTION
# =========================================================================

class TestMetricParameter:
    """Verify the LLM picks the correct metric based on user intent."""

    def test_bill_count_metric(self):
        """Asking about bills/invoices should use billCount metric."""
        result = _chat("How many bills did we get from AWS each month this year?")
        tools = result.get("tool_calls_executed", [])
        assert "spend_by_vendor" in tools, (
            f"Expected spend_by_vendor, got: {tools}. Reply: {result['reply'][:300]}"
        )
        tables = result.get("tables", [])
        assert len(tables) >= 1, (
            f"Expected a table. Reply: {result['reply'][:300]}"
        )
        assert tables[0]["metric"] == "Bill Count", (
            f"Expected 'Bill Count' metric, got: '{tables[0]['metric']}'"
        )
        print(f"  PASS: bill count metric selected")

    def test_vendor_count_metric_on_dimension(self):
        """Asking how many vendors per dimension should use vendorCount metric."""
        result = _chat("How many vendors do we have in each department?")
        tools = result.get("tool_calls_executed", [])
        has_tabular = any(t in tools for t in ["spend_by_dimension", "vendor_count"])
        assert has_tabular, (
            f"Expected spend_by_dimension or vendor_count, got: {tools}. Reply: {result['reply'][:300]}"
        )
        tables = result.get("tables", [])
        assert len(tables) >= 1, (
            f"Expected a table. Reply: {result['reply'][:300]}"
        )
        assert tables[0]["metric"] == "Vendor Count", (
            f"Expected 'Vendor Count' metric, got: '{tables[0]['metric']}'"
        )
        print(f"  PASS: vendor count metric selected")

    def test_default_spend_metric(self):
        """A generic spend query should default to Spend metric."""
        result = _chat("Show me the monthly breakdown for AWS this year")
        tables = result.get("tables", [])
        assert len(tables) >= 1, (
            f"Expected a table. Reply: {result['reply'][:300]}"
        )
        assert tables[0]["metric"] == "Spend", (
            f"Expected 'Spend' metric (default), got: '{tables[0]['metric']}'"
        )
        print(f"  PASS: default spend metric selected")


# =========================================================================
# PROSE CONTRACT — LLM SHOULD NOT REPRODUCE TABLE DATA
# =========================================================================

class TestTableProseContract:
    """Verify the LLM writes brief summaries without reproducing table data."""

    def test_no_numbers_from_table_in_reply(self):
        """The reply should not contain raw numeric values from the table rows."""
        result = _chat("What are the top 5 vendors by spend this year?")
        tables = result.get("tables", [])
        if not tables or not tables[0]["rows"]:
            print(f"  SKIP: no table data to verify against")
            return

        reply = result["reply"]
        reproduced = []
        for row in tables[0]["rows"]:
            for val in row:
                if isinstance(val, (int, float)) and val > 100:
                    val_str = str(int(val))
                    if val_str in reply:
                        reproduced.append(val_str)

        assert len(reproduced) == 0, (
            f"Reply reproduces {len(reproduced)} numbers from the table: {reproduced[:5]}. "
            f"Reply: {reply[:500]}"
        )
        print(f"  PASS: reply does not reproduce table numbers")

    def test_no_numbered_list_in_reply(self):
        """The reply should not list vendors as a numbered list when a table is present."""
        result = _chat("Top 5 vendors by spend this year")
        tables = result.get("tables", [])
        if not tables:
            print(f"  SKIP: no table returned")
            return

        reply = result["reply"]
        numbered = re.findall(r"^\d+\.\s", reply, re.MULTILINE)
        assert len(numbered) == 0, (
            f"Reply should not contain a numbered list when table is present. "
            f"Found {len(numbered)} items. Reply: {reply[:500]}"
        )
        print(f"  PASS: no numbered list in reply when table present")

    def test_no_markdown_table_in_reply(self):
        """The reply should not contain a markdown table when a table widget is present."""
        result = _chat("Break down GCP spend by project this year")
        tables = result.get("tables", [])
        if not tables:
            print(f"  SKIP: no table returned")
            return

        reply = result["reply"]
        pipe_rows = re.findall(r"^\|.+\|", reply, re.MULTILINE)
        assert len(pipe_rows) == 0, (
            f"Reply contains a markdown table ({len(pipe_rows)} pipe-delimited rows) "
            f"when a table widget is already rendered. Reply: {reply[:500]}"
        )
        print(f"  PASS: no markdown table in reply when table widget present")

    def test_no_bullet_list_of_data_in_reply(self):
        """The reply should not use bullet lists to restate table data."""
        result = _chat("Show me our top 5 vendors by spend this year")
        tables = result.get("tables", [])
        if not tables or not tables[0]["rows"]:
            print(f"  SKIP: no table data to verify against")
            return

        reply = result["reply"]
        bullets = re.findall(r"^[-*]\s", reply, re.MULTILINE)
        assert len(bullets) == 0, (
            f"Reply contains {len(bullets)} bullet points when a table widget is "
            f"present. Reply: {reply[:500]}"
        )
        print(f"  PASS: no bullet list in reply when table present")


# =========================================================================
# SPEND DETAIL ROUTING AND CROSS-TAB
# =========================================================================

class TestSpendDetailRouting:
    """Verify the LLM routes breakdown requests correctly."""

    def test_category_breakdown_uses_spend_detail_not_dimensions(self):
        """Asking to break down by category should call spend_detail, not
        spend_detail_dimensions."""
        result = _chat("Break down GCP spend by category this year")
        tools = result.get("tool_calls_executed", [])
        assert "spend_detail" in tools, (
            f"Expected spend_detail, got: {tools}. Reply: {result['reply'][:300]}"
        )
        assert "spend_detail_dimensions" not in tools, (
            f"Should not call spend_detail_dimensions for a breakdown request, got: {tools}"
        )
        tables = result.get("tables", [])
        assert len(tables) >= 1, (
            f"Expected a table from spend_detail. Reply: {result['reply'][:300]}"
        )
        assert tables[0]["columns"][0] == "Category", (
            f"First column should be 'Category', got: {tables[0]['columns']}"
        )
        print(f"  PASS: used spend_detail (not dimensions) with {len(tables[0]['rows'])} rows")

    def test_cross_tab_uses_single_call_with_secondary_group_by(self):
        """A 2D cross-tab request should use one spend_detail call with
        secondary_group_by, not two separate calls."""
        result = _chat(
            "For GCP this year, show me a table with categories as rows "
            "and projects as columns"
        )
        tools = result.get("tool_calls_executed", [])
        assert tools.count("spend_detail") == 1, (
            f"Expected exactly 1 spend_detail call, got {tools.count('spend_detail')} "
            f"in: {tools}. Reply: {result['reply'][:300]}"
        )
        tables = result.get("tables", [])
        assert len(tables) == 1, (
            f"Expected exactly 1 table (cross-tab), got {len(tables)}. "
            f"Reply: {result['reply'][:300]}"
        )
        table = tables[0]
        assert table["columns"][0] == "Category", (
            f"First column should be 'Category' (row dimension), got: {table['columns']}"
        )
        assert len(table["columns"]) >= 3, (
            f"Cross-tab should have at least 3 columns (dimension + 2 projects), "
            f"got: {table['columns']}"
        )
        print(
            f"  PASS: single spend_detail call produced cross-tab with "
            f"{len(table['rows'])} rows × {len(table['columns']) - 1} projects"
        )


# =========================================================================
# EMPTY RESULTS — NO TABLE, GRACEFUL MESSAGING
# =========================================================================

class TestEmptyResultHandling:
    """Verify graceful handling when queries return no data."""

    def test_invalid_filter_lists_valid_values(self):
        """A bogus project filter should list valid projects — no table."""
        result = _chat(
            "Show me GCP spend this year filtered to the xyznonexistent project"
        )
        tables = result.get("tables", [])
        assert len(tables) == 0, (
            f"Expected no table for invalid filter, got {len(tables)} table(s). "
            f"Reply: {result['reply'][:300]}"
        )
        reply = result["reply"].lower()
        assert "arcade" in reply or "project" in reply, (
            f"Reply should mention valid projects. Reply: {result['reply'][:300]}"
        )
        print(f"  PASS: invalid filter returned valid values, no table")


# =========================================================================
# EXECUTE_PYTHON NOT OFFERED
# =========================================================================

class TestExecutePythonNotOffered:
    """Verify the LLM can no longer call execute_python."""

    def test_service_breakdown_uses_spend_detail(self):
        """A per-service breakdown should use spend_detail, not execute_python."""
        result = _chat("What are our top AWS services by spend this year?")
        tools = result.get("tool_calls_executed", [])
        assert "execute_python" not in tools, (
            f"execute_python should not be called, got: {tools}"
        )
        expected = {"spend_detail", "spend_by_dimension", "spend_by_vendor"}
        assert expected.intersection(tools), (
            f"Expected a structured analytics tool, got: {tools}. Reply: {result['reply'][:300]}"
        )
        print(f"  PASS: used {tools} instead of execute_python")


# =========================================================================

if __name__ == "__main__":
    import sys

    test_classes = [
        TestTablePayloadOnResponse,
        TestMetricParameter,
        TestTableProseContract,
        TestSpendDetailRouting,
        TestEmptyResultHandling,
        TestExecutePythonNotOffered,
    ]

    passed = 0
    failed = 0
    skipped = 0
    errors = []

    for cls in test_classes:
        print(f"\n{'='*60}")
        print(f"  {cls.__name__}")
        print(f"{'='*60}")
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            method = getattr(instance, method_name)
            try:
                method()
                passed += 1
            except Exception as e:
                if "SKIP" in str(e):
                    skipped += 1
                else:
                    failed += 1
                    errors.append(f"  FAIL: {cls.__name__}.{method_name}: {e}")
                    print(f"  FAIL: {method_name}: {e}")

    print(f"\n{'='*60}")
    print(f"  RESULTS: {passed} passed, {failed} failed, {skipped} skipped")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(e)
        sys.exit(1)
    else:
        print("\nAll tests passed!")
