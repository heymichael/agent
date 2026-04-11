@agent_vendor_management @domain_vendors @llm_live
Feature: Vendor List and Filtering
  Validates vendor_list tool usage for filtering, CSV download behavior,
  and that the model correctly maps natural-language queries to tool filters.

  # ── Vendor list queries (scenarios 19–21, 24) ─────────────────────────

  @module_list @capability_vendor_list @tool_vendor_list
  Scenario: 1099 vendors
    When the user says "List all the 1099 vendors"
    Then the agent calls "vendor_list"
    And the response includes a CSV download

  @module_list @capability_vendor_list @tool_vendor_list
  Scenario: Department filter
    When the user says "Show me all vendors in the Marketing department"
    Then the agent calls "vendor_list"

  @module_list @capability_vendor_list @tool_vendor_count
  Scenario: Invalid filter value shows valid options
    When the user says "How many vendors pay by wire? List them."
    Then the reply does not refuse the filter

  # ── CSV download behavior (scenarios 27–29) ───────────────────────────

  @module_csv_download @capability_csv_download @tool_vendor_list
  Scenario: CSV download for 10+ results
    When the user says "List all 1099 vendors"
    Then the agent calls "vendor_list"
    And the response includes a CSV download

  @module_csv_download @capability_csv_download @tool_vendor_list
  Scenario: No CSV for small result sets
    When the user says "Show me vendors in the AI department"
    Then the agent calls "vendor_list"
    And the response does not include a CSV download

  @module_csv_download @capability_csv_download @tool_vendor_list
  Scenario: CSV filename includes filter context
    When the user says "List all ACH vendors"
    Then the response includes a CSV download
    And the CSV filename reflects the applied filter

  # ── No inline listing when CSV present (scenario 63) ──────────────────

  @module_csv_download @capability_csv_download @tool_vendor_list
  Scenario: No inline listing when CSV present
    When the user says "List all 1099 vendors"
    Then the response includes a CSV download
    And the reply does not list vendors inline

  # ── CSV completeness (scenario 64) ────────────────────────────────────

  @module_csv_download @capability_csv_download @tool_vendor_list
  Scenario: CSV contains complete result set
    When the user says "Show me all 1099 vendors"
    Then the response includes a CSV download
    And the CSV has more than 50 data rows

  # ── Filter adherence (scenarios 65–67) ────────────────────────────────

  @module_list @capability_vendor_list_filtering @tool_vendor_list
  Scenario: Owner filter used for "owned by" queries
    When the user says "Show me all vendors owned by Michael Mader"
    Then the agent calls "vendor_list"
    And the reply does not refuse the filter

  @module_list @capability_vendor_list_filtering @tool_vendor_list
  Scenario: Contract end range filter
    When the user says "Show me vendors with contracts expiring in the next 3 months"
    Then the agent calls "vendor_list"
    And the reply does not refuse the filter

  @module_list @capability_vendor_list_filtering @tool_vendor_list
  Scenario: Auto-renew boolean filter
    When the user says "Show me all vendors with auto-renew enabled"
    Then the agent calls "vendor_list"
    And the reply does not refuse the filter

  @module_list @capability_vendor_list_filtering @tool_vendor_list
  Scenario: Owner existence filter
    When the user says "Show me all vendors that have an owner assigned"
    Then the agent calls "vendor_list"
    And the reply does not refuse the filter

  @module_list @capability_vendor_list_filtering @tool_vendor_list
  Scenario: No department filter
    When the user says "Show me vendors that don't have a department assigned"
    Then the agent calls "vendor_list"
    And the reply does not refuse the filter
