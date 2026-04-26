@agent_cms_schema_design @domain_cms @module_schema_design @llm_live
Feature: CMS Schema Design
  Validates the CMS schema design agent's ability to create and refine
  content type definitions through natural language conversation.

  # ─────────────────────────────────────────────────────────────────────────
  # Creating content types
  # ─────────────────────────────────────────────────────────────────────────

  @capability_create_content_type @tool_cms_create_content_type
  Scenario: Create a basic content type with multiple fields
    When the CMS admin says "Create a testimonials collection with name, quote, and company"
    Then the agent calls "cms_create_content_type"
    And the tool response indicates success
    And the reply mentions "testimonials"
    And the reply mentions "name"

  @capability_create_content_type @tool_cms_create_content_type
  Scenario: Create a content type with richtext field
    When the CMS admin says "Create a team members collection with name, title, and a bio field that should be richtext"
    Then the agent calls "cms_create_content_type"
    And the reply mentions "bio"
    And the reply mentions "richtext"

  @capability_create_content_type @tool_cms_create_content_type
  Scenario: Create a content type with select field
    When the CMS admin says "Create an articles collection with title, body, and a status field with options draft, published, and archived"
    Then the agent calls "cms_create_content_type"
    And the reply mentions "status"

  # ─────────────────────────────────────────────────────────────────────────
  # Modifying content types
  # ─────────────────────────────────────────────────────────────────────────

  @capability_modify_content_type @tool_cms_update_content_type_schema
  Scenario: Add a field to an existing draft content type
    When the CMS admin says "Create a products collection with name and description"
    Then the agent calls "cms_create_content_type"
    When the CMS admin says "Add a price field as number"
    Then the agent calls "cms_update_content_type_schema" or proposes the change
    And the reply mentions "price"

  @capability_modify_content_type @tool_cms_update_content_type_schema
  Scenario: Make a field optional
    Given a draft content type "Products" exists
    When the CMS admin says "Make the description field optional on Products"
    Then the agent calls "cms_update_content_type_schema" or asks for clarification
    And the reply mentions "optional" or "Products" or asks for clarification

  # ─────────────────────────────────────────────────────────────────────────
  # Unsupported field types
  # ─────────────────────────────────────────────────────────────────────────

  @capability_unsupported_field_handling
  Scenario: Request for image field explains it's not supported
    When the CMS admin says "Create a products collection with name, description, and a product image"
    Then the reply mentions image limitation
    And the reply does not call tool with image field

  @capability_unsupported_field_handling
  Scenario: Request for relationship field explains it's not supported
    When the CMS admin says "Add an author relationship field to the articles collection"
    Then the reply mentions relationship limitation

  # ─────────────────────────────────────────────────────────────────────────
  # Context tracking
  # ─────────────────────────────────────────────────────────────────────────

  @capability_context_switch @tool_cms_set_active_content_type
  Scenario: Agent states which content type it's working on
    When the CMS admin says "Create a testimonials collection with name and quote"
    Then the agent calls "cms_create_content_type"
    And the reply mentions "testimonials"

  # ─────────────────────────────────────────────────────────────────────────
  # Commit guidance
  # ─────────────────────────────────────────────────────────────────────────

  Scenario: Agent directs to UI for committing
    When the CMS admin says "I'm done with the schema, let's publish it"
    Then the reply mentions "commit" or "button"
    And the agent does not call "cms_commit_content_type"

  # ─────────────────────────────────────────────────────────────────────────
  # Label/Name field generation
  # ─────────────────────────────────────────────────────────────────────────

  @capability_create_content_type @tool_cms_create_content_type
  Scenario: Create content type with human-readable field labels
    When the CMS admin says "Create a team members collection with Profile Picture as text and Job Title"
    Then the agent calls "cms_create_content_type"
    And the tool response indicates success
    And the schema uses snake_case names

  # ─────────────────────────────────────────────────────────────────────────
  # Compound operations
  # ─────────────────────────────────────────────────────────────────────────

  @capability_modify_content_type @tool_cms_update_content_type_schema
  Scenario: Delete a field from a multi-field draft
    When the CMS admin says "Create a profiles collection with name, bio, and notes"
    Then the agent calls "cms_create_content_type"
    When the CMS admin says "Remove the notes field from profiles"
    Then the agent calls "cms_update_content_type_schema" or asks for clarification

  # ─────────────────────────────────────────────────────────────────────────
  # Response quality
  # ─────────────────────────────────────────────────────────────────────────

  @capability_response_format
  Scenario: Agent summarizes changes naturally without raw JSON
    When the CMS admin says "Create an events collection with title and date"
    Then the agent calls "cms_create_content_type"
    And the reply does not contain raw JSON schema
    And the reply mentions "events"

  @capability_no_loop
  Scenario: Agent does not repeat successful tool calls
    When the CMS admin says "Create a simple posts collection with title and body"
    Then the agent calls "cms_create_content_type"
    And the tool response indicates success
    And the agent does not call "cms_create_content_type" again
