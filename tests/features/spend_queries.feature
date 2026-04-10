@agent_vendor_management @domain_spend @module_spend_queries @tool_ask_expense_agent @llm_live
Feature: Spend Queries
  Validates spend detail discovery, drill-downs, summaries, empty
  dimension handling, and hidden vendor resolution in spend context.
  All scenarios exercise the ask_expense_agent delegation tool.

  # ── Spend detail scenarios (13–17) ────────────────────────────────────

  @capability_spend_detail
  Scenario: Service discovery
    When the user says "What AWS services are we spending money on?"
    Then the agent calls "ask_expense_agent"

  @capability_spend_detail
  Scenario: Service breakdown by month
    When the user says "Show me AWS spending broken down by service for the last 3 months"
    Then the agent calls "ask_expense_agent"

  @capability_spend_detail
  Scenario: Subcategory drill-down with filter
    When the user says "What are the S3 usage types and costs for March?"
    Then the agent calls "ask_expense_agent"

  @capability_spend_detail
  Scenario: Empty dimension handling
    When the user says "Which AWS projects are costing us the most?"
    Then the agent calls "ask_expense_agent"
    And the reply gracefully explains no data is available

  @capability_spend_detail
  Scenario: Ranking by subcategory
    When the user says "What is the most expensive AWS usage type this quarter?"
    Then the agent calls "ask_expense_agent"

  # ── Tool selection (scenario 18) ──────────────────────────────────────

  @capability_spend_tool_selection
  Scenario: Summary vs detail tool selection
    When the user says "How much have we spent on AWS total this year?"
    Then the agent calls "ask_expense_agent"

  # ── Spend edge cases (scenarios 22–23) ────────────────────────────────

  @capability_spend_detail
  Scenario: Vendor without detail data
    When the user says "Break down Sidley Austin spending by service"
    Then the agent calls "ask_expense_agent"
    And the reply gracefully explains no data is available

  @capability_spend_detail
  Scenario: Summary fallback with detail check
    When the user says "What are we spending on Sidley Austin and can you break it down further?"
    Then the agent calls "ask_expense_agent"

  # ── Hidden vendor resolution (scenarios 25–26) ────────────────────────

  @capability_vendor_resolution
  Scenario: Hidden vendor — exact name resolves without disambiguation
    When the user says "How much did we spend on AWS this quarter?"
    Then the agent calls "ask_expense_agent"
    And the reply does not ask for disambiguation

  @capability_vendor_resolution
  Scenario: Hidden vendor — alias resolves to non-hidden vendor
    When the user says "Show me Google Cloud spending"
    Then the agent calls "ask_expense_agent"
    And the reply does not ask for disambiguation
