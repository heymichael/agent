@agent_vendor_management @domain_vendors @module_single_edit @capability_single_vendor_modify @tool_modify_vendor @llm_live
Feature: Single Vendor Modify
  Validates the modify_vendor tool for single-vendor edits, including
  field resolution, fuzzy matching, disambiguation, and error handling.

  # Scenarios 1–12 from the task appendix

  Scenario: Basic vendor field modification
    When the user says "Set Maya Glenn's department to Marketing"
    Then the agent calls "modify_vendor"
    And the agent returns a "confirm_edit" pending action

  Scenario: Fuzzy owner name matching
    When the user says "Change the owner for Rhonda Bender to Huy Hoang"
    Then the agent calls "modify_vendor"
    And the agent returns a "confirm_edit" pending action

  Scenario: Gibberish field values still trigger tool call
    When the user says "Set Rhonda Bender's department to adadfdaadf"
    Then the agent calls "modify_vendor"

  Scenario: LLM does not hallucinate valid values
    When the user says "Set Rhonda Bender's department to xyzzy123"
    Then the agent calls "modify_vendor"
    And the reply does not list fabricated departments

  Scenario: Ambiguous vendor triggers disambiguation
    When the user says "Change Carrie O'Neal's department to Administration"
    Then the agent calls "modify_vendor"

  @capability_disambiguation
  Scenario: Disambiguation selection flow
    When the user says "Change Carrie O'Neal's department to Administration"
    And the reply contains disambiguation candidates
    And the user re-sends with the first candidate UUID
    Then the agent calls "modify_vendor"
    And the agent returns a "confirm_edit" pending action

  Scenario: Multi-vendor request processes both vendors
    When the user says "Change department for B. On the Go and Cheese Plus to Marketing"
    Then the reply acknowledges both vendors

  Scenario: Vendor deletion denied
    When the user says "Delete Rhonda Bender"
    Then the reply denies deletion

  Scenario: Confirmation message after successful edit
    When the user says "Set Maya Glenn's department to Marketing"
    Then the agent calls "modify_vendor"
    And the agent returns a "confirm_edit" pending action

  Scenario: FK field resolution with fuzzy abbreviation
    When the user says "Set Maya Glenn's department to Mktg"
    Then the agent calls "modify_vendor"

  Scenario: Vendor identified by UUID
    When the user says "Set Maya Glenn's department to Marketing"
    And the edit pending action contains a vendor UUID
    And the user re-sends with that UUID
    Then the agent calls "modify_vendor"
    And the agent returns a "confirm_edit" pending action
