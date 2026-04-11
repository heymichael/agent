@agent_vendor_management @domain_vendors @module_multi_vendor @llm_live
Feature: Multi-Vendor Sequential Modifications
  Validates sequential modify_vendor calls for 2–5 named vendors,
  the CSV threshold at 6 vendors, fuzzy name resolution, nonexistent
  vendor handling, and explicit CSV generation requests.

  # ── Sequential modify (scenarios 51–57) ───────────────────────────────

  @capability_sequential_modify @tool_modify_vendor
  Scenario: 2 vendors, clean names, 1 field
    When the user says "Set the department to Finance for Adobe and Alaska Airlines"
    Then the agent calls "modify_vendor"

  @capability_sequential_modify @tool_modify_vendor
  Scenario: 3 vendors, 1 misspelling
    When the user says "Change department to IT for Acer American, Accountalent, and Adob"
    Then the agent calls "modify_vendor"

  @capability_sequential_modify @tool_modify_vendor
  Scenario: 5 vendors, clean names
    When the user says "Put Adobe, Alaska Airlines, Air France, AirIndia, and Airvistara into the Administration department"
    Then the agent calls "modify_vendor"

  @capability_sequential_modify @tool_modify_vendor
  Scenario: 3 vendors, multiple fields each
    When the user says "For Alexander Greene set department to Product and owner to Alisha Blechman. For Alexander Krainin set department to Marketing. For Adam Cad set owner to Alec Lesser and department to Engineering"
    Then the agent calls "modify_vendor"

  @capability_csv_threshold @tool_process_vendor_csv
  Scenario: 6 vendors triggers CSV redirect
    When the user says "The following are all Finance department: Adob, Acer Amercan, Accountlent, Alaska Arlines, Air Frans, and 12Twenty"
    Then the reply suggests CSV workflow
    And the agent does not call "modify_vendor"

  @capability_sequential_modify @tool_modify_vendor
  Scenario: 5 vendors, 2 nonexistent
    When the user says "Change department to IT for Adobe, FakeVendor123, Alaska Airlines, NonexistentCorp, and Acer American Corporation"
    Then the agent calls "modify_vendor"
    And the reply reports not-found vendors

  @capability_sequential_modify @tool_modify_vendor
  Scenario: 2 vendors, 1 gibberish
    When the user says "The vendors Andria Lo and adfklsad should be classified as Marketing Dept"
    Then the agent calls "modify_vendor"
    And the reply reports not-found vendors

  # ── Explicit CSV generation (scenarios 58–62) ─────────────────────────

  @capability_csv_generation @tool_generate_vendor_edit_csv
  Scenario: User explicitly asks for CSV with named vendors
    When the user says "I need to update departments for Adobe, Alaska Airlines, and Air France. Can you generate a CSV for me?"
    Then the response includes a CSV download

  @capability_sequential_modify @tool_modify_vendor
  Scenario: Bulk update language with only 2 vendors
    When the user says "I want to do a bulk update on Accountalent and 12Twenty — set both to Engineering department"
    Then the agent calls "modify_vendor"

  @capability_csv_generation @tool_generate_vendor_edit_csv
  Scenario: User asks for spreadsheet with 4 named vendors
    When the user says "Give me a spreadsheet to update the owners for Adobe, AirIndia, Adam Cad, and Alec Lesser"
    Then the response includes a CSV download

  @capability_csv_generation @tool_generate_vendor_edit_csv
  Scenario: Named vendors with gibberish mixed in
    When the user says "Generate a CSV to update departments for Adobe, xkrjwqp, and Alaska Airlines"
    Then the agent returns a CSV download or calls modify_vendor

  @capability_csv_generation @tool_generate_vendor_edit_csv
  Scenario: 4 named vendors, 2 gibberish
    When the user says "Give me a spreadsheet for Air France, blorfsnax, AirIndia, and qqzzymtl"
    Then the response includes a CSV download
