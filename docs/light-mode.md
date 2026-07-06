# Planner light mode

Light mode sends a **compact planner prompt** for small/local models. Their step
latency is dominated by prompt ingestion (prefill), so prompt size is the main
lever: the full planner context is ~29K characters (~8.5K tokens), which costs
~100 seconds *per agent hop* on a 14B model on Apple Silicon.

## How it is selected

- Per model: **AI Models → Routing → prompt mode** (`prompt_mode` on
  `provider_models`): `full` | `light` | unset.
- Unset = **auto**: light when the model belongs to a local provider
  (`providers.is_local` / `provider_models.is_local`), full otherwise.
- Per turn override: `payload["_light_mode_session"]` (true/false) wins over the
  per-model setting.
- The pipeline resolves the flag once per turn into `payload["_light_mode"]`
  (`pipeline_runner._resolve_light_mode`), like `_extra_guidance`.

## What changes versus full mode

| Prompt part | Full mode | Light mode |
| --- | --- | --- |
| Always-on builtin manifest (system instructions) | full JSON arg schema + rules per tool (~12.9K chars) | name + description + one-line param list (`cmd*, cwd`; `*` = required) |
| `orchestrator_*` system-skill contracts | 3 verbose contract blocks (~6.4K chars) | replaced by the distilled `agent.instruction.light.md` core contract (~1.5K) covering: JSON-only output, the four actions, tool_id/exact-name discipline, never-invent, never-claim-success-without-tool-result, response_mode, `say` |
| Other system/runtime skills | instructions + full tool schemas | instructions kept, tool schemas → brief param list |
| SELECTED skills' tools | full arg schemas | full arg schemas (unchanged — the agent is actively calling these) |
| Planner JSON schema | full schema dump (~2.6K chars) | one-line field list (`_light_schema_summary`); the real schema is still **enforced** by the provider's structured outputs (`format` on Ollama) |
| Capabilities primer | included | omitted |
| Chat flow rules (act-vs-ask, narration, propose_plan) | verbose | 2-line version (the core contract covers narration and ask_user) |
| Skill catalog, memory block, conversation state, fabric agents, payload | unchanged | unchanged |
| Workflow rule (never ask the user) | unchanged | unchanged |

Measured effect (default workspace, empty thread): total planner context
~29.5K → ~9K characters (~3x less prefill per hop).

## Trade-offs / known limitations

- Tools outside the selected skills expose only parameter *names*, not full
  schemas. If a small model gets an argument shape wrong, the normal tool-error
  recovery loop corrects it; select the skill to get full schemas.
- The distilled contract intentionally drops the long-tail edge-case rules of
  the `orchestrator_*` contracts. Cloud/full-mode behavior is unchanged.
- DB edits to the `orchestrator_*` skill instructions do NOT affect light mode
  (it uses the code-shipped `agent.instruction.light.md`).
