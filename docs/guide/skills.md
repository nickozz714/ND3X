# Skills

A **skill** is a reusable, use-case-scoped capability. It bundles:

- a **description** — *when* the agent should pick it (this is what selection reads),
- **instructions** — *how* to do the job once selected (Markdown),
- a set of **tools** — the functions it may call ([tools.md](tools.md)),
- optional **files** — reference material attached to the skill,
- optional **input/output schemas** and a **priority**.

Skills are the unit the agent selects between. Think of each skill as "one job the agent
knows how to do well."

## Skill types

- **Normal skills** — what you build; selectable by the agent. This is 99% of cases.
- **System skills** — code-authoritative contracts always injected (not selectable;
  Expert-managed). Don't touch unless you know why.
- **Runtime skills** — auto-injected by the orchestrator in specific situations (e.g. file
  inspection when an artifact is present). Not selectable.

## Creating / editing a skill

Open **AI Workbench → Skills**. Each skill has:

| Field | Purpose |
|-------|---------|
| **Name*** | Stable identifier, `snake_case` (e.g. `invoice_reconciliation`). |
| **Display name** | Friendly label for the UI. |
| **Description*** | The selection trigger. **Most important field** (see below). |
| **Instructions*** (Markdown) | The how-to the agent follows once the skill is selected. |
| **Priority** | Tie-breaker / ordering in the catalog. |
| **Source / Source name / Version** | Provenance bookkeeping. |
| **Tools** panel | Which tools this skill can use. A skill with **0 tools cannot execute**. |
| **Files** panel | Reference docs attached to the skill. |
| **Enabled** | Whether the agent may select it (mirrors the Agent Settings toggle). |

## What kind of skills to build

**Scope each skill to a single, recognizable use-case** — the way a person would name a
job. Good: `expense_document_management`, `repo_doc_generation`, `web_research`. Avoid one
giant "do everything" skill (the agent can't tell when to use it) and avoid hair-splitting
one job across many micro-skills (they blur together and get mis-selected).

A practical rule of thumb:

- **One skill per resource + intent group**, not per verb. Prefer a single
  `planning_hierarchy` skill that can read/create/update its items over four separate
  create/read/update/delete skills — fewer, fatter, well-bounded skills select more
  reliably. Keep **destructive** actions (delete/stop) as their own skill *only* if you
  want an explicit safety boundary.
- **Group the tools that are used together** onto the same skill.

### Writing the description (this drives selection)

The agent picks skills purely from descriptions, so make them **disambiguating**:

1. Start with **"Use when the user wants to …"** and list the concrete triggers.
2. Add explicit **negative boundaries** that point to the sibling skill:
   *"Not for X — use `other_skill` instead."*
3. Avoid repeating the same trigger words across sibling skills — overlap is the #1 cause
   of wrong selection (especially with a small selection model).

> Example
> ```
> Use when the user wants aggregated TIME/HOURS reports: logged hours, hours by
> task/day/code, or cross-project time summaries. Not for starting or stopping a timer —
> use `time_tracking`. Not for reading the work-item structure — use `planning_read`.
> ```

### Writing the instructions

Instructions are loaded only when the skill is selected, so be specific and generous:

- List the tools and **when to use each**.
- State **resolution rules** (e.g. "never invent IDs; look them up first").
- State **completion integrity** ("don't claim success unless the tool returned success").
- Note what to hand downstream (useful inside workflows).

## Health flags to watch

The Skills list flags a skill with **No tools linked** — it can be selected but can't
act. Attach the tools it needs.

## Lifecycle

Skills are stored in the database and edited live — changes apply on the next turn. Delete
is cascade-safe (removing a skill cleans up its tool/file links). Prefer **disabling** a
skill over deleting if you might want it back.
