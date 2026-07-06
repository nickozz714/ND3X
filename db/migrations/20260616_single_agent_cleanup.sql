-- Single-agent refactor — DB cleanup (idempotent).
--
-- In the single-agent model the agent's skills come from the skills table directly
-- (all enabled non-system skills); the per-assistant `assistant_tool` links are
-- legacy and already ignored at runtime (the config loader sets config.tools=[]).
-- This clears them.
--
-- NOTE: the assistant-row collapse (reduce the 1 router + 1 final_answer + 11 planner
-- rows to a single "Agent" row, repoint workflow_operation.operation_ref_id to it, and
-- clear assistant_skill) is intentionally DEFERRED to pair with the front-end
-- assistant-UI collapse and to follow a live chat test, so the chat behaviour can be
-- validated in isolation from a data migration. The chat agent does not depend on
-- these rows.

DELETE FROM assistant_tool;

-- MySQL: the same statement applies. If you later drop the table entirely, also remove
-- the Assistant.tools relationship + models/assistant_tool.py first so create_all does
-- not recreate it.
