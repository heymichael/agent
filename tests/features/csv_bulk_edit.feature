@agent_vendor_management @domain_vendors @module_data_management @capability_csv_bulk_edit @tool_process_vendor_csv @llm_live
Feature: CSV Bulk Edit
  The CSV bulk-edit pipeline accepts a CSV attachment via /chat, processes
  it through the agent, and returns a confirmation dialog or validation
  error. Covers success flows, column/ID/value validation, and edge cases.

  Background:
    Given test vendors exist

  # ── Success cases (scenarios 30–34) ────────────────────────────────────

  Scenario: Single-field department change for 2 vendors
    When the user uploads a CSV changing department to "Marketing" for "Test Vendor Alpha" and "Test Vendor Bravo"
    Then the agent returns a "confirm_csv_batch" pending action
    And the batch summary shows vendor_count 2
    And the batch summary field_counts includes "department_id"

  Scenario: Multiple fields on one vendor
    When the user uploads a CSV changing department and billingFrequency for "Test Vendor Charlie"
    Then the agent returns a "confirm_csv_batch" pending action
    And the batch summary shows vendor_count 1
    And the batch summary has 2 fields in field_counts

  Scenario: Partial columns — only id and purpose
    When the user uploads a CSV changing purpose for "Test Vendor Echo" and "Test Vendor Foxtrot"
    Then the agent returns a "confirm_csv_batch" pending action
    And the batch summary shows vendor_count 2
    And the batch summary field_counts includes "purpose"

  Scenario: Single row subset from test data
    When the user uploads a CSV changing department to "IT" for "Test Vendor Delta"
    Then the agent returns a "confirm_csv_batch" pending action
    And the batch summary shows vendor_count 1

  Scenario: Readonly name column is silently ignored
    When the user uploads a CSV with name and department columns for "Test Vendor Alpha"
    Then the agent returns a "confirm_csv_batch" pending action
    And the batch changes do not include "name"
    And the batch changes include "department_id"

  # ── Column validation errors (scenarios 35–38) ────────────────────────

  Scenario Outline: Column validation — <description>
    When the user uploads a CSV with columns "<columns>" for "Test Vendor Alpha"
    Then the reply mentions "<keyword>"
    And no pending action is returned

    Examples:
      | description                 | columns                      | keyword        |
      | misspelled column name      | id,deparment                 | deparment      |
      | unknown extra column        | id,department,favoriteColor  | favoritecolor  |
      | missing id column           | department,purpose           | id             |
      | upstream-sourced field      | id,paymentMethod             | paymentmethod  |

  # ── ID validation errors (scenarios 39–42) ─────────────────────────────

  Scenario: Malformed UUID — extra character appended
    When the user uploads a CSV with an extra-character UUID for "Test Vendor Alpha"
    Then the reply mentions a UUID or format error
    And no pending action is returned

  Scenario: Truncated UUID — missing last 4 characters
    When the user uploads a CSV with a truncated UUID for "Test Vendor Alpha"
    Then the reply mentions a UUID or format error
    And no pending action is returned

  Scenario: Nonexistent UUID
    When the user uploads a CSV with UUID "00000000-0000-4000-a000-000000000000"
    Then the reply mentions a not-found error
    And no pending action is returned

  Scenario: Empty ID cell
    When the user uploads a CSV with an empty ID cell
    Then the reply mentions a missing ID error
    And no pending action is returned

  # ── Value validation errors (scenarios 43–46) ──────────────────────────

  Scenario Outline: Value validation — <description>
    When the user uploads a CSV setting "<field>" to "<value>" for "Test Vendor Alpha"
    Then the reply mentions "<keyword>"
    And no pending action is returned

    Examples:
      | description              | field             | value              | keyword    |
      | invalid department name  | department        | Zorbology          | zorbology  |
      | invalid owner email      | owner             | nobody@nowhere.com | nobody     |
      | invalid billing enum     | billingFrequency  | biweekly           | biweekly   |
      | invalid date format      | contractStartDate | not-a-date         | date       |

  # ── Edge cases (scenarios 47–49) ───────────────────────────────────────

  Scenario: BOM character from Excel export
    When the user uploads a CSV with a UTF-8 BOM prefix for "Test Vendor Alpha"
    Then the CSV is processed without a column error

  Scenario: Headers only — no data rows
    When the user uploads a CSV with only headers "id,department"
    Then the reply indicates the CSV is empty
    And no pending action is returned

  Scenario: No actual changes — values match current data
    When the user uploads a CSV with unchanged Engineering departments for "Test Vendor Alpha" and "Test Vendor Bravo"
    Then no pending action is returned
    And the reply indicates no changes were detected

  # ── Mode switch (scenario 50) ──────────────────────────────────────────

  @capability_mode_switch
  Scenario: CSV batch then single-vendor modify in same conversation
    When the user does a CSV batch edit for "Test Vendor Delta"
    And then asks to change "Test Vendor Echo" department to Marketing in the same conversation
    Then the follow-up uses modify_vendor not CSV redirect
