@agent_vendor_management @domain_vendors @module_table_controls @llm_live @task_63
Feature: Table View Controls
  Validates the set_view_columns and set_table_filters tools that let the
  agent control which columns and row filters are active in the data table.

  # ── Column visibility (C1-C10) ───────────────────────────────────────

  @capability_column_visibility @tool_set_view_columns
  Scenario: C1 — Add a named column group
    When the user says "add the contract columns"
    Then the agent calls "set_view_columns"
    And the agent returns a "set_columns" pending action
    And the view_columns include "contractStartDate"
    And the view_columns include "contractEndDate"
    And the view_columns include "contractLengthMonths"
    And the view_columns include "autoRenew"

  @capability_column_visibility @tool_set_view_columns
  Scenario: C2 — Show all columns
    When the user says "show every single column in the vendor table"
    Then the agent calls "set_view_columns"
    And the agent returns a "set_columns" pending action
    And the view_columns have at least 15 entries

  @capability_column_visibility @tool_set_view_columns
  Scenario: C3 — Reset to default columns
    When the user says "reset the table columns to defaults"
    Then the agent calls "set_view_columns"
    And the agent returns a "set_columns" pending action
    And the view_columns include "accountType"
    And the view_columns include "department"
    And the view_columns include "owner"

  @capability_column_visibility @tool_set_view_columns
  Scenario: C4 — Hide a column from default view
    When the user says "hide department" with visible columns "accountType,department,owner"
    Then the agent calls "set_view_columns"
    And the agent returns a "set_columns" pending action
    And the view_columns do not include "department"

  @capability_column_visibility @tool_set_view_columns
  Scenario: C5 — Hide does not introduce new columns
    When the user says "hide department" with visible columns "accountType,department,owner"
    Then the agent calls "set_view_columns"
    And the agent returns a "set_columns" pending action
    And the view_columns only contain keys from "name,accountType,department,owner"

  @capability_column_visibility @tool_set_view_columns
  Scenario: C6 — Add a single column to current view
    When the user says "also show billing frequency" with visible columns "accountType,department,owner"
    Then the agent calls "set_view_columns"
    And the agent returns a "set_columns" pending action
    And the view_columns include "billingFrequency"
    And the view_columns include "accountType"

  @capability_column_visibility @tool_set_view_columns
  Scenario: C7 — Pinned column always present
    When the user says "show only payment method and billing frequency"
    Then the agent calls "set_view_columns"
    And the agent returns a "set_columns" pending action
    And the view_columns include "paymentMethod"
    And the view_columns include "billingFrequency"

  @capability_column_visibility @tool_set_view_columns
  Scenario: C8 — Invalid column key rejected
    When the user says "add the column called fooBarBaz to the table"
    Then the reply mentions any of "invalid|unknown|not a valid|recognized|not a recognized|doesn't exist|does not exist"

  @capability_column_visibility @tool_set_view_columns
  Scenario: C9 — Unknown table ID rejected
    When set_view_columns is called directly with table "nonexistent" and columns "name"
    Then the tool result contains error "Unknown table"

  @capability_column_visibility @tool_set_view_columns
  Scenario: C10 — Fuzzy column name resolution
    When the user says "add acct type to the table"
    Then the agent calls "set_view_columns"
    And the agent returns a "set_columns" pending action
    And the view_columns include "accountType"

  # ── Row filters (F1-F13) ─────────────────────────────────────────────

  @capability_table_filters @tool_set_table_filters
  Scenario: F1 — Single categorical filter
    When the user says "filter the table to Marketing department" with data pane open
    Then the agent calls "set_table_filters"
    And the agent returns a "set_filters" pending action
    And the table_filters target column "department"
    And the table_filters column "department" has value "Marketing"

  @capability_table_filters @tool_set_table_filters
  Scenario: F2a — Multi-value filter (natural phrasing)
    When the user says "show IT and Marketing departments" with data pane open
    Then the agent calls "set_table_filters"
    And the agent returns a "set_filters" pending action
    And the table_filters column "department" has value "IT"
    And the table_filters column "department" has value "Marketing"

  @capability_table_filters @tool_set_table_filters
  Scenario: F2b — Multi-value filter (explicit phrasing)
    When the user says "filter the table to department IT and department Marketing" with data pane open
    Then the agent calls "set_table_filters"
    And the agent returns a "set_filters" pending action
    And the table_filters column "department" has value "IT"
    And the table_filters column "department" has value "Marketing"

  @capability_table_filters @tool_set_table_filters
  Scenario: F3 — Boolean filter
    When the user says "show vendors with auto-renew enabled" with data pane open
    Then the agent calls "set_table_filters"
    And the agent returns a "set_filters" pending action
    And the table_filters target column "autoRenew"

  @capability_table_filters @tool_set_table_filters
  Scenario: F4 — Has-value filter
    When the user says "show vendors that have an owner" with data pane open
    Then the agent calls "set_table_filters"
    And the agent returns a "set_filters" pending action
    And the table_filters column "owner" has value "*"

  @capability_table_filters @tool_set_table_filters
  Scenario: F5 — Is-empty filter
    When the user says "filter the table to vendors that have no owner assigned" with data pane open
    Then the agent calls "set_table_filters"
    And the agent returns a "set_filters" pending action
    And the table_filters target column "owner"
    And the table_filters column "owner" has value "none"

  @capability_table_filters @tool_set_table_filters
  Scenario: F6 — Clear all filters
    When the user says "clear all table filters" with data pane open
    Then the agent calls "set_table_filters"
    And the agent returns a "set_filters" pending action
    And the table_filters are empty

  @capability_table_filters @tool_set_table_filters
  Scenario: F7 — Non-filterable column rejected
    When the user says "filter where vendor name is Acme" with data pane open
    Then the reply mentions any of "search|not support|not filterable|doesn't support|isn't recognized"

  @capability_table_filters @tool_set_table_filters
  Scenario: F8 — Prefix pattern rejected
    When the user says "show vendors starting with Ac" with data pane open
    Then the reply mentions any of "search|not support|pattern|exact"

  @capability_table_filters @tool_set_table_filters
  Scenario: F9 — Wildcard in filter value rejected
    When set_table_filters is called directly with column "department" and values "Ac*"
    Then the tool result contains error "exact matches"

  @capability_table_filters @tool_set_table_filters
  Scenario: F10 — OR across columns rejected
    When the user says "show vendors where department is Marketing or owner is john@example.com" with data pane open
    Then the reply mentions "or" and does not return a set_filters action

  @capability_table_filters @tool_set_table_filters
  Scenario: F11 — AND across columns
    When the user says "filter to IT department and Business account type" with data pane open
    Then the agent calls "set_table_filters"
    And the agent returns a "set_filters" pending action
    And the table_filters target column "department"
    And the table_filters target column "accountType"

  @capability_table_filters @tool_set_table_filters
  Scenario: F12 — Unknown column in filter rejected
    When set_table_filters is called directly with column "bogusColumn" and values "test"
    Then the tool result contains error "Unknown column"

  @capability_table_filters @tool_set_table_filters
  Scenario: F13 — Unknown table in filter rejected
    When set_table_filters is called directly with table "nonexistent"
    Then the tool result contains error "Unknown table"
