# Agent-mode framework (CLI-agent providers)

ND3X routing slots run in one of two **execution modes**, decided purely by which
provider is assigned to the slot. This is provider-agnostic: Claude Code is the
first CLI agent, Codex or any other can slot in with no changes to the subsystems.

## The two modes (and why)

- **`model`** — *orchestrator-native*. The orchestrator drives the LLM through its
  own multi-step logic (planner loop, structured pipelines) and enforces structured
  output via `response_format` / `json_schema`. The classic path; works for every
  plain chat model.
- **`agent`** — *CLI-delegated*. The slot resolves to a **CLI-agent provider**
  (`ChatProvider.is_cli_agent = True`). That provider runs its **own** agent loop
  with its own tools; ND3X ships it skills/MCP/tools via the stdio gateway and gets
  the result back through an **output contract** (a tolerantly parsed JSON envelope).
  A CLI agent has `supports_structured_output = False` and ignores `response_format`,
  so anything needing JSON uses an envelope/schema-in-prompt, never a hard schema.

The mode of a slot is `execution_mode.slot_mode(db, slot)` → `"agent"`, `"model"`,
or `None`. It's also surfaced on the routing API (`CapabilityAssignmentRead.execution_mode`)
so the UI can show a per-slot badge.

## No fallbacks (core principle)

The slot assignment **is** the configuration — the system never guesses:

- **Empty slot → the step does not run** (feature off).
- **CLI agent on a slot → agent mode runs** (an envelope-based path exists for it).
- **Plain model on a slot → model mode runs.**
- **Modality/realtime slots** (embeddings, TTS/STT, voice, realtime, image) have no
  CLI-agent interface — a CLI agent is **rejected at assignment time**
  (`set_assignment` raises), never silently substituted at runtime.

## Capability classes

`execution_mode.CAP_CLASS` maps every routing slot (exactly `capability_router.ALL_SLOTS`) to:

- **`outsourceable`** — text/reasoning/deciding: `chat.planner`, `chat.cognition`,
  `chat.memory_decision`, `chat.auto_decision`, `meeting.action_detector`,
  `wizard.*`. These may run in agent mode.
- **`modality`** — `embeddings`, `transcription`, `tts`, `voice`, `realtime`,
  `image_generation`. Orchestrator-only; CLI agents not assignable.

## Where agent mode is wired

- **Chat** (`chat.planner`): `pipeline_runner` branches on `is_cli_agent_type(...)`
  and runs `ClaudeCodeChatAgent` (option A) — clean conversation + ND3X tools via the
  gateway, answer returned as the reply.
- **Workflow step**: the `agent` engine (alias `claude_code`) runs the step via
  `ClaudeCodeOperationRunner`, ending with the handoff envelope.
- **Cognition** (`chat.cognition`): `post_turn` branches on `slot_mode` and runs
  `CognitionAgentRunner` — one blackbox call returns `{decision, memories, beliefs,
  curiosity}`, persisted via the repos.
- **Decision/generator slots** (`chat.memory_decision`, `chat.auto_decision`,
  `wizard.*`): `ask_orchestration_async` injects the `json_schema` into the prompt
  for a CLI agent (it can't enforce `response_format`) and parses tolerantly.
- **Web search** (`chat.web_search`): a CLI agent searches via its own WebSearch.

The machinery all subsystems share lives in `services/providers/cli_agent_runner.py`
(`CliAgentRunner`: provider resolution by capability, the ND3X MCP gateway config
lifecycle, `last_json_object` envelope parsing).

## Adding a new CLI agent (e.g. Codex)

1. Write a `ChatProvider` subclass with `is_cli_agent = True` and a unique
   `provider_type` (it auto-registers on the type registry). Give it the CLI
   specifics (command, auth env, model coercion).
2. If it needs a bespoke run path, subclass `CliAgentRunner` (like
   `CognitionAgentRunner`); otherwise it reuses the existing runners.
3. Import it in `execution_mode.py` so its class is registered for type lookups.

No changes to chat/workflow/cognition/decision code — those branch on the
`is_cli_agent` capability, never on a provider-type string.
