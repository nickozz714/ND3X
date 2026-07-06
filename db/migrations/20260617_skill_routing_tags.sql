-- Skill tags for the Skills overview (TODO §1).
--
-- Adds a JSON `routing_tags` column to `skills` (organisational tags used for
-- filtering the overview; NOT used for agent skill-selection, which runs on
-- `description`). Reuses the router-era `routing_tags` name already present in the
-- skill schema + FE type — only the column was missing.
--
-- SQLite has no "ADD COLUMN IF NOT EXISTS"; re-running after the column exists will
-- error harmlessly ("duplicate column name") — safe to ignore.

ALTER TABLE skills ADD COLUMN routing_tags JSON;

-- MySQL equivalent (guarded):
--   ALTER TABLE skills ADD COLUMN IF NOT EXISTS routing_tags JSON NULL;
-- Existing rows keep NULL (treated as an empty tag list by the API).
