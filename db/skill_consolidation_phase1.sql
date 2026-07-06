-- Skill/tool consolidation -- phase 1 (2026-06-16)
-- Goal: shrink the single-agent skill catalog so the selector has fewer, fatter,
-- well-bounded skills (reduces the cluster confusion that mis-selected skills).
--
-- Scope of THIS phase (the safest, highest-impact slice):
--   A. Cleanup: disable the 0-tool fabric skill; prune dead PlayWright+Firecrawl
--      orphan tools (dropped servers) and disable those server rows.
--   B. Merge PM planning hierarchy create+read+update -> `pm_planning_hierarchy`
--      (delete kept SEPARATE as a destructive guardrail).
--   C. Merge KeyVault lookup+placeholder_create+placeholder_update ->
--      `keyvault_secret_management` (reference_handoff kept SEPARATE: distinct
--      workflow-handoff purpose).
--
-- Absorbed skills are DISABLED (is_enabled=0), not deleted -- the catalog reads
-- include_disabled=False so they leave selection, but stay recoverable.
-- Keyed by skill `name` (stable); tool unions are copied by tool_id from the
-- absorbed skills (id-safe, avoids tool-name ambiguity).

BEGIN;

-- =====================================================================
-- B. PM planning hierarchy: create + read + update  ->  pm_planning_hierarchy
-- =====================================================================

-- Union the create/update tools into the read survivor (shared tools de-duped by uq_skill_tool).
-- NOTE: skill_tool.is_enabled is NOT NULL with no default -- it MUST be in the column list,
-- otherwise INSERT OR IGNORE silently drops every row on the NOT NULL violation.
INSERT OR IGNORE INTO skill_tool (skill_id, tool_id, is_enabled)
SELECT (SELECT id FROM skills WHERE name = 'pm_planning_hierarchy_read'), st.tool_id, 1
FROM skill_tool st
WHERE st.skill_id IN (
  SELECT id FROM skills WHERE name IN ('pm_planning_hierarchy_create','pm_planning_hierarchy_update')
);

UPDATE skills
SET name = 'pm_planning_hierarchy',
    display_name = 'PM Planning Hierarchy',
    description = 'Use when the user wants to READ, CREATE, or UPDATE the work items inside a project: epics, features, workitems, tasks (including listing active tasks/workitems, and renaming or changing progress/status). Not for DELETING items -- use `pm_planning_hierarchy_delete`. Not for the project container itself (create/rename/delete a project) -- use `pm_project_management`. Not for a project-level snapshot or resolving which project -- use `pm_project_discovery`. Not for hours/time reports -- use `pm_reporting`.',
    instructions = 'Use this skill to READ, CREATE, and UPDATE planning hierarchy objects (epics, features, workitems, tasks). For deletion, use the separate pm_planning_hierarchy_delete skill.

Hierarchy:
- Project contains epics. Epic contains features. Feature contains workitems. Workitem contains tasks.

Tools:
- Read/resolve: pm_project_list, pm_project_get, pm_project_get_full, pm_epic_list, pm_feature_list, pm_workitem_list, pm_task_list
- Active work: pm_report_active_tasks (active tasks), pm_report_active_workitems (active workitems)
- Create: pm_epic_create, pm_feature_create, pm_workitem_create, pm_task_create
- Update: pm_epic_update, pm_feature_update, pm_workitem_update, pm_task_update

ID resolution (all operations):
- Never invent project_id, epic_id, feature_id, workitem_id, or task_id.
- If a needed parent/target ID is missing, resolve it first with the minimal relevant lookup; use pm_project_get_full when inspecting the whole hierarchy is more efficient.
- Do not act on fuzzy name similarity alone; if multiple plausible objects match, ask_user.

Create rules:
- Creating an epic needs project_id; a feature needs epic_id; a workitem needs feature_id; a task needs workitem_id.
- A name is required per created object; description is optional. Do not invent names/descriptions that materially affect correctness.
- When creating a chain in one plan (epic -> feature -> workitem -> task), placeholders may be used only for same-plan mutation chaining.

Update rules:
- Only update fields the user requested; omit unchanged fields; do not send nulls for unchanged fields.
- Workitems and tasks support progress updates; do not invent a progress value the user did not provide or clearly imply.

Read rules:
- Covers read-only inspection; use pm_report_active_tasks / pm_report_active_workitems when the user asks for active tasks/workitems.

Completion integrity:
- Do not claim an object was retrieved, created, or updated unless the corresponding tool succeeded.

Downstream handoff (when useful):
- relevant IDs (project/epic/feature/workitem/task), object names, parent-child relationships, status/progress changes, operation status, unresolved ambiguities. Do not include large raw lists or raw tool payloads.'
WHERE name = 'pm_planning_hierarchy_read';

-- Point the kept delete skill at the merged name.
UPDATE skills
SET description = 'Use ONLY when the user explicitly asks to DELETE epics, features, workitems, or tasks. Not for deleting a whole project -- use `pm_project_management`. Not for creating, reading, renaming, or status/progress changes -- use `pm_planning_hierarchy`.'
WHERE name = 'pm_planning_hierarchy_delete';

UPDATE skills SET is_enabled = 0
WHERE name IN ('pm_planning_hierarchy_create','pm_planning_hierarchy_update');

-- =====================================================================
-- C. KeyVault: lookup + placeholder_create + placeholder_update  ->  keyvault_secret_management
-- =====================================================================

INSERT OR IGNORE INTO skill_tool (skill_id, tool_id, is_enabled)
SELECT (SELECT id FROM skills WHERE name = 'keyvault_secret_lookup'), st.tool_id, 1
FROM skill_tool st
WHERE st.skill_id IN (
  SELECT id FROM skills WHERE name IN ('keyvault_secret_placeholder_create','keyvault_secret_placeholder_update')
);

UPDATE skills
SET name = 'keyvault_secret_management',
    display_name = 'KeyVault Secret Management',
    description = 'Use when the user wants to manage KeyVault secret PLACEHOLDERS and their metadata: list/inspect/resolve placeholders, check existence, create or ensure a placeholder, or update placeholder description/tags/state. Works on metadata only -- never actual secret values. Not for handing off secret references between workflow steps -- use `keyvault_secret_reference_handoff`.',
    instructions = 'Use this skill to look up KeyVault secret metadata and to create, ensure, or update secret PLACEHOLDERS. This skill never reads, stores, or exposes actual secret values -- it works only with metadata/placeholders.

Tools:
- Lookup/resolve: keyvault_secret_list, keyvault_secret_get
- Create/ensure placeholder: keyvault_secret_placeholder_create, keyvault_secret_ensure_placeholder
- Update placeholder metadata: keyvault_secret_placeholder_update

Boundary (critical):
- These tools do not return secret values. Never ask for, echo, or store actual secret values. Secret value provisioning is a separate admin flow.

Lookup rules:
- To list available secrets/placeholders use keyvault_secret_list; for a specific known name use keyvault_secret_get.
- If the exact name is unknown or ambiguous, list first. Prefer exact matches; if multiple plausible matches remain, ask_user -- especially before any create/update.

Create/ensure rules:
- Use keyvault_secret_placeholder_create when the user explicitly wants a new placeholder metadata record.
- Use keyvault_secret_ensure_placeholder when idempotency/safe-retry matters, or a workflow must ensure placeholders exist before deployment/code generation.
- name is required; description and tags are optional. Do not invent placeholder names; if generated code or prior workflow output provides required_secret_placeholders, use those names literally. If the name is unclear, ask_user.
- Verify existence first when a duplicate would be harmful; if the user says create and it already exists, return a clear result instead of duplicating.

Update rules:
- A unique placeholder name is required before update; resolve via lookup if missing/ambiguous.
- Only update requested fields (description, tags, placeholder state); omit unchanged fields; do not null unchanged fields. Never change, rotate, or expose actual secret values.

Completion integrity:
- Do not claim a placeholder was looked up, created, ensured, or updated unless the corresponding tool succeeded.

Downstream handoff (when useful):
- placeholder/secret name, existence status, description, tags, placeholder state, create/ensure/update status, required_secret_placeholders. Never include secret values or raw tool payloads.'
WHERE name = 'keyvault_secret_lookup';

UPDATE skills
SET description = 'Use when another workflow step needs safe secret placeholder names, required_secret_placeholders, or secret_bindings handed off WITHOUT exposing secret values. Not for user-facing placeholder lookup/create/update -- use `keyvault_secret_management`.'
WHERE name = 'keyvault_secret_reference_handoff';

UPDATE skills SET is_enabled = 0
WHERE name IN ('keyvault_secret_placeholder_create','keyvault_secret_placeholder_update');

-- =====================================================================
-- A. Cleanup
-- =====================================================================

-- 0-tool skill: turn off (can be re-enabled once tools are attached).
UPDATE skills SET is_enabled = 0 WHERE name = 'fabric_onelake_workspace_discovery';

-- Prune dead orphan tools from dropped servers (attached to no skill; assistant_tool empty).
DELETE FROM tool
WHERE mcp_server_id IN (SELECT id FROM mcp_server WHERE name IN ('PlayWright','Firecrawl'))
  AND id NOT IN (SELECT tool_id FROM skill_tool);

-- Disable the dropped server rows.
UPDATE mcp_server SET is_enabled = 0 WHERE name IN ('PlayWright','Firecrawl');

COMMIT;
