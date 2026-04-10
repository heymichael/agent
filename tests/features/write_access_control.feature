@agent_vendor_management @domain_vendors @module_acl @capability_write_access_control @llm_live
Feature: Write Access Control
  Verifies department-based write ACL for both modify_vendor and
  process_vendor_csv with a scoped (non-admin) user. The scoped user
  has allowed_departments=['Product'], so Product vendors are editable
  and vendors in other departments are denied.

  Background:
    Given ACL test vendors exist

  # ── Single-vendor modify_vendor access ────────────────────────────────

  @tool_modify_vendor
  Scenario: Allowed vendor opens edit
    When the scoped user says "Open the edit form for ACL Test Vendor Allowed"
    Then the agent calls "modify_vendor"
    And the reply does not mention denial

  @tool_modify_vendor
  Scenario: Denied vendor returns not authorized
    When the scoped user says "Open the edit form for ACL Test Vendor Denied"
    Then the agent calls "modify_vendor"
    And the reply mentions denial

  # ── CSV bulk-edit access ──────────────────────────────────────────────

  @tool_process_vendor_csv
  Scenario: CSV with denied vendor rejected
    When the scoped user uploads a CSV changing purpose for "ACL Test Vendor Denied"
    Then the reply mentions denial
    And no pending action is returned

  @tool_process_vendor_csv
  Scenario: CSV with mixed vendors rejected
    When the scoped user uploads a CSV changing purpose for "ACL Test Vendor Allowed" and "ACL Test Vendor Denied"
    Then the reply mentions denial
    And no pending action is returned

  @tool_process_vendor_csv
  Scenario: CSV with allowed vendor accepted
    When the scoped user uploads a CSV changing purpose for "ACL Test Vendor Allowed"
    Then the reply does not mention denial
