"""Step definitions for vendor list and filtering scenarios.

Most When/Then steps are inherited from the shared conftest. This file
contains only the vendor-list-specific assertions.
"""

import re

from pytest_bdd import scenarios, then

scenarios("../features/vendor_list.feature")


# ── Then — CSV content assertions ────────────────────────────────────────


@then("the reply does not list vendors inline")
def assert_no_inline_listing(context):
    reply = context["result"]["reply"]
    numbered = re.findall(r"^\d+\.\s", reply, re.MULTILINE)
    bulleted = re.findall(r"^[-•]\s", reply, re.MULTILINE)
    assert len(numbered) == 0, (
        f"Reply should not contain a numbered list when CSV is present. "
        f"Found {len(numbered)} numbered items. Reply: {reply[:500]}"
    )
    assert len(bulleted) == 0, (
        f"Reply should not contain a bulleted list when CSV is present. "
        f"Found {len(bulleted)} bullet items. Reply: {reply[:500]}"
    )


@then("the CSV has more than 50 data rows")
def assert_csv_row_count(context):
    downloads = context["result"].get("downloads", [])
    assert len(downloads) >= 1, "No CSV download found"
    csv_content = downloads[0]["content"]
    csv_lines = [line for line in csv_content.strip().splitlines() if line.strip()]
    csv_row_count = len(csv_lines) - 1  # subtract header
    assert csv_row_count > 50, (
        f"CSV should contain all matching vendors (>50), but only has {csv_row_count} rows"
    )


@then("the CSV filename reflects the applied filter")
def assert_csv_filename(context):
    downloads = context["result"].get("downloads", [])
    assert len(downloads) >= 1, "No CSV download found"
    filename = downloads[0].get("filename", "")
    assert filename, "CSV download has no filename"
    assert "vendor" in filename.lower() or "csv" in filename.lower(), (
        f"Expected filter context in filename, got: {filename}"
    )
