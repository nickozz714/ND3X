-- 20260616_cascade_delete_skill_tool_links.sql
--
-- Cascade-delete contract for Skills and Tools (TODO §12). Deleting a Skill or
-- Tool must never leave dangling link rows in assistant_skill / skill_tool /
-- assistant_tool. The application now deletes child link rows explicitly in the
-- same transaction (ToolRepository.delete / SkillRepository.delete), and the FK
-- definitions carry ON DELETE CASCADE. This migration:
--   1. Cleans up any pre-existing dangling rows (the 26 assistant_skill + 86
--      skill_tool rows found in the dev DB, and any assistant_tool equivalents).
--   2. (MySQL only) rebuilds the assistant_tool foreign keys with ON DELETE
--      CASCADE — assistant_skill / skill_tool already declare it.
--
-- Part 1 is idempotent and dialect-agnostic (sqlite + mysql). Part 2 is MySQL
-- DDL; skip it on SQLite (SQLite cannot ALTER a FK and recreates the schema
-- from the ORM models, which already declare the cascade).

-- 1) Remove dangling link rows (LEFT-JOIN-IS-NULL pattern, expressed as NOT IN).
DELETE FROM assistant_skill
WHERE assistant_id NOT IN (SELECT id FROM assistant)
   OR skill_id NOT IN (SELECT id FROM skills);

DELETE FROM skill_tool
WHERE skill_id NOT IN (SELECT id FROM skills)
   OR tool_id NOT IN (SELECT id FROM tool);

DELETE FROM assistant_tool
WHERE assistant_id NOT IN (SELECT id FROM assistant)
   OR tool_id NOT IN (SELECT id FROM tool);

-- 2) MySQL only — rebuild assistant_tool FKs with ON DELETE CASCADE.
-- The original constraint names are auto-generated; adjust the DROP names to
-- match your schema (SHOW CREATE TABLE assistant_tool) before running.
--
--   ALTER TABLE assistant_tool DROP FOREIGN KEY assistant_tool_ibfk_1;
--   ALTER TABLE assistant_tool DROP FOREIGN KEY assistant_tool_ibfk_2;
--   ALTER TABLE assistant_tool
--     ADD CONSTRAINT fk_assistant_tool_assistant
--       FOREIGN KEY (assistant_id) REFERENCES assistant(id) ON DELETE CASCADE,
--     ADD CONSTRAINT fk_assistant_tool_tool
--       FOREIGN KEY (tool_id) REFERENCES tool(id) ON DELETE CASCADE;
