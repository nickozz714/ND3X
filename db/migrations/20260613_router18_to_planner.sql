-- 20260613_router18_to_planner.sql
--
-- Convert the mislabeled "Repository Documentation Assistant" from router to
-- planner. It stored the planner response schema and behaves like a planner.
-- With response schemas now code-authoritative by assistant_type, leaving it as
-- a router would wrongly apply the canonical router schema + router instruction.
--
-- Idempotent and dialect-agnostic (sqlite + mysql). Run once against the active
-- database (SQLITE_PATH or the configured MySQL database).

UPDATE assistant
SET assistant_type = 'planner'
WHERE assistant_type = 'router'
  AND name = 'Repository Documentation Assistant';
