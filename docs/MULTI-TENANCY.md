# Multi-tenancy — design (v1, for review)

Goal: ND3X professionally deployable for multiple **fully separated organizations**.
Inside an org: **teams**, **projects** with cross-team members, and per-project
**skill/tool grants** that are enforced in every session.

Current state (verified 2026-08): every domain table is global — no `org_id`, and
almost nothing has an owner column. Identity = `users` (email + argon2 + roles JSON,
JWT HS256). Migrations = `create_all` + an add-only schema reconciler at boot.
The `/api/main/ask*` chat endpoints carry **no auth** today — closing that is part
of phase 2 regardless of tenancy.

## 1. Core entities (new tables)

```
organizations      id, name, slug (unique), is_active, created_at
org_memberships    id, org_id FK, user_id FK, role: owner|admin|member   (unique org+user)
teams              id, org_id FK, name, description                      (unique org+name)
team_memberships   id, team_id FK, user_id FK, role: lead|member         (unique team+user)
org_invites        id, org_id FK, email, role, token (unique), expires_at, accepted_at
```

- `users` stays **global identity** (one login, member of multiple orgs). The JWT
  gets an `org` claim = the active org; an org-switcher changes it (re-issue token).
- Org roles replace global roles for org-scoped actions; the existing
  `Admin/Expert/User` roles remain only for server operations (setup, providers-infra).

## 2. Projects — promote what exists

`assistant_projects` (today: a thread-grouping) becomes THE project:

```
assistant_projects   + org_id FK, + description, + status (active|archived), + created_by
project_members      id, project_id FK, user_id FK, role: lead|member      (unique proj+user)
                     — any org member, so cross-team by construction
project_skills       project_id FK, skill_id FK        (grant)
project_tools        project_id FK, tool_id FK         (grant)
```

**Enforcement in sessions:** a thread belongs to a project (already true). When a
run starts in project P:
- the skill catalog offered to the agent = P's `project_skills` (∩ enabled) — plugs
  into the existing `allowed_skills` filter in the pipeline runner;
- the toolset exposed (planner + MCP gateway) = P's `project_tools` (∩ enabled);
- no project ⇒ org-default behaviour (configurable: everything vs. nothing).
Board sprints (task #9) later reuse the same grants: an agent session for a sprint
inherits the project's capability set.

## 3. Scoping existing data

Add `org_id` (nullable at first) to every domain table from the inventory —
assistants, threads/messages, skills(+files), tools, mcp_servers(+auth), workflows
(+runs), providers(+models), capability_assignments (slot becomes unique **per org**
→ per-org routing + per-org API keys + per-org budgets), board_item (+ project_id),
secrets, text_docs/chunks, token_usage, usage_budget (per org), meeting_profiles,
system_memories/beliefs/curiosity, audit_trace_events, notification_recipients,
mail_settings, fabric_data_agents, repositories, slash_commands, prompt_variables,
transfer_* .

Stays global: `users`, `log_entries` (server log), server-level
`application_settings` (org-level settings get an `org_id` + composite unique key).
PM + KeyVault HTTP surfaces proxy an external MCP server → scoped at that boundary
in a later phase (the native `secrets` table is scoped normally).

## 4. Request context & enforcement

- `require_user` → extended to `OrgContext { user_id, email, org_id, org_role }`
  resolved from the JWT `org` claim + membership check. One dependency, injected
  in every router.
- Services receive the context (or org_id) explicitly; queries filter on it.
  Rollout per domain (phase order below) rather than a big-bang scoped session.
- `/api/main/ask*` gets `require_user` + org stamping of the created thread.

## 5. Migration path (safe, phased)

1. **Foundation** — new tables via `create_all`; nullable `org_id` columns land via
   the boot reconciler; a bootstrap step creates the **Default Organization**,
   backfills every existing row to it and makes existing users owner/members.
   Nothing changes functionally.
2. **Identity & chat core** — JWT org claim + OrgContext; auth on ask endpoints;
   org filtering on threads/projects/messages/board/cognition/usage.
3. **Configuration domain** — assistants, skills, tools, MCP servers, workflows,
   providers + routing + budgets per org.
4. **Projects & grants** — members, project_skills/project_tools, session
   enforcement (allowed_skills/tools), project management UI.
5. **Org admin FE** — Users page becomes Organization: orgs/teams/members/invites,
   org-switcher in the shell topbar, project screens with grant pickers.

Each phase ships green (backfilled default org keeps single-org installs behaving
exactly as today) — an install that never adds a second org notices nothing.
