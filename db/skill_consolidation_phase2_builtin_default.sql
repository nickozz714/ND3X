-- Builtin tools available by default — retire the pure wrapper skills (TODO §2).
--
-- All enabled tools on the Builtin MCP server are now injected into the agent's
-- manifest on every turn (runtime_config_loader._load_builtin_always_on_tools →
-- AssistantConfig.tools → prompt_builder "Always-available builtin tools"). So the
-- skills that only *wrapped* those tools add no value and are disabled (catalog uses
-- include_disabled=False, so they leave selection but stay recoverable).
--
-- RETIRE (pure wrappers): text_document_management, runtime_cli_automation,
--   azure_session_management.
-- KEEP (domain skills that encode know-how, still selectable):
--   pdf_document_rendering, declaration_document_management; runtime_file_artifact_inspection
--   is a runtime skill (auto-injected) and is unaffected.
-- The retired skills' tools (text__*, system__shell_exec, system__az_login*) remain
-- fully usable as always-on builtin tools — document mgmt stays a core capability.

UPDATE skills SET is_enabled = 0
WHERE name IN ('text_document_management', 'runtime_cli_automation', 'azure_session_management');
