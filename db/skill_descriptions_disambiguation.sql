-- Skill-description disambiguation (2026-06-16)
-- WHY: the single-agent skill SELECTOR picks skills purely from their `description`.
-- Sibling skills in dense clusters (pm_*, personal_*, execute_*) repeated the same
-- trigger words ("inspect ... planning hierarchy", "active tasks", "health overview")
-- with no negative boundaries, so a small selection model (e.g. gpt-5-mini) picked the
-- wrong sibling. This rewrites the clustered descriptions to add explicit "Use when ..."
-- triggers AND "Not for X -- use `<sibling>` instead" boundaries.
--
-- Keyed by skill `name` (stable across environments) so it can be reviewed in git and
-- re-applied to other DBs. Local/UI-managed skills are not stored in the repo otherwise.
-- Idempotent: re-running just re-sets the same text.

BEGIN;

-- ===== PM cluster (projects vs hierarchy; read vs report; CRUD verbs) =====

UPDATE skills SET description =
'Use when the user wants to FIND or RESOLVE which project they mean, or get a high-level read-only snapshot of a project and its hierarchy. Not for reading individual epics/features/workitems/tasks in detail -- use `pm_planning_hierarchy_read`. Not for creating, renaming, or deleting projects -- use `pm_project_management`.'
WHERE name = 'pm_project_discovery';

UPDATE skills SET description =
'Use when the user wants to create, update, rename, or delete the PROJECT record itself (the top-level container). Not for items inside a project (epics/features/workitems/tasks) -- use the `pm_planning_hierarchy_*` skills. Not for merely finding or inspecting a project -- use `pm_project_discovery`.'
WHERE name = 'pm_project_management';

UPDATE skills SET description =
'Use when the user wants to READ or LIST the work items inside a project: epics, features, workitems, tasks, including which are currently active. Not for a project-level snapshot or resolving which project -- use `pm_project_discovery`. Not for hours/time reports -- use `pm_reporting`.'
WHERE name = 'pm_planning_hierarchy_read';

UPDATE skills SET description =
'Use when the user wants to CREATE new epics, features, workitems, or tasks inside a project planning hierarchy. Not for creating the project container itself -- use `pm_project_management`. Not for editing existing items -- use `pm_planning_hierarchy_update`.'
WHERE name = 'pm_planning_hierarchy_create';

UPDATE skills SET description =
'Use when the user wants to rename, edit, or change the status/progress of EXISTING epics, features, workitems, or tasks. Not for creating new items -- use `pm_planning_hierarchy_create`. Not for deleting items -- use `pm_planning_hierarchy_delete`.'
WHERE name = 'pm_planning_hierarchy_update';

UPDATE skills SET description =
'Use ONLY when the user explicitly asks to DELETE epics, features, workitems, or tasks. Not for deleting a whole project -- use `pm_project_management`. Not for closing/marking-done or other status changes -- use `pm_planning_hierarchy_update`.'
WHERE name = 'pm_planning_hierarchy_delete';

UPDATE skills SET description =
'Use when the user wants aggregated TIME/HOURS reports: logged hours, hours by task/day/code, cross-project time summaries, or counts of active tasks/workitems presented as a report. Not for starting or stopping a timer -- use `pm_time_tracking`. Not for reading the hierarchy structure itself -- use `pm_planning_hierarchy_read`.'
WHERE name = 'pm_reporting';

UPDATE skills SET description =
'Use when the user wants to START or STOP a time entry (clock in/out) on a task or workitem, with hour-code details. Not for viewing or summarizing already-logged hours -- use `pm_reporting`.'
WHERE name = 'pm_time_tracking';

-- ===== personal_* lifestyle/training cluster =====

UPDATE skills SET description =
'Use when the user wants a READ-ONLY overview/context snapshot that combines lifestyle profile, preferences, current goals, and a health summary (e.g. coaching context). Not for creating/editing the profile -- use `personal_profile_onboarding`. Not for managing goals as records -- use `personal_goal_management`. Not for body metrics specifically -- use `personal_body_measurement_tracking`.'
WHERE name = 'personal_lifestyle_context';

UPDATE skills SET description =
'Use when CREATING a lifestyle user, or creating/updating the user profile and workout preferences. Not for just viewing the current setup -- use `personal_lifestyle_context`.'
WHERE name = 'personal_profile_onboarding';

UPDATE skills SET description =
'Use when the user wants to list, review, create, or save structured lifestyle/training GOALS as records. Not for a general context snapshot that merely mentions goals -- use `personal_lifestyle_context`.'
WHERE name = 'personal_goal_management';

UPDATE skills SET description =
'Use when the user wants to log body measurements (e.g. weight) or review body/health measurement metrics specifically. Not for a full lifestyle snapshot -- use `personal_lifestyle_context`. Not for workout sessions -- use `personal_workout_logging`.'
WHERE name = 'personal_body_measurement_tracking';

UPDATE skills SET description =
'Use when the user wants to search or inspect EXERCISE DEFINITIONS, resolve an exercise by name, or create a missing exercise definition. Not for logging a performed workout -- use `personal_workout_logging`.'
WHERE name = 'personal_exercise_library';

UPDATE skills SET description =
'Use when the user wants to LOG a structured strength/gym workout session (exercises, sets, reps, weight, intensity, notes, completion). Not for continuous cardio like running/cycling/swimming -- use `personal_activity_tracking`. Not for reviewing past workouts -- use `personal_workout_history_analytics`. Not for deleting a session -- use `personal_lifestyle_delete`.'
WHERE name = 'personal_workout_logging';

UPDATE skills SET description =
'Use when the user wants to log, update, or list CONTINUOUS activity sessions such as running, walking, cycling, or swimming. Not for structured strength workouts with sets/reps -- use `personal_workout_logging`. Not for deleting a session -- use `personal_lifestyle_delete`.'
WHERE name = 'personal_activity_tracking';

UPDATE skills SET description =
'Use when the user wants to REVIEW the past: workout history, inspect a previous session, analyze training trends, or summarize progress. Not for logging a new session -- use `personal_workout_logging`. Not for forward-looking "what should I train today" advice -- use `personal_training_today_guidance`.'
WHERE name = 'personal_workout_history_analytics';

UPDATE skills SET description =
'Use when the user asks what to train TODAY, whether to rest, or how to adjust upcoming training (forward-looking coaching). Not for reviewing past training data -- use `personal_workout_history_analytics`. Not for building a full multi-day plan -- use `personal_program_creation`.'
WHERE name = 'personal_training_today_guidance';

UPDATE skills SET description =
'Use when the user wants to create and save a structured multi-day training PROGRAM (schedule plus day definitions). Not for logging a single session -- use `personal_workout_logging`. Not for one-day advice -- use `personal_training_today_guidance`.'
WHERE name = 'personal_program_creation';

UPDATE skills SET description =
'Use ONLY when the user explicitly asks to DELETE a logged workout session or activity session. This is the canonical deletion skill for logged sessions. Not for editing/correcting a session -- use `personal_workout_logging` or `personal_activity_tracking`.'
WHERE name = 'personal_lifestyle_delete';

-- ===== EXECUTE microservice-platform cluster =====

UPDATE skills SET description =
'Use to RESOLVE an EXECUTE project and inspect its metadata, runtime status, exposed tools and endpoints, or to refresh discovery. Read-only context gathering. Not for reading failure logs/diagnostics -- use `execute_project_logs_diagnostics`. Not for changing source or redeploying -- use `execute_project_source_update` / `execute_project_redeploy`.'
WHERE name = 'execute_project_discovery';

UPDATE skills SET description =
'Use when the user wants to diagnose problems: inspect runtime failures, error logs, startup issues, or EXECUTE platform health. Not for routine project metadata/status lookup with no problem to debug -- use `execute_project_discovery`.'
WHERE name = 'execute_project_logs_diagnostics';

UPDATE skills SET description =
'Use when the user wants to CREATE (and optionally deploy) a brand-new EXECUTE microservice from a single-file main.py. Only for NEW projects. Not for changing an existing deployed project -- use `execute_project_source_update`.'
WHERE name = 'execute_microservice_create_main_py';

UPDATE skills SET description =
'Use when the user wants to inspect or MODIFY the source files of an EXISTING deployed EXECUTE project. Only for existing projects; never for creating a new one -- use `execute_microservice_create_main_py`. After editing, making changes active is a separate step -- use `execute_project_redeploy`.'
WHERE name = 'execute_project_source_update';

UPDATE skills SET description =
'Use when the user explicitly asks to redeploy, rebuild, restart, or make uploaded source changes active for an existing EXECUTE project. Not for editing the source itself -- use `execute_project_source_update`. Not for stopping or deleting the project -- use `execute_project_lifecycle_management`.'
WHERE name = 'execute_project_redeploy';

UPDATE skills SET description =
'Use ONLY when the user explicitly asks to STOP or DELETE an existing EXECUTE project. Not for restarting/redeploying (which keeps the project) -- use `execute_project_redeploy`.'
WHERE name = 'execute_project_lifecycle_management';

UPDATE skills SET description =
'Use when the user wants to call an HTTP ENDPOINT exposed by a deployed EXECUTE project. Not for invoking a registered runtime TOOL of the project -- use `execute_runtime_tool_execution`.'
WHERE name = 'execute_endpoint_invocation';

UPDATE skills SET description =
'Use when the user wants to invoke a registered runtime TOOL exposed by a deployed EXECUTE project. Not for calling a raw HTTP endpoint -- use `execute_endpoint_invocation`.'
WHERE name = 'execute_runtime_tool_execution';

-- ===== Tighten the over-broad catch-all =====

UPDATE skills SET description =
'Use as a LAST RESORT for ad-hoc CLI/shell, curl/API calls, Azure CLI commands, or runtime file operations only when NO dedicated skill fits. Prefer a specific skill first: Azure login -> `azure_session_management`; EXECUTE project deploy/logs/source -> the `execute_*` skills; Fabric/OneLake -> the `fabric_*` skills; secrets -> the `keyvault_*` skills; file artifact inspection -> `runtime_file_artifact_inspection`. Shell execution is backend-guarded and requires user confirmation.'
WHERE name = 'runtime_cli_automation';

COMMIT;
