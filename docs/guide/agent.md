# The Agent

ND3X runs as a **single agent**. There is one agent for the whole platform — you do not
create or pick between multiple assistants. Everything the agent can do comes from the
**skills** you turn on; everything it *says* and *how* it behaves comes from its
**instruction**.

## What the agent does on every turn

1. **Reads your message** and decides a mode:
   - *answer* — small talk or a general-knowledge question → it just replies, no skills, no cost beyond one cheap call.
   - *select* — real work → it chooses the **smallest sufficient set of skills** whose descriptions match your request, and drafts a short **plan** (the steps it intends to take).
   - *ask_user* — genuinely ambiguous → it asks one clarifying question.
2. **Loads the chosen skills** — their instructions and their tools become available (progressive disclosure: only the selected skills' tools are loaded, not all of them).
3. **Executes** a reason → call tool → observe loop until it has what it needs.
4. **Writes the final answer.**

Mutating or destructive tool calls are **guarded**: the agent pauses and asks you to
confirm before they run.

Because the agent selects by **description**, the quality of your skill descriptions is
the single biggest lever on whether it picks the right capability. See
[skills.md](skills.md).

## Background tasks

For longer sub-jobs the agent can start a **background task** (also reachable with the
`/task` slash command). The task runs on its own while your turn continues, and its
result is folded back into the thread automatically when it finishes — you don't have
to poll for it. A background task is a full agent run in its own right, so it can select
skills and use tools like any turn.

> On a **local model**, background tasks share the model's single queue, so running
> several at once won't be faster — point a task at a **different** model (a cloud model,
> or a second smaller local one) for real parallelism.

## Agent Settings tab

Open **AI Workbench → Agent Settings**. Because there is only one agent, this opens
straight onto it — there is no list and no "new assistant" button.

### 1. The instruction (the agent's personality & rules)

You edit the agent's system instruction **through the UI**: saving in Agent Settings
persists it (Expert/Admin only). It's the durable, always-on part of the agent.

Use the instruction for durable, always-on guidance: tone, language, formatting
preferences, house rules ("always cite sources", "never expose secrets"), and how to
behave when unsure. Keep it concise — it is injected on every turn, so bloat costs tokens
on every request.

> Do **not** put per-task knowledge here. Task- or domain-specific behaviour belongs in a
> **skill's** instructions, which are only loaded when that skill is selected.

### 2. Enabled skills (the agent's abilities)

The skills you toggle **on** are exactly the agent's usable capabilities — the catalog it
selects from each turn. Toggling a skill **off** removes it from selection immediately
(the catalog is read live from the database per turn, so no restart is needed).

- Turn a skill **off** to retire a capability without deleting it (recoverable).
- A skill with **no tools attached** cannot do anything — the UI flags these; either
  attach tools or leave it off.

### 3. Import / Export

Export and import operate on **the one agent only**: Export produces that agent's config
(including the file-backed instruction); Import updates the same agent and rewrites the
instruction file. There is no multi-assistant export.

## Practical tips

- **Behaviour wrong across the board?** Edit the instruction.
- **Picked the wrong capability?** Fix the relevant skill **descriptions** (boundaries),
  not the instruction. See [skills.md](skills.md).
- **Missing a capability?** Build or enable a skill ([skills.md](skills.md)) and make sure
  it has the right tools ([tools.md](tools.md)).
- **Too expensive / too slow?** Tune which model runs each step in
  [ai-models.md](ai-models.md).
