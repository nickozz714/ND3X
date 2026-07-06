# Background agents & parallelism

Verified end-to-end on 2026-07-04 (cloud parent + local parent, qwen2.5:14b on
the planner slot).

## What exists

- `task__create` / `task__status` / `task__result` / `task__list`
  (`services/builtin/tools/background_tasks.py`): Claude-Code-style background
  tasks. A task runs as a detached asyncio task that reuses `agent__dispatch`;
  the parent turn returns immediately with a `task_id`.
- Completed tasks are **drained automatically**: the next agent-loop iteration
  on the owning thread emits a `background_task_completed` trace event, so the
  model (and the chat Steps view) sees the outcome without polling.
- `agent__dispatch` subagents run through the full orchestrator
  (`run_ask_orchestrator`) on their own `subagent-<id>` thread. With no explicit
  `model` argument they resolve the **chat.planner slot** — including light
  mode and the local runtime budget when that slot is a local model.

## Verified behavior

| Case | Result |
| --- | --- |
| Cloud parent (gpt-5.4-mini) + slot-routed subagent (qwen2.5:14b) | task_id in 6.2s; subagent done after 48s; `background_task_completed` drained on the next turn; `task__result` returned the exact output |
| Local parent (qwen2.5:14b) + local subagent | task_id in 68.6s; subagent queued behind the parent's planner hops and completed right after the turn ended |

## Parallelism policy (local models)

Ollama runs with `OLLAMA_NUM_PARALLEL=1`: **all local model calls serialize**
in one queue (max 512 deep). Consequences and the chosen policy:

- "Parallel" subagents on the local model are correct but give **no wall-clock
  win** — they wait for the single slot. The parent's own hops and the
  subagent's hops interleave per call.
- **`OLLAMA_NUM_PARALLEL` costs memory, not context**: on current Ollama each
  parallel slot keeps the full `num_ctx`, so the loaded KV cache scales as
  `num_ctx × parallel`. With `num_ctx=16384` and a 14B model (~12 GB weights),
  bumping to 2 parallel roughly doubles the KV cache — tight on a 25.8 GB
  machine. It does NOT reduce per-request context (so no truncation), it just
  needs more RAM/VRAM.
- Recommended patterns instead:
  1. Keep background work on the queue (default) — correctness over speed.
  2. Give subagents a **different model** via `task__create`/`agent__dispatch`
     `model` argument (e.g. a cloud model, or a second SMALLER local model like
     qwen2.5:7b). Two DIFFERENT loaded models each serve concurrently
     (`OLLAMA_MAX_LOADED_MODELS` permitting), so this gives true parallelism
     without raising num_parallel — memory permitting (e.g. 14B ~12 GB + 7B
     ~5 GB fits in 25.8 GB).
  3. Or raise `OLLAMA_NUM_PARALLEL` if the machine has the RAM for
     `num_ctx × parallel` of KV cache on top of the model weights.

The exact same applies to running **two chats in parallel on the same local
model**: they serialize at Ollama, not in ND3X (ND3X runs each turn as its own
asyncio task with no app-level lock). Point the second chat at a different
model for real concurrency.

## Known limitations

- The task registry is **in-memory**: tasks do not survive a backend restart
  and are only visible via trace notifications / the task tools, not in a
  dedicated FE panel. Persistence + a task panel are candidate follow-ups
  (TODO item 3, third checkbox).
