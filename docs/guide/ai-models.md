# AI Models (routing, cost & performance)

*(Admin only.)* The **AI Models** tab decides **which LLM runs each part of the system**.
This is where you trade off **cost** vs **performance**. It has three sub-tabs:

- **Cloud** — cloud providers (Claude, OpenAI, …): add API keys and their models.
- **Local** — local models on this machine via **Ollama** (hardware check,
  recommendations, one-click deploy).
- **Routing** — assign a provider+model to each **capability slot**. Cloud and local
  models can be mixed freely.

## Cloud

Add a provider, paste its API key, then **Discover** to pull its available models (or add a
model by ID). Each model has a **capability** (chat / embeddings / transcription / tts /
realtime) — you can **manually override** a model's capability here if it was
auto-classified wrong (e.g. a realtime model mislabelled as chat). The OpenAI key entered
here is also what the realtime voice mint uses.

### Azure AI Foundry

The **Azure AI Foundry** preset connects an Azure Foundry (or Azure OpenAI) resource through
its v1 OpenAI-compatible API. Three things to know:

- **Base URL** = your resource endpoint, e.g. `https://<resource>.openai.azure.com` (the
  `/openai/v1` route is appended automatically; `.services.ai.azure.com` also works).
- **Model id = your DEPLOYMENT name**, not the model name — deploy a model in the Foundry
  portal first, then register that deployment name here (**Discover** lists them).
- **API key** = the Azure resource key. This covers Azure OpenAI models (GPT-4o/4.1/
  o-series) and Azure-sold open models (DeepSeek, Grok, Llama, Phi, Mistral) alike.

### Claude Code (CLI)

The **Claude Code (CLI)** preset runs the local `claude` command-line agent on your Claude
subscription — **no per-token API cost**. It's a *CLI-agent* provider: assigned to a slot it
runs in **agent mode** (its own loop with its own tools), and ND3X hands it the ND3X skills,
MCP servers and tools via the gateway. A good fit for `chat.planner` or `chat.background`.

- **Auth** = paste the token from `claude setup-token` into the **API key** field (stored
  encrypted). Leave it empty only to fall back to the host machine's own `claude` login.
  ND3X strips `ANTHROPIC_API_KEY` from the CLI's environment so it always uses the
  subscription, never per-token billing.
- **No base URL / model list** — pick a tier alias (`opus` / `sonnet` / `haiku`).

**Running ND3X in Docker.** The `claude` CLI ships **inside the ND3X image** (installed via
`npm i -g @anthropic-ai/claude-code`), so the provider works in a container out of the box —
you only supply the **setup-token** as the API key. You do **not** run `claude` on the host
and "point" the container at it: the provider *spawns the CLI as a subprocess*, so the binary
must live where the backend runs. (Mounting the host binary doesn't work across platforms —
a macOS `claude` can't run in a Linux container.) The setup-token is portable: generate it
once on any machine with `claude setup-token` and paste it in — no host login needed in the
container.

## Local

Shows your machine's hardware, **recommends** models that fit, and can **deploy** a local
model via Ollama in one click. Local models then appear in Routing exactly like cloud ones
— so you can run cheap/offline steps locally and reserve cloud models for the hard parts.

## Routing — the slots

Routing maps each **capability slot** to a model. Two display modes:

- **Simple** — you set just the **Agent model** (and the required Embeddings model); every
  chat sub-step falls back to the Agent model. One model runs everything.
- **Advanced** — you can give each sub-step its own (usually cheaper) model.

| Slot | Mode | What runs here |
|------|------|----------------|
| **Agent model** (`chat.planner`) | Simple | The main brain: one loop that chooses skills, plans, uses tools, and writes the answer. **Required.** |
| **Background agents** (`chat.background`) | Advanced | The model for **dispatched / background subagents** (`agent__dispatch`, `task__create`). Set a different — e.g. cheaper, or a separate cloud — model so background jobs don't queue behind your foreground turn. Empty → background dispatch is **refused** (no fallback); a per-call model override still works. A CLI-agent model here runs background jobs in **agent mode**. |
| **Memory & learning** (`chat.cognition`) | Advanced | Reads finished conversations and records durable memories/beliefs. Empty → no long-term memory written. |
| **Memory lookup decision** (`chat.memory_decision`) | Advanced | Tiny model judging whether a message is worth searching saved memories. Empty → memory lookup skipped. |
| **Auto-decider** (`chat.auto_decision`) | Advanced | In Auto mode, answers the agent's questions / approves plans on your behalf so it runs unattended. A nano model is ideal. Empty → falls back to cognition, then the Agent model. |
| **Meeting action detector** (`meeting.action_detector`) | — | During live meetings, watches the transcript and decides when to run a quick look-up. Runs every few seconds — use a nano model. Empty → meeting actions off. |
| **AI wizards** (`wizard.generator`, plus `wizard.skill` / `wizard.workflow` / `wizard.meeting_profile`) | — | The "Generate with AI" wizards. `wizard.generator` is the shared default; the per-wizard slots override it. Empty → the wizards are off. |
| **Search index** (`embeddings`) | Simple | Turns notes/documents into vectors for meaning-based search. **Required.** Changing it later requires a re-index. |
| **Recordings → text** (`transcription`) | — | Speech-to-text for uploaded/recorded audio. Empty → no transcription. |
| **Spoken replies** (`tts`) | — | Text-to-speech for answers. Empty → text only. |
| **Voice chat (take turns)** (`voice`) | — | Turn-based voice: speak → transcribe → a chat model answers → read back. Empty → off. |
| **Live voice (full-duplex)** (`realtime`) | — | Real-time two-way spoken conversation (the Live Voice button). Needs a dedicated realtime model. Empty → off. |
| **Image generation** (`image_generation`) | — | The `image__generate` tool / `/img` command. Works with OpenAI, Gemini image models, or an OpenAI-compatible images endpoint. Empty → off. |

**Required vs optional:** required slots (Agent model, Embeddings) **error and stop** when
empty. Optional slots simply **disable their feature** when empty — there is never a silent
hard-coded fallback to some default model. "No model assigned" means "that capability is
off," by design.

### Model mode vs. agent mode

Each chat/reasoning slot shows a small **mode badge**:

- **model** — the orchestrator drives the assigned model through its own multi-step logic
  and structured output. This is the normal path for OpenAI/Anthropic/Gemini/Foundry/local
  models.
- **agent** — the slot is assigned a **CLI-agent** provider (e.g. **Claude Code**), which
  runs its *own* agent loop with its own tools; ND3X hands it the ND3X skills/MCP/tools via
  the gateway and takes back a result. CLI-agents can't be assigned to modality slots
  (embeddings/voice/TTS/realtime/image) — those stay orchestrator-only.

## Light mode (small & local models)

Small and local models spend most of their time reading the prompt, so ND3X can send
them a **compact prompt** — "light mode" — that keeps them responsive without changing
what they can do. Selected skills still get their full tool details; only the always-on
scaffolding is trimmed.

- **Automatic:** on for local models, off for cloud models.
- **Per model:** override it under **AI Models → Routing** (prompt mode: *full* /
  *light* / *auto*).
- **Per workflow operation:** an agent step can force light mode on or off (see
  [workflows.md](workflows.md)).

If a small model occasionally gets a tool's arguments wrong in light mode, the normal
tool-error recovery corrects it; selecting the relevant skill always gives it the full
details.

## Optimising for cost vs performance

The system makes ~one cheap selection call + an execution/answer phase per turn (plus
optional background cognition/memory). Tune by slot:

**Cost-optimized**
- Use **Advanced** mode.
- Put a **small/fast or local** model on **Skill choice** and **Memory lookup decision** —
  these are short, frequent, and don't need a frontier model.
- Keep a capable model on the **Agent model** slot *only* (that's where answer quality
  lives), or even drop it to a mid-tier model if your tasks are simple.
- Run **Memory & learning** on a cheap model, or leave it empty if you don't need
  long-term memory.
- Consider **local (Ollama)** models for selection/memory to remove their cost entirely.
- Set a **token budget** in the [Usage](usage.md) tab to cap spend.

**Performance-optimized**
- Put your strongest model on the **Agent model** slot (drives tool-use and final answers).
- Give **Skill choice** a solid (not tiny) model so it selects the right skill on busy
  catalogs — wrong selection wastes a whole turn.
- Enable **Memory & learning** and **Memory lookup** with capable models so the agent
  recalls context.
- Prefer low-latency cloud models; avoid large local models if your hardware makes them slow.

**Balanced (recommended default)**
- Strong **Agent model**; **small** Skill-choice + Memory-decision models; cheap
  **cognition**; local embeddings if available. This keeps the expensive model on the one
  step that determines answer quality and makes everything else cheap.

> Tip: watch the **Usage → by stage** breakdown ([usage.md](usage.md)) after a change — it
> shows exactly which slot is consuming tokens, so you can see the effect of moving a model
> between slots.
