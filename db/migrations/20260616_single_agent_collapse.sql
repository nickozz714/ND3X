-- Single-agent collapse: replace the domain (planner) assistants with ONE editable
-- "Agent" row that the chat reads (instruction + attached skills). Keeps the
-- RouterAssistant (retired/dead) and AnswerAssistant (final-answer synthesis) rows.
-- get_single_agent() loads "Agent" by name; the skill catalog = the Agent's skills.
--
-- Idempotent-ish: re-running would create a second "Agent"; guard by checking first.
-- (sqlite shown; MySQL identical apart from datetime().)

BEGIN;

-- 1. drop domain-planner skill links (no reliance on FK cascade in sqlite)
DELETE FROM assistant_skill
WHERE assistant_id IN (SELECT id FROM assistant WHERE assistant_type='planner' AND name != 'Agent');

-- 2. create the single Agent (only if absent)
INSERT INTO assistant (name, description, instruction, schema, assistant_type, routing_tags,
                       model, temperature, priority, is_router_selectable, created_at, updated_at, is_active)
SELECT 'Agent',
       'The single workspace agent. Selects the relevant skill(s) by description and uses their tools to fulfil the request.',
       'You are the assistant for this workspace. You have been given the skill(s) relevant to the user''s request together with their tools. Use them to fulfil the request — call tools by verified tool_id, never claim a mutation succeeded unless its tool call succeeded — then write a clear final answer. Follow the system contracts.',
       '{}', 'planner', '[]', NULL, NULL, 0, 1, datetime('now'), datetime('now'), 1
WHERE NOT EXISTS (SELECT 1 FROM assistant WHERE name='Agent');

-- 3. attach every enabled non-system skill to the Agent
INSERT INTO assistant_skill (assistant_id, skill_id, is_enabled)
SELECT (SELECT id FROM assistant WHERE name='Agent'), s.id, 1
FROM skills s
WHERE s.is_enabled=1 AND COALESCE(s.is_system,0)=0
  AND NOT EXISTS (SELECT 1 FROM assistant_skill a WHERE a.assistant_id=(SELECT id FROM assistant WHERE name='Agent') AND a.skill_id=s.id);

-- 4. repoint workflow assistant-operations to the Agent
UPDATE workflow_operation SET operation_ref_id=(SELECT id FROM assistant WHERE name='Agent')
WHERE operation_type='assistant';

-- 5. collapse: delete the domain planner assistants (keep Agent + Router + Answer)
DELETE FROM assistant WHERE assistant_type='planner' AND name != 'Agent';

COMMIT;
