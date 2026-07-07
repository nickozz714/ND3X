# ND3X Orchestrator

ND3X is a **single-agent** orchestration runtime. Instead of a monolithic chatbot,
a user turn is handled by one bounded agent loop that selects the skills it needs,
reasons and acts with tools, and then writes the final reply. This README explains
the orchestration concepts and the **system-level** building blocks: system tools,
system skills, and how the agent selects skills.

> **Using the platform?** See the [**User & Operator Guide**](docs/guide/README.md) —
> screen-by-screen docs for the agent, skills, tools, MCP servers, workflows, AI models
> (cost vs performance), usage, users, and every desktop tile.
>
> **Want the mental model?** See [**How ND3X works**](docs/guide/how-it-works.md) — one
> agent, skills→tools, workflows, cognition/memory, model routing, voice, and platform
> services, explained conceptually.

> Models are never hard-coded. Every stage resolves its model from a routing slot;
> an unassigned slot means that capability is explicitly *not available* (a clear
> disabled state), never a silent default.

---

## First-time setup

A fresh checkout needs **no `.env`**. Start the back-end and front-end and the UI
opens a setup wizard that:

1. picks the **base directory** (holds `db/`, `logs/`, `files/`, `ask/`, `voice/`)
   and the **database** — a name inside the base dir (default `ND3X.db`), an
   existing file, or a mysql connection (with **Browse** pickers),
2. creates the first **admin** user,
3. configures **providers + models** and assigns the **routing slots** — models can
   be **auto-discovered** from a configured provider (the *Discover* button →
   `POST /api/setup/discover-models`) instead of typed by hand, and a **light mode**
   is offered for small/local models,
4. **generates and persists** the secrets (JWT + Fernet keys).

On every start an **idempotent first-run bootstrap** ensures the minimum a fresh/empty
database needs to be usable — the Builtin MCP server, the system/runtime skills, the
always-on builtin tools, and a **default agent** — created only-if-missing. It just
guarantees a brand-new install (e.g. a fresh Docker deploy) is not empty; the database
stays authoritative from then on.

### Where configuration lives
- **Almost everything is in the database** — providers/models/slots, the admin
  user, and all operational settings (limits, intervals, logging, file roots,
  MCP, Ollama, agent budgets, …) via the settings registry, editable under
  Application Settings → *System configuration*. At startup these are hydrated into the in-memory config;
  an env var of the same name still overrides (ops escape hatch).
- **Only the irreducible minimum is on disk**, under `<base>/.nd3x/`:
  `bootstrap.json` (base dir + DB connection) and `secrets.json` (chmod 600 —
  `JWT_SECRET` + the Fernet keys). These can't live in the DB they
  connect to / decrypt. A one-line pointer at **`ND3X_HOME`** (default `~/.nd3x`)
  records the active base dir; `ND3X_BASE_DIR` overrides it.

Secrets are generated once and kept stable (the Fernet key encrypts provider
secrets in the DB; the JWT secret signs sessions). To re-run setup, delete
`~/.nd3x/pointer.json`.

### Adopting an existing database
Point the wizard at an existing `ND3X.db`: it inspects the file, and if it already
has an admin the wizard collapses to **Adopt & finish** (the rest of the config is
already in the DB). If that base still has its `secrets.json`, provider keys keep
working; if not, the wizard asks for the original `MAIL_SECRET_KEY` (and optionally
`JWT_SECRET`) so the encrypted provider keys can be decrypted.

**Headless / Docker:** skip the wizard by providing config via the environment —
set a database (`DB_DIALECT`/`SQLITE_PATH` or the `DB_*` mysql fields) plus
`JWT_SECRET` and `MAIL_SECRET_KEY`. Any other setting can be overridden by an env
var of the same name. See `.env.example`. The old `ND3X_BOOTSTRAP_*` user
variables were removed; the first admin is created by the wizard.

---

## How a turn flows

ND3X runs a **single agent**. A user turn is handled by one bounded ReAct agent
loop — there is **no separate router that picks between multiple assistants**. The
one agent decides, step by step, what to do next:

```
user turn
   │
   ▼
┌───────────────────────────────────────────────────────────────┐
│  SINGLE AGENT LOOP   (reason → act → observe, bounded)         │
│                                                                │
│  Each step the agent produces ONE action:                      │
│   • select_skills — load the skill(s) relevant to the turn     │
│                     (and their tools). Usually the first step. │
│   • tool_calls    — call tools, observe the results, continue. │
│   • ask_user      — ask one concise clarifying question.       │
│   • final         — write the user-facing answer and stop.     │
└───────────────────────────────────────────────────────────────┘
   │
   ▼
final answer   (plain text / markdown)
```

Key points:

- **One agent, one loop.** Skill selection is *folded into* the loop as the agent's
  first step (driven by the skill catalog in its prompt), not a separate routing
  stage. Selecting a skill loads that skill's tools for the rest of the turn.
- **Bounded.** The loop has per-turn budgets — max iterations, max tool calls and a
  wall-clock timeout — so it always terminates.
- **Trivial turns stay cheap.** A greeting or simple acknowledgement is answered
  directly (`final`) without selecting skills or calling tools.
- **Only the selected skills** (plus the always-on builtin tools and the
  code-authoritative system-skill contracts) are rendered into the prompt — never
  the whole catalog — so each turn sees just what it needs.
- Multi-step results still travel through a compact **structured handoff**, so a
  later workflow operation gets the essentials without re-reading everything.

---

## System tools

**System tools are the built-in tools the orchestrator provides directly**, in
process, independent of any externally configured tooling. They are always
available to the runtime, are code-authoritative (not editable as data), and are
the trusted primitives the agent loop can rely on — for example inspecting files
and generated artifacts produced during a turn.

This is distinct from *dynamic* tools, which are configured per deployment and
resolved through verified tool identifiers. System tools need no configuration and
are part of the runtime itself.

---

## System skills

**System skills are code-authoritative contracts** that the orchestrator injects
into the relevant prompts on every turn. They cannot be overridden from data, and
they define the rules every assistant step must follow. Conceptually they cover:

- **Response contract** — the structured output shape a planner step must return,
  and what each action (`tool_calls` / `final` / `ask_user`) means.
- **Tool-call contract** — every tool call must carry a verified tool identifier
  and may only use tools from the active manifest; a mutation is never reported as
  done until the tool actually succeeded.
- **Handoff contract** — what to put in the compact structured handoff that flows
  to later steps, and how to keep it small (no raw tool dumps or whole documents).
- **Completion-integrity contract** — never claim a create/update/delete/save
  succeeded unless the corresponding tool call succeeded; how to report failures.
- **File-artifact inspection** — how to work with files and artifacts that a turn
  produced or referenced.

Alongside these, only the *selected* skills for the active assistant are rendered
into the prompt — not the entire catalog — so each step sees just what it needs.

---

## Skill selection

With one agent, the work an old multi-agent "router" would do — choosing a handler —
collapses into the agent's own **skill selection**. On its first step the agent is
shown the **skill catalog** and picks the smallest sufficient set of skills for the
turn (`action='select_skills'`), which loads those skills' tools for the rest of the
loop. It is not a separate stage or a separate model call.

Guiding rules the agent follows:

- **Keep simple turns simple** — answer directly rather than selecting skills or
  asking what the user wants; do not over-ask.
- **Smallest sufficient set** — pick the most specific skill(s) and the fewest
  needed; don't load the whole catalog.
- **Stay in context** — prefer the skills already in play unless the turn clearly
  changed domain.
- **Workflows are tools, not a routing mode** — when a repeatable, multi-step job
  fits, the agent lists and runs a predefined **workflow** through builtin tools
  (`workflow__list` / `workflow__run`), rather than a router deciding it.

---

## Platform capabilities

Beyond the core single-agent loop, ND3X ships a broad set of user-facing
capabilities. The [**User & Operator Guide**](docs/guide/README.md) explains each one
screen by screen; this is the quick index.

| Capability | What it does |
|------------|--------------|
| **Chat attachments + retrieval** | Attach files and images per message (up to 5 × 10 MB). They're indexed into a per-thread search store so the agent retrieves the relevant parts; native provider file-search is used where available. |
| **Context compaction** | Summarises a thread when its context window nears the limit, so long conversations keep running. |
| **Important messages** | Flag a message to force a memory/cognition pass even when the turn looks trivial. |
| **Thread / project delete** | Cleanly delete threads and projects (and optionally their memories, beliefs and curiosity jobs). |
| **Cognition** | Long-term memories, beliefs, and curiosity/research, queryable per thread and per project. |
| **Usage truth-layer** | Per-thread, per-model token & cost accounting, reconciled against what providers actually report. |
| **Agent narration / Auto-mode** | Live step-by-step narration, a propose-a-plan approval flow, and an auto-decider for autonomous turns. |
| **Voice & live transcription** | Transcription (with speaker diarization) and a live meeting lane producing structured notes. |
| **Meeting profiles** | Profiles that shape how meetings become notes (instructions / language / output template / action policy), with a live action-detection lane. |
| **Transfer Hub** | Move files between systems (local, SFTP, Azure Blob / File Share, SharePoint, OneLake, and more) via a visual route builder, on demand or scheduled, with encrypted credentials. |
| **Fabric Data Agents** | Query Microsoft Fabric Data Agents in natural language (service-principal, Azure CLI, bearer-token, or interactive-browser auth). |
| **KeyVault (secrets)** | Encrypted secret store with `.env` bulk import; secrets are used in workflows as `${secret.NAME}` without ever being shown to the model. |
| **Admin toggles** | Enable/disable built-in tools and system skills from the UI, plus Azure-login helpers. |
| **Bundled binaries** | Bundled `ffmpeg` / `pandoc` / `poppler` etc. are put on PATH at startup, degrading gracefully when absent. |

## At a glance

| Concept | One-line summary |
|---------|------------------|
| Single agent | One bounded ReAct loop handles the turn: select skills → call tools → answer. |
| Skill selection | The agent's first step; loads the chosen skills' tools. No separate router. |
| Final answer | The agent writes the user-facing reply from what it gathered. |
| System tools | Built-in, always-available, code-authoritative primitives. |
| System skills | Injected, non-overridable contracts every step obeys. |
| Routing slots | Where each internal step's model comes from — no hard-coded models. |

---

## License

Copyright © 2026 Nick du Chatinier.

ND3X is licensed under the **GNU Affero General Public License v3.0 or later
(AGPL-3.0-or-later)** — see [`LICENSE`](LICENSE). You may use, study, modify and
redistribute it under those terms; if you run a modified version as a network
service, the AGPL requires you to offer that version's source to its users.

## Contributing

Contributions are welcome **through Pull Requests only** — nobody pushes directly
to `main`, and every change needs the maintainer's review and approval (enforced by
branch protection + [`CODEOWNERS`](.github/CODEOWNERS)). Please open an issue first
for anything non-trivial. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Security

Never commit secrets or data (`.env`, `*.db`, `*.sqlite`, dumps — all git-ignored).
Report vulnerabilities privately; see [`SECURITY.md`](SECURITY.md).
| Bootstrap | Idempotent first-run defaults (agent + builtin tools + skills) on an empty DB. |
