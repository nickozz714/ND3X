# ND3X Platform & Orchestrator — Capabilities

This document describes **what the orchestrator/platform can do** — its functions and
how they are implemented — as *mechanisms*. It is intentionally **not** an instance
catalog: it does not enumerate any deployment's skills, tools, MCP servers, models, or
memories. For the short conceptual overview see the root [`README.md`](../README.md);
this file is the extended reference it links to.

> **Looking for how to *use* the platform** (screen by screen — the agent, skills, tools,
> MCP servers, workflows, AI models, usage, users, tiles)? See the
> [**User & Operator Guide**](guide/README.md). This file is the internals companion to it.

All paths are relative to the repository root (`src/...`). Line numbers are given for
load-bearing constants and may drift; symbol names are the stable anchors.

> **Routing invariant.** Models are never hard-coded. Every LLM call resolves its model
> from a *routing slot*; an unassigned slot means that capability is explicitly *not
> available* (a clear disabled state), never a silent default. See
> [Model routing](#5-model-routing).

---

## 1. Single-agent orchestration

As of the 2026-06 restructure the orchestrator runs a **single agent**. The legacy
router → planner → final-answer pipeline is retired; `SINGLE_AGENT_MODE` defaults to
**on** (`src/component/config.py:122`, env override `SINGLE_AGENT_MODE`,
`config.py:253`). `AssistantOrchestrator.handle_turn` branches on it
(`src/services/assistants/orchestration/orchestrator.py:1411`): when set it calls
`router.run_single_agent(...)`; the `False` branch is the legacy multi-assistant router
path, kept only for rollback.

### Turn flow

```
user turn
   │
   ▼  select_skills()            one cheap LLM call: pick skill(s) by description,
┌──────────────┐                 emit an agent_plan of {step, skill, action}.
│  SELECTION   │  ── mode=answer / ask_user ──▶ short-circuit, return immediately
└──────┬───────┘
       ▼  run_single_agent()     selected skill names + plan injected into payload
┌──────────────┐
│  AGENT LOOP  │  ReAct: reason → call tools (by verified tool_id) → observe → repeat,
│ (pipeline_   │  bounded by per-turn budgets. Mutations/guarded tools pause for
│  runner)     │  confirmation. direct_answer short-circuits the loop.
└──────┬───────┘
       ▼  final-answer writer    composes the user-facing reply from gathered results
┌──────────────┐                 (plain text/markdown — no tool calls).
│   WRITER     │
└──────────────┘
```

### Skill selection (`RoutingService.select_skills`)

`src/services/assistants/orchestration/routing.py` (`select_skills`, ~line 266).
A single LLM call over the **skill catalog** (`runtime_loader.list_agent_skill_catalog()`)
using `SKILL_SELECTOR_INSTRUCTION` + `SKILL_SELECTOR_SCHEMA` (loaded from
`skill_selector.instruction.md` / `skill_selector.schema.json`). It is driven by the
`chat.selection` slot (resolved with `model=None` so `role_to_slot("skill_selector:…")`
applies; falls back to `chat.planner` when `chat.selection` is unassigned). It returns a
`selection` dict whose `mode` is one of:

- `answer` — the selector answered the (trivial) turn itself; short-circuit.
- `ask_user` — clarification needed; short-circuit with one question.
- `plan` (default) — proceed, carrying `selected_skill_names` and an **`agent_plan`** of
  `{step, skill, action}` objects.

`run_single_agent` (`routing.py` ~line 329) places the selected skill names into the
payload as `_selected_skill_names`, runs the agent pipeline, and surfaces
`result["agent_plan"] = {selected_skill_names, steps}` so the plan is visible on the
result.

### The ReAct agent loop (`pipeline_runner.py`)

`src/services/assistants/orchestration/pipeline_runner.py` runs the agent as an
iterative ReAct loop: build planner prompt → LLM call → dispatch the returned `action`
→ (execute tools → observe → re-enter) until it answers or asks. Per-turn budgets come
from `_agent_loop_budgets(is_workflow)` (`pipeline_runner.py:64`):

| Budget | Chat default | Workflow default | Settings key (chat / workflow) |
|---|---|---|---|
| max iterations / step | 8 | 12 | `CHAT_AGENT_MAX_ITERATIONS_PER_STEP` / `WORKFLOW_AGENT_MAX_ITERATIONS_PER_OPERATION` |
| max tool calls / step | 12 | 20 | `CHAT_AGENT_MAX_TOOL_CALLS_PER_STEP` / `WORKFLOW_AGENT_MAX_TOOL_CALLS_PER_OPERATION` |
| max same-error repeats | 2 | 2 | `CHAT_AGENT_MAX_SAME_ERROR_REPEATS` / `WORKFLOW_AGENT_MAX_SAME_ERROR_REPEATS` |
| max wall-clock seconds | 300 | 600 | `CHAT_AGENT_MAX_WALL_CLOCK_SECONDS` / `WORKFLOW_AGENT_MAX_WALL_CLOCK_SECONDS` |

Exceeding a budget ends the turn with a terminal budget state rather than looping
forever. Tool calls are executed by the tool runner keyed on a **verified `tool_id`**:
the planner may only call tools listed in the active skills' manifest, and each call
carries the manifest's `tool_id` so an invented/unauthorised tool cannot run.

### Mutation confirmation & guarded tools

Before executing, the loop checks `tool_guard.is_mutation_tool(...)`. If any call mutates
state, it does **not** run the tool; it builds a human-readable
`build_mutation_confirmation_prompt(...)`, stores a `pending_action`
(`type="mutation_confirmation"`) and returns `mode="confirm_action"` in a
waiting-for-confirmation terminal state. **Guarded tools** (e.g. shell execution) follow
the same pause-and-confirm path in chat mode via `build_tool_confirmation_pending_action`;
in workflow mode they are evaluated against a policy
(`evaluate_workflow_guarded_tool_policy`) instead of prompting a human. On the user's
next turn the orchestrator verifies the pending call's hash
(`verify_pending_tool_confirmation`, see [§7](#7-platform-services)) before executing it
with `confirmed_tool_call_hashes={…}` — so the confirmed call cannot be swapped for a
different one.

### Final-answer writer, `direct_answer`, `ask_user`

- **`direct_answer` short-circuit.** When the planner returns `action="final"` (or
  selection returns `mode=answer`), the loop returns the answer immediately without
  invoking the separate writer — there is nothing left to synthesise.
- **Writer step.** Otherwise, once the agent stops calling tools, a dedicated
  final-answer assistant (`runtime.get_final_answer_runtime_assistant()`) composes the
  user-facing reply from the accumulated tool calls/results/docs. It is a plain
  text/markdown answer (JSON-shaped output is unwrapped or, if malformed, replaced with a
  no-evidence fallback). Its model resolves via the `writer:*` role.
- **`ask_user`.** When the agent needs clarification it returns `mode="ask_user"` with a
  single concise question; the turn ends and the user's reply continues the thread.

---

## 2. Skills → tools mechanism

A **skill** is the unit of capability the agent selects. Its model
(`src/models/skill.py`) carries: `name`, `display_name`, `description` (what selection
matches on), `instructions` (injected guidance), `input_schema` / `output_schema`
(JSON), and attached **files** (`SkillFile`, one-to-many). Tools (`src/models/tool.py`)
carry `name`, `description`, `argument` (the call schema), `output_schema`,
`tool_instructions`, `type`, `is_enabled`; skills and tools are linked through the
`skill_tool` junction (`src/models/skill_tool.py`) with a per-link `is_enabled` flag.

### Progressive injection (`prompt_builder.py`)

`src/services/assistants/prompt_builder.py` renders only what the turn needs:

- `render_skill_manifest(assistant, selected_skill_names)` renders **system skills
  always**, and a non-system skill **only if it was selected** this turn. For each
  rendered skill it inlines `description` + `instructions`, lists attached files as
  *metadata only* (path, content-type, size, checksum — never contents), and cascades to
  `render_tool_manifest(skill.tools)`.
- `render_tool_manifest(tools)` emits per-tool `tool_id`, `name`, `description`,
  `argument` schema and `tool_instructions`, skipping disabled tools.
- `build_planner_prompt(...)` reads `_selected_skill_names` from the payload and embeds
  the resulting manifest under an "active skills and allowed tools" section, with the
  contract that the agent **may only call tools listed there**.

`resolve_effective_selected_skills(...)`
(`src/services/assistants/orchestration/runtime_skill_injection.py`) merges the
selection with auto-injected *runtime* skills (e.g. file-artifact inspection when the
turn references files) to form the effective set. Net effect: a tool is unusable unless
its skill is selected (or system/runtime) **and** both the skill link and the tool are
enabled.

### Cascade-delete integrity contract

The schema guarantees no orphaned rows when a skill is removed:

- `Skill.files` uses `cascade="all, delete-orphan"`, `passive_deletes=True`, backed by
  `SkillFile.skill_id` `ForeignKey(..., ondelete="CASCADE")` — deleting a skill deletes
  its files at the DB level.
- Both FKs in `skill_tool` (`skill_id`, `tool_id`) use `ondelete="CASCADE"` — deleting a
  skill (or a tool) removes the junction rows, but **tool rows themselves persist** so a
  tool can be shared/reattached across skills.

DB-level cascades (not just ORM bookkeeping) mean the integrity holds even under
concurrent mutation or rollback.

---

## 3. Workflows

Workflows run the **same single agent** as chat, on a schedule or on demand, across a
graph of typed operations. Engine: `src/services/workflows/workflow_executor.py`.

### Operation types

Dispatched by string type in `WorkflowExecutor` (~lines 1493–1531):

| Type | What it does |
|---|---|
| `assistant` | Runs a **pre-given-skill agent activity** (the one agent + the operation's locked skills). |
| `for_each` | Iterates an array, spawning a child run per item with `max_concurrency` / `failure_strategy`; emits results as `downstream_handoff.iterables[result_key]`. |
| `sub_workflow` | Runs a child workflow by id (new `WorkflowRun`, `trigger_type="sub_workflow"`, parent linkage). |
| `condition` | Evaluates a boolean (operator/source/path/expected) and skips a dependent branch (`skipped_operation_ids`). |
| `set_variable` | Sets workflow-scoped variables from templates (`variables_set`), propagated downstream via context. |
| `notification` | Emits a `trace` (silent) or `ui` notification (subject/message/severity). |
| `artifact` | Persists text/JSON to `FILES_DIR/workflows/{run_id}/artifacts/` and returns its metadata. |
| `http_request` | Templated HTTP call (GET/POST/…); response modes json/text/status_only. |

### Scheduling vs. the worker

Scheduling and execution are separate concerns:

- **Scheduler** — `WorkflowScheduleTickService.tick_once()`
  (`src/services/workflows/workflow_schedule_tick_service.py`) polls enabled workflows
  with a `schedule_cron` and *enqueues* due runs (`status="queued"`, `trigger_type="cron"`).
- **Worker** — `WorkflowWorker` (`src/services/workflows/workflow_worker.py`) runs its own
  asyncio loop, polling every `interval_seconds` (default 10s), claiming up to
  `batch_size` (default 3) queued runs and executing each via
  `WorkflowExecutor.execute_run(run_id)`, with graceful shutdown.

### Handoff & iterables (`downstream_handoff`)

Operations pass data through a compact `downstream_handoff` dict
(`workflow_executor.py` ~2830): `summary`, `facts`, `artifacts`, `iterables`,
`open_questions`, `output_ref`, `status`. A `for_each` writes its child results to
`iterables[result_key]`; a downstream operation consumes them by sourcing
`downstream_handoff.iterables.<name>` (resolved by `_resolve_for_each_items`). This keeps
hand-offs small — no raw tool dumps or whole documents flow between operations.

### Pre-given-skill agent activities

An `assistant` operation runs through `AssistantOperationRunner`
(`src/services/workflows/assistant_operation_runner.py`). In `SINGLE_AGENT_MODE` it calls
`runtime.get_single_agent_runtime_assistant()` and **ignores `operation_ref_id`** — the
operation's skills are pre-selected by the executor into
`payload["_selected_skill_names"]`, so no per-turn skill selection happens (the cheap
repeated-work path). The executor passes the per-operation model straight through:

```python
result = await self.assistant_runner.run(
    assistant_id=operation.operation_ref_id,
    ...,
    model=config.get("model"),     # workflow_executor.py ~2949
)
```

An empty/absent `config.model` means **use the default routing slot** (`model=None` →
the orchestrator resolves the slot per stage); a pinned model id is used only if it is
registered and enabled. (This is the BE contract that workflow TODO item 15 relies on.)

---

## 4. Cognition / memory

After a turn, the orchestrator can enqueue **system cognition** — interpretation, memory
& belief formation, curiosity/research, and retrieval that feeds future turns. Entry:
`src/services/system_cognition/system_cognition_service.py`, run through
`system_pipeline_runner.py`. Cognition LLM calls default to the `chat.cognition` slot;
retrieval *decisions* use the `memory_decision:*` role. The whole post-turn flow is
**gated on triviality** so routine turns cost nothing.

- **Triviality gate.** `CognitionRouterSystemAssistant` classifies each turn as
  `skip` / `light` / `standard` / `deep` and sets the `run_interpretation` /
  `run_memory` / `run_curiosity` / `run_deep_research` flags. `skip` (greetings,
  confirmations, CRUD, generated content) returns early and runs nothing downstream.
- **Turn interpretation.** `TurnInterpretationSystemAssistant` decomposes a non-trivial
  turn into structured material (topics, decisions, preferences, constraints,
  `candidate_memories`, `researchworthy_topics`, belief seeds) used by every later stage.
- **Memory & belief extraction.** `MemorySystemAssistant` extracts durable memories
  (typed: project / user-preference / architecture-decision / implementation-detail /
  semantic / correction / note) with an importance score and a `thread` / `project` /
  `global` scope. `BeliefSystemAssistant` synthesises structured, reusable reasoning
  capsules from research. Both attach embeddings for later retrieval.
- **Curiosity & research.** `CuriositySystemAssistant` enqueues autonomous research jobs
  (`CuriosityJob`); `ResearchSystemAssistant` runs them (EXA-backed) and feeds results
  into belief synthesis — scaled by `depth` (small/medium/deep).
- **Retrieval search-gate + injection.** A decision assistant
  (`PlannerMemoryRetrievalDecisionAssistant` / router variant in
  `memory_retrieval_decision_assistant.py`) first decides *whether* retrieval would help
  (`should_retrieve`) — false for confirmations/active-state-sufficient turns.
  `MemoryRetrievalPolicy` (`memory_retrieval_policy.py`) then scores candidates by vector
  similarity (lexical fallback) with per-scope thresholds, returning a small top-N.
  `MemoryInjectionService` injects them into the prompt **once per thread** (tracked in an
  injected-once table) so the same memory isn't re-injected.
- **Compaction.** `CompactionService` (`src/services/compaction_service.py`) summarises a
  long thread when it nears the context window, stores the summary
  (`ThreadCompaction`, never deleting the thread), and resets the server-side session so
  later turns continue from `{summary + recent turns}`.

---

## 5. Model routing

Routing is authoritative (`src/services/providers/capability_router.py`): a capability is
usable only when its slot has a model assigned — no silent fallback. Required
capabilities (`chat`, `embeddings`) raise `CapabilityNotConfigured` when unset; optional
ones are simply disabled and skipped.

### Slots

The live slot set is `ALL_SLOTS` (`capability_router.py:30`):

```
chat.planner   chat.selection   chat.cognition
embeddings     transcription    tts     voice    realtime
```

`chat.planner` is the single chat model that runs the agent (selection + planner loop +
the writer). `chat.selection` and `chat.cognition` are optional specialisations.
`role_to_slot` (`src/services/providers/provider_factory.py:40`) additionally names
`chat.router`, `chat.final_answer` and `chat.memory_decision`; the first two are retired
legacy slots and all three fall back to `chat.planner` when unassigned.

### Role → slot resolution & fallback

Every orchestration LLM call carries a `role` string; `role_to_slot(role)` maps it to a
slot:

| Role prefix | Slot |
|---|---|
| `router:` | `chat.router` |
| `writer:` | `chat.final_answer` |
| `skill_selector:` | `chat.selection` (→ `chat.planner` if unassigned) |
| `memory_decision:` | `chat.memory_decision` |
| `cognition:` | `chat.cognition` |
| anything else (`assistant:`, workflow, …) | `chat.planner` |

`resolve_chat(...)` (`provider_factory.py` ~151) applies the priority chain:
1. a **forced** UI model pick for the turn, else
2. the role's slot, else
3. an explicitly requested registered model, else
4. any assigned chat slot (`_any_chat_slot`: `chat.planner` then `chat.cognition`).
When nothing resolves it returns `None` → transparent OpenAI pass-through.

### Simple vs. Advanced

The routing UI exposes a **Simple/Advanced** view over the same slots: Simple assigns one
chat model everywhere (just `chat.planner`); Advanced exposes the optional specialised
slots (`chat.selection`, `chat.cognition`, and the media slots) for per-capability
models. There is no behavioural branch beyond which slots a deployment chooses to fill.

### Per-model capability override

A model's auto-detected capability can be set manually: `ProviderModelUpdate.capability`
(`src/schemas/provider.py`) drives `PUT /admin/providers/models/{model_pk}`
(`src/routers/providers_router.py`), validated against
`{chat, embeddings, transcription, tts, realtime}` and protected by a uniqueness
constraint (`uq_provider_model_capability`) so a `(provider, model, capability)` triple
can't duplicate. This reclassifies models the provider mislabels (e.g. a realtime model
detected as chat).

### Cloud vs. local (Ollama) providers, and the OpenAI key

Provider adapters are built in `provider_factory.py` by `provider_type`: `openai`
(pass-through), `anthropic`, and `openai_compatible` / `ollama` (an OpenAI-compatible
client at a configured `base_url`, e.g. `http://localhost:11434/v1`). So local models are
just another provider behind the same slot machinery.

Credentials are **DB settings, not env**: API keys are stored Fernet-encrypted
(`Provider.api_key_encrypted`) and resolved **lazily** — the OpenAI service is
constructed with an `api_key_provider` callback and only decrypts the key on first
client access (`ProviderRegistryService.get_api_key`), as covered by
`tests/test_openai_lazy_key.py`. With an empty registry the resolvers return `None` and
the platform transparently falls back to OpenAI pass-through.

---

## 6. Voice

Voice is provider-agnostic and slot-driven; each mode is disabled unless its slot is
assigned.

- **Recordings → text (STT).** `POST /main/voice/transcribe`
  (`src/routers/main_routes.py`) for synchronous transcription, and `POST /main/voice/start`
  for async diarised/word-timestamped jobs (`VoiceService`,
  `src/services/voice/voice_service.py`). Both resolve the **`transcription`** slot
  (`build_transcription_provider`).
- **Spoken replies (TTS).** Synthesis runs inside the cascaded voice pipeline
  (`POST /main/voice/cascaded`, `CascadedVoicePipeline` in
  `src/services/providers/voice_pipeline.py`), resolving the **`tts`** slot
  (`build_speech_provider` / `resolve_tts_model`).
- **Turn-based voice chat.** A turn flows record → STT (diarised transcript) → the single
  agent → reply, polled via `GET /main/voice/{thread_id}/{run_id}` and its `/result`
  variant.
- **Live full-duplex realtime.** `POST /main/voice-webrtc/session`
  (`src/routers/voice_webrtc_routes.py`) mints an ephemeral client secret for a browser
  WebRTC session, resolving the **`realtime`** slot (mandatory — a clear error if
  unassigned). The mint POSTs to OpenAI's `/v1/realtime/client_secrets` over a pinned
  **certifi** CA bundle (`voice_webrtc_conversation_service.py`) to survive an empty
  system trust store. The realtime model's tool calls bridge back to ND3X tasks via
  `POST /main/voice-webrtc/task/start` (+ status/result).

---

## 7. Platform services

- **Guarded shell / az-login.** Shell execution (`system__shell_exec`) is a high-risk
  guarded tool (`GUARDED_TOOL_POLICIES`,
  `src/services/assistants/orchestration/guarded_tools.py`): it requires confirmation,
  rejects a blocklist (`DANGEROUS_SHELL_PATTERNS`: `rm -rf /`, `mkfs`, `shutdown`, fork
  bombs…), and is hash-pinned — `tool_call_hash` + `verify_pending_tool_confirmation`
  ensure the confirmed command is exactly the one executed. Azure login
  (`src/services/shell/az_login_service.py`, routes in `src/routers/builtin.py`) supports
  device-code (`POST /builtin/az-login`), service-principal
  (`/builtin/az-login/service-principal`), and `~/.azure` token import
  (`/builtin/az-login/import`), with a `GET /builtin/az-login/status` poll. (The
  device-code flow is parked for remote deployments — see
  [`NOTES-azure-login.md`](../../NOTES-azure-login.md) in the workspace.)
- **Audit / trace.** Every orchestrator action is appended to an append-only ledger
  (`audit_trace_events`, `src/models/audit.py`) by `AuditService`
  (`src/services/audit_service.py`), keyed by `thread_id` / `turn_id` / `seq` with
  type/level/summary/data. `OrchestratorTracer`
  (`src/services/assistants/orchestration/tracing.py`) writes both the in-memory trace and
  the DB rows. Browse/search via `GET /main/audit/search`, `/main/audit/threads`,
  `/main/audit/thread/{thread_id}` (`src/routers/ui_routes.py`).
- **Token usage, budget & dashboard.** Each LLM call is recorded per thread/turn/stage
  (`token_usage`, `src/models/token_usage.py`) by `UsageService`
  (`src/services/usage_service.py`), with per-thread context occupancy
  (`tokens_left`/`near_limit` at 85%), global aggregates (by model/provider/stage), and a
  monthly token/USD budget (`usage_budget`, `over_budget` at 100%). Surfaced via
  `src/routers/usage_router.py` (`/main/usage/thread/{id}`, `/sessions`, `/summary`,
  `/budget`, and `POST /main/usage/thread/{id}/compact`). An in-process
  `OpenAIRateLimiter` (`src/services/openai_usage_control.py`) enforces RPM/TPM per model.
- **Editable agent instruction.** The agent's system instruction is file-backed at
  `src/services/assistants/runtime/system_specs/agent.instruction.md`, loaded by the
  runtime config loader and editable in Agent Settings via
  `GET`/`PUT /assistants/agent/instruction` (`src/routers/assistant_routes.py`; the `PUT`
  requires the expert role and writes the file on disk).
