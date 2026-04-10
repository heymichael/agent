@agent_vendor_management @domain_vendors @module_list @capability_vendor_lookup @tool_vendor_lookup @llm_live
Feature: Vendor Lookup
  Validates the vendor_lookup tool for single-vendor profile queries,
  including name resolution, alias handling, specific field questions,
  full profile requests, and error cases.

  # ── Basic lookup ────────────────────────────────────────────────────────

  Scenario: Lookup vendor by name
    When the user says "Tell me about Adobe"
    Then the agent calls "vendor_lookup"
    And the reply mentions "Adobe"

  Scenario: Lookup vendor by abbreviation
    When the user says "What department is AWS in?"
    Then the agent calls "vendor_lookup"
    And the reply mentions a department

  # ── Full profile ────────────────────────────────────────────────────────

  Scenario: Full profile request
    When the user says "Show me everything you know about Datadog"
    Then the agent calls "vendor_lookup"
    And the reply includes multiple vendor fields

  # ── Specific field question ─────────────────────────────────────────────

  Scenario: Specific field question
    When the user says "When does the Alaska Airlines contract renew?"
    Then the agent calls "vendor_lookup"

  # ── Error cases ─────────────────────────────────────────────────────────

  Scenario: Nonexistent vendor
    When the user says "Tell me about FakeVendor123"
    Then the reply mentions a not-found error

  @capability_disambiguation
  Scenario: Ambiguous vendor name
    When the user says "Tell me about Google"
    Then the reply contains disambiguation candidates
