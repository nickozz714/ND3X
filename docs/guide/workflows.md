# Workflows

A **workflow** is a multi-step, background pipeline — like a Microsoft Fabric Data Factory
pipeline. It chains **operations** together, can branch and loop, can run the agent at any
step, and can be scheduled to run on its own. Use workflows for repeatable, multi-step jobs
that you don't want to drive by hand each time (nightly reports, ingest-then-summarise,
multi-stage automations).

```
Workflow ─< Operation        (the design)
WorkflowRun ─< OperationRun   (one execution)
```

## The Workflows tab

Open **AI Workbench → Workflows**. You get a list of workflows; opening one shows the
**builder canvas**. Each workflow has a name, description, optional **input schema** (what
it expects when started), and an optional **schedule**.

## The builder (canvas)

The **canvas** is the visual editor. You add **operations** as nodes and connect them with
**dependencies** (`depends_on`) plus **on-success** / **on-failure** follow-ups. The shape
of the graph defines order and branching:

- Operations with no unmet dependencies run first; downstream ones wait for theirs.
- Independent branches run in parallel; use a **merge** to rejoin them.
- `on_success_follow_up` / `on_failure_follow_up` let you route differently by outcome.

## The editor (Operation Inspector)

Selecting a node opens the **Operation Inspector** — the per-operation editor. Common
fields across all operation types:

- **Name** and **type**.
- **Depends on** / follow-ups (wiring).
- **Execution policy** — **retry policy**, **timeout (seconds)**, and **join strategy**
  (how it waits on multiple upstream branches).
- **Input mapping** — how this operation's inputs are filled from earlier results (see
  *Passing data* below).

## Operation types

| Type | What it does |
|------|--------------|
| **assistant** | Runs **the agent** with a prompt and a **pre-given skill** (no skill-selection step — it's handed the skill, which makes scheduled runs cheaper and deterministic). Optionally override the **model**, the **allowed builtin tools**, and the **light-mode** for this step. |
| **for_each** | Iterates over a list, running a nested set of operations per item. Choose a **failure strategy** (`stop` or `continue`) and where the items come from. |
| **sub_workflow** | Calls another workflow as a step (composition/reuse). |
| **condition** | Branches on a test (see operators below). |
| **set_variable** | Writes one or more **workflow variables** for later steps to read. |
| **new_thread** | Creates a shared conversation thread (stored as a variable). Later **assistant** steps that reference it run in the *same* thread; without it, each assistant step gets its own fresh thread. |
| **merge** | Rejoins parallel branches into one (with an output key). |
| **wait** | Pauses for a **duration (seconds)**. |
| **notification** | Emits a notification — **Workflow log only** (trace), or **UI / system notification** — with a severity (Info / Success / Warning / Error). |
| **http_request** | Calls an external HTTP endpoint. Supports `${secret.NAME}` in the URL/headers/body — the value is injected server-side and masked in the trace, so it never reaches the model. |
| **tool** | Runs a single **builtin tool** directly (by name, with config args) — a lightweight, non-agent step. |
| **artifact** | Produces/stores an artifact output. |
| **fail** | Stops the run with a failure (optional message/error code) — e.g. after a `condition`, to abort deliberately. |

`condition`, `set_variable`, `new_thread`, `merge`, `wait`, `notification`,
`http_request`, `tool`, `artifact`, and `fail` are **self-contained** (their config holds
everything). `assistant`, `for_each`, and `sub_workflow` reference other things (a skill, a
child operation set, or another workflow).

### Condition operators

`equals`, `does not equal`, `contains`, `does not contain`, `is greater than` (and
`… or equal to`), `is less than` (and `… or equal to`), `is one of` / `is not one of`
(array), `exists` / `does not exist`, `is truthy` / `is falsy`.

## The agent inside a workflow

An **assistant** operation runs the same single agent, but with the **skill pre-given** —
it skips selection and goes straight to executing that skill's tools. This is the main
efficiency win for repeated runs. Pick the skill in the inspector. Per step you can also:

- **Model** — pin a registered chat model (default = "use the workbench/agent default").
  A pinned model **overrides the routing slot** for this step (so a step can run on a fast
  cloud model even when the slot points at a local one).
- **Allowed builtin tools** — restrict which always-on builtin tools this step may use
  (empty = all). Skill tools stay available; this just keeps a focused step from wandering
  into unrelated tools.
- **Light mode** — *Auto* (compact prompt for local models) / *On* / *Off*, per step.

Mutating tools still require confirmation (surfaced as a **pending action**). A step is
also bounded by a hard wall-clock ceiling, so it can never run forever.

## Passing data between steps

- **Workflow input** — values supplied when the run starts (shaped by the input schema).
- **Workflow variables** — written by `set_variable`, read anywhere downstream.
- **Operation outputs** — reference a previous operation's result (by key) via the
  **input-mapping** editor; `for_each` can iterate over an upstream operation's output
  list.
- **Prompt variables** — reusable named values you can drop into agent prompts (managed in
  the workflow's Prompt Variables section), so prompts stay templated rather than
  hard-coded.

## Scheduling

A workflow can run on a **cron schedule** (Schedule modal) so it fires automatically, or
be triggered manually. Manual runs that need inputs open a **Run Input** modal first.

## Running & monitoring

- **Run now** starts a run (prompting for inputs if the workflow defines them).
- The **Monitoring drawer** + **Trace timeline** show a run's operations, their status,
  inputs/outputs, and errors as it progresses.
- **Pending actions** — when the agent hits a guarded/mutating tool mid-workflow, the run
  pauses and surfaces a confirmation you approve or reject.
- The **Workflow Runs** tile (a desktop tile) is the **cross-workflow monitor** — every
  run across all workflows in one Fabric-Monitor-style list. See
  [platform-tiles.md](platform-tiles.md).

## Tips

- Keep agent steps deterministic by pre-giving the right skill and, where cost matters, a
  smaller model.
- Use `condition` + follow-ups instead of cramming logic into prompts.
- Use `for_each` with `continue` when partial success is acceptable; `stop` when any
  failure should abort.
- Use `notification` (UI severity) to surface results without watching the run.
