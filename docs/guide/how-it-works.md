# How ND3X works

A short conceptual tour of what happens behind the screens. The rest of the guide is
screen-by-screen; this page is the **mental model** that ties it together. Nothing
here is required reading to *use* ND3X — it's for when you want to know *why* it
behaves the way it does.

## One agent, one bounded turn

ND3X runs a **single agent**. A turn flows in three moves:

1. **Select skills** — one quick step reads your message and picks the **skill(s)**
   whose description fits, or answers trivial messages (a greeting) directly, or asks
   one clarifying question.
2. **Agent loop** — the agent then reasons, calls tools, observes the results, and
   repeats (reason → act → observe) until it has what it needs. The loop is **bounded**
   by budgets (a max number of steps, tool calls, and a wall-clock limit), so it always
   finishes instead of spinning.
3. **Answer** — it writes the final reply from what it gathered (plain text/markdown).
   Simple turns skip straight to the answer.

**Mutations pause for you.** Before running anything that changes state (or a guarded
tool like shell execution), the agent stops and asks for confirmation. The confirmed
action is pinned, so what runs is exactly what you approved — it can't be swapped for
something else. In a workflow, guarded tools are decided by policy instead of a prompt.

## Skills load only what a turn needs

A **skill** bundles a description (what selection matches on), instructions, and a set
of **tools**. Only the skills selected for a turn are loaded into the agent's prompt,
along with their tools — so the prompt stays lean and the agent can *only* call tools
that belong to an active skill. **System skills** (the always-on contracts) and a few
**runtime skills** (e.g. file inspection when your message references a file) are added
automatically. A tool is usable only when its skill is active **and** both the skill
and the tool are enabled.

Removing a skill never leaves loose ends: its files and tool-links are cleaned up, but
shared **tools themselves persist** so they can be reattached to other skills.

## Workflows are the same agent, scripted

A **workflow** runs that same one agent across a graph of **operations** — on demand
or on a schedule. Operation types include running the agent with a pre-given skill,
looping over a list (`for_each`), calling another workflow, branching on a condition,
setting variables, HTTP requests, notifications, artifacts, and more.

- **Scheduling and execution are separate:** a scheduler enqueues due runs; a worker
  claims and executes them in the background.
- **Data passes compactly between steps** — a small hand-off of summary, facts,
  artifacts and list-results, never raw tool dumps or whole documents.
- Agent steps in a workflow use a **pre-given skill** (no per-turn selection — cheaper
  and deterministic), and you can pin the model, scope the tools, and set light mode
  per operation.

See [workflows.md](workflows.md) for the hands-on version.

## Cognition & memory (after the turn)

After a non-trivial turn, ND3X can do background **cognition**: interpret what
happened, extract durable **memories** and beliefs, optionally run **curiosity**
research, and retrieve relevant memories to inform future turns. The whole thing is
**gated on triviality** — greetings, confirmations, and routine CRUD skip it entirely,
so it costs nothing when there's nothing to learn.

Retrieved memories are injected into a thread **once**, so the same memory isn't
repeated. And when a thread gets long, **compaction** summarises it and continues from
`{summary + recent turns}` — so long conversations keep running (see
[usage.md](usage.md)).

## Model routing (no hard-coded models)

Every LLM call resolves its model from a **routing slot**. An unassigned slot means
that capability is **explicitly off** — a clear disabled state, never a silent default.

- **Simple** view assigns one chat model everywhere; **Advanced** exposes optional
  specialised slots (skill selection, cognition, and the media slots).
- **Cloud or local** are the same to the platform: local models (via Ollama) are just
  another provider behind the same slots.
- **Keys are encrypted** in the database (not `.env`) and decrypted only when needed.

Full details in [ai-models.md](ai-models.md).

## Voice

Voice is slot-driven and each mode is off unless its slot is assigned: transcription
(speech → text), spoken replies (text → speech), turn-based voice chat (record → the
agent → reply), and live full-duplex realtime in the browser. See
[meeting-profiles.md](meeting-profiles.md) for how meetings become notes.

## Platform services

- **Guarded shell & Azure login** — high-risk actions (shell execution) require
  confirmation, reject a dangerous-command blocklist, and are pinned to the exact
  approved command.
- **Audit / trace** — every action the agent takes is appended to an audit ledger you
  can browse and search (the Audit Dashboard tile).
- **Usage & budget** — every model call is metered per thread/model, with context
  occupancy, cost, and a monthly budget (see [usage.md](usage.md)).
- **Editable agent instruction** — the agent's always-on instruction is edited in
  Agent Settings (Expert/Admin), see [agent.md](agent.md).
