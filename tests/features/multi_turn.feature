@agent_vendor_management @domain_vendors @module_multi_turn @capability_multi_turn_context @llm_live
Feature: Multi-Turn Count Then List
  Validates that replaying tool_messages from a vendor_count turn gives
  the model enough context to produce a CSV download, even when the
  follow-up is vague. Tests the tool-history round-trip fix for bug 113#9.

  # Scenarios 68–72 from the task appendix

  @tool_vendor_count @tool_vendor_list
  Scenario Outline: Count then list — <clarity>
    When the user asks "How many vendors are in Engineering?"
    And the user follows up with "<follow_up>"
    Then the response includes a CSV download

    Examples:
      | clarity              | follow_up                                  |
      | fully explicit       | Give me a CSV of the vendors in Engineering |
      | clear pronoun        | Can you export them to a CSV?              |
      | medium clarity       | Show me the list                           |
      | vague pronoun        | Can I get those?                           |
      | minimal              | gimme                                      |
