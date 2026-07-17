# ND3X — TODO

## Claude Code agent: dynamische ND3X-context-manifest (pinpointed)
_Aangedragen 2026-07-11. ✅ **GEBOUWD**: `services/providers/nd3x_agent_context.py`
(`build_nd3x_context_block`) — verbonden MCP-servers bij naam + dynamische skill-catalogus (enabled,
niet-system/runtime) + skill_files_root van geselecteerde skills; bewust GÉÉN per-tool-manifest. Gewired
in chat (`claude_code_chat_agent.py`) én workflow (`claude_code_operation_runner.py`). Getest
(tests/test_nd3x_agent_context.py, groen)._

**Doel:** de wereld-context die de Claude Code-agent krijgt (chat + workflow) dynamisch uit
de DB opbouwen — net als de orchestrator's `PromptBuilder` — maar toegespitst op Claude Code,
in plaats van de nu handgeschreven statische `ND3X_AGENT_PREAMBLE`.

**Bouw een dunne `build_nd3x_agent_context(db)`** die assembleert:
- **Wereld-model** (statisch): wat ND3X is, host-grens, taal → houden.
- **Skill-catalogus** (dynamisch, DB): naam + beschrijving van enabled, niet-system/niet-runtime
  skills. Hergebruik `PromptBuilder.render_skill_catalog`-query. Dit is de echte winst — Claude
  Code weet nu niet welke capability-bundels bestaan.
- **Verbonden MCP-servers bij naam** (dynamisch): vervangt de hardcoded "e.g. Fabric".
- **Skill-files**: per (geselecteerde) skill het `skill_files_root`-pad meegeven, zodat Claude
  Code's eigen Read/Bash de scripts kan lezen/draaien (file-backed skills). Dit is de enige
  ontbrekende draad om file-backed skills bruikbaar te maken.

**Nadrukkelijk NIET meenemen:**
- Tool-voor-tool manifest → de gateway exposet tools al native via MCP (naam+desc+schema).
- Orchestrator-instructieteksten / `tool_id` / `action=select_skills` / plan-schema → brengt
  precies de planner-JSON-verwarring terug die we op 2026-07-10 wegwerkten.

**Aansluiting:** chat (`_agent_instruction`) en workflow (`_handoff_instruction`) delen al de
preamble; beide gaan `build_nd3x_agent_context(db)` aanroepen (chat-agent heeft `self.db`, de
runner `self.db`). Selectie: geselecteerde skills volledig (instructie + files_root), de rest
als lichte catalogus. Let op de twee betekenissen van "skill" (ND3X vs Claude Code's eigen).

## HABIT4T — smart-home MCP-server (TADO + TOSOT + HUE + TUYA) — ✅ GEBOUWD (= Home-Service)
_Aangedragen 2026-07-11. **Standalone product, BUITEN de ND3X-repos.** Gebouwd + live gedeployed als
`../Home-Service/` (private repo nickozz714/Home-Service) — véél verder dan de oorspronkelijke spec:
5 vendors live (tado/gree/tuya/roborock/hue), energie-integraties (HomeWizard/forecast/weer),
Tapo-camera + lokale vision, camera-tijdlijn, "Zie → Doe"-regels, gezichtsherkenning, web-GUI, MCP-
pariteit. Draait intern op de huisserver. Echte-apparaat-verificatie is met Nick gedaan. Dit item
verwijst naar de oude werknaam; het echte werk staat in de Home-Service-worklog._

> ⚠️ **NACHTRUN-MODUS: ALLEEN BOUWEN (Nick, 2026-07-11).** Bouw in `../HABIT4T/` de codebase, adapters,
> trait-model, MCP-server, registry, tools, `.env.example`, README/Dockerfile en **mock-tests** (fake
> UDP/HTTP/signed-cloud). `git init` in die map + eigen feature-branch. **NIET doen** (geblokkeerd op Nick,
> vereist creds/hardware/LAN): stap 0 live LAN-discovery, TADO device-code login, HUE link-knop, Tuya-keys,
> en élke call naar een echt apparaat/cloud-account. **Vink dit item NIET af** — laat het open met een
> notitie "code klaar, echte-apparaat-verificatie wacht op Nick". Alleen mock-tests hoeven groen.

**→ Volledige design-spec: `/Users/nickduchatinier/Repositories/Claude/HABIT4T/DESIGN.md`**
(authoritative, 16 secties; hier alleen samenvatting). Los van ND3X; ND3X-registratie is optioneel (§12).
**→ Operator-gids: `HABIT4T/README.md`** — exacte stappen die Nick zélf uitvoert (onboarding TADO/HUE/
Tuya, LAN-checks) + curl/Postman-voorbeelden. **Nachtrun:** houd deze README synchroon met de echte
interface en lever bij de bouw een `postman_collection.json` (host/port als variabelen).
**Autonomie:** codebase + adapters + mock-tests zijn autonoom bouwbaar; ECHTE-apparaat-verificatie is
geblokkeerd op Nick (browser-login TADO, link-knop HUE, Tuya-keys, Gree op het LAN) — dus "bouwen kan
autonoom, verifiëren doen we samen".

**Doel:** één stdio-MCP-server die smart-home-apparaten uitleest én bestuurt over vendors heen
(TADO + TOSOT nu; Philips HUE + TUYA later), achter één **trait-gebaseerd** device-model. ND3X
combineert info (TADO-temp → TOSOT bijsturen/uit) via een cron-workflow. NIET via Home Assistant.

**Keuzes met Nick (2026-07-11, AskUserQuestion):**
- **Standalone stdio MCP** (zoals Fabric), één proces met vendor-adapters, in `ND3X-services/HABIT4T/`.
  Werknaam HABIT4T (vrij te hernoemen). Werkt mee door de fixes van vandaag (stdio-manager bedraad,
  gateway-delegatie, 64 MB readline-limiet).
- **TOSOT eerst onderzoeken** (nightrun stap 0): lokaal (greeclimate UDP, same-LAN) vs cloud
  (Tuya/Ewpe) → LAN-check, beslissen, documenteren, dán bouwen.
- **Beide tool-niveaus**: vendor-neutrale `home_*` tools + dunne `home_sync`; primaire combineer-weg
  is een ND3X cron-workflow ("climate-guard").

**Kern-ontwerp (zie DESIGN.md voor alles):** trait-model (OnOff/Thermostat/TemperatureSensor/
FanSpeed/Brightness/Color/…) i.p.v. climate-only, zodat HUE/TUYA er later onder passen; `VendorAdapter`
protocol als enige extensiepunt; persistente device-registry met stabiele ids + rooms; secrets via
env/secret-store (nooit naar AI); onboarding-CLIs per vendor (TADO device-code, HUE link-button,
Tuya keys). 14-stappen build-volgorde staat in DESIGN.md §14.

**TADO auth-drift:** password-grant deprecated (2025) → device-code flow (client_id
`1bb50063-6b0c-4d11-bd99-387f4a91cc46`, `login.tado.com/oauth2`, PyTado). **Live verifiëren** bij bouw.

## Gateway ↔ stdio MCP + Azure-sessie — ✅ OPGELOST (2026-07-11)
Gekozen + gebouwd: gateway **delegeert** tool-executie naar de hoofdserver (interne endpoint
`/api/internal/mcp/execute` + in-memory shared secret). Eén runtime, één Azure-sessie. Fabric
E2E bewezen (200). Zie worklog.

## Gegeneraliseerd "agent-mode" framework voor CLI-agent-providers (Claude Code, later Codex, …)
_Aangedragen 2026-07-11 (Nick). ✅ **Fase 0 t/m 5 GEBOUWD** (commits e4cd67f→9dc5f97): is_cli_agent-
capability + slot_mode + CAP_CLASS, gedeelde CliAgentRunner-base, capability-based dispatch +
no-fallback + modaliteit-guard, cognition als agent-target (zie item hieronder), decision-slots als
agent-modus, en de UI/execution_mode-afronding. Het gedetailleerde plan hieronder is historisch
(referentie); de kern staat. Eventuele losse polish kan als nieuw, klein item terugkomen._

**Kernmodel (Nick, 2026-07-11) — TWEE ASSEN:**

*As 1 — provider-modus (per slot, o.b.v. de toegewezen provider):*
- **Model-modus** (orchestrator-native): de orchestrator stuurt de LLM aan via z'n eigen meerstaps-
  logica; het model levert alleen antwoorden/structured output. Klassieke pad.
- **Systeem-modus** (CLI-gedelegeerd — naam TBD, kandidaten: "agent"/"system"/"delegated"): de slot
  resolvet naar een CLI-agent die z'n EIGEN agent-loop met EIGEN tools draait. De orchestrator besteedt
  veel per-stap-werk uit, maar **stuurt de ND3X-skills + MCP-servers + tools mee** (via de gateway)
  zodat de LLM-in-het-systeem die kan gebruiken. = wat we nu met Claude Code doen.

*As 2 — capability-klasse (kan het überhaupt uitbesteed worden?):*
- **Uitbesteedbaar** (tekst/redeneren/beslissen): planner, workflow-stap, cognition/memory-extractie,
  memory_decision, auto_decision, router, generators. → mag systeem-modus, mits provider = CLI-agent.
- **Orchestrator-only** (modaliteit/realtime): TTS, STT, Live duplex/realtime, embeddings, image-gen.
  → CLI-agent heeft er geen interface voor; ALTIJD in de orchestrator, CLI-agent op zo'n slot = verboden.

**Modus-namen (Nick, 2026-07-11): `model` en `agent`** (provider `execution_mode`). Belangrijk: het
"waarom" moet duidelijk gedocumenteerd zijn — `model` = orchestrator stuurt de LLM aan via z'n eigen
meerstaps-logica en dwingt structured output af; `agent` = de provider draait z'n eigen agent-loop met
eigen tools, wij sturen ND3X-skills/MCP/tools mee, en hij levert een resultaat via een output-contract
(geen schema-enforcement — CLI-agents kunnen dat niet: `supports_structured_output=False`, `.chat()`
negeert `response_format`).

**GEEN fallbacks (Nick, 2026-07-11 — kernprincipe).** De slot-toewijzing ÍS de configuratie; het
systeem gokt niet:
- **Leeg slot → de stap gebeurt niet** (feature off). (Bestaat al voor `chat.memory_decision`: "runs
  ONLY when the slot has a model assigned".) Dit wordt de norm voor álle uitbesteedbare slots.
- **Agent op een slot → agent-modus draait** — terecht, want die slot ondersteunt het. Geen stille
  terugval naar een structured model, geen "broken structured call" meer. (De huidige gap waarbij
  decision-slots op claude_code stil terugvielen op `claude_code.chat` met gedropt schema = juist wat
  we wegnemen: die slots krijgen een echte agent-modus i.p.v. een fallback.)
- **Model op een slot → model-modus draait.**
- **Modaliteit/realtime-slots** (TTS/STT/Live/embeddings/image): een CLI-agent is daar simpelweg
  **niet toewijsbaar** — afdwingen bij de toewijzing (UI/registry biedt 't niet aan), niet via een
  runtime-fallback. Proza-slots (final_answer/writer, web_search) draaien al als plain chat.

**Nu nog fout:** "systeem-modus" is hard vastgepind op de string `"claude_code"` en per subsysteem apart
gebouwd (ClaudeCodeChatAgent, ClaudeCodeOperationRunner, web_search `_claude_code`). Moet **capability-
based** (`provider.is_cli_agent`) en **provider-agnostisch** (Codex e.a. later).

**Kaart — waar agent-mode is / moet / niet kan:**
| Slot / rol | Structured JSON? | Status |
|---|---|---|
| chat.planner | ja (plan) | ✅ done — option-A agent (pipeline_runner:984) |
| workflow-stap | ja (handoff-envelope) | ✅ done — claude_code engine |
| chat.web_search | nee (proza) | ✅ done — native CLI search |
| chat.final_answer / writer: | nee (proza) | ✅ werkt als plain chat |
| chat.cognition (+ generators: skill/workflow/meeting/finalizer) | ja | ❌ TODO — blackbox agent (zie hieronder) |
| chat.memory_decision | ja (klein) | ❌ TODO — agent-modus (leeg=uit, agent=agent); geen fallback |
| chat.auto_decision | ja (klein) | ❌ TODO — idem |
| chat.router | ja | ❌ TODO — idem |
| embeddings/image/realtime/transcription | n.v.t. | ❌ CLI-agent NIET toewijsbaar (afdwingen bij toewijzing) |

**Generalisatie (3 stukken):**
1. **Capability i.p.v. naam.** Voeg op de provider-base een marker toe (bv. `is_cli_agent: bool` /
   `agent_capabilities`) i.p.v. `if provider_type == "claude_code"`. Elke CLI-agent-provider (claude_code,
   toekomstige codex) zet 'm True; subsystemen branchen op de capability. Dit is het "niet vastpinnen
   op Claude"-deel.
2. **Gedeelde `CliAgentRunner` base.** ClaudeCodeChatAgent + ClaudeCodeOperationRunner delen nu al veel
   (env strippen, gateway-config, provider bouwen, spawnen, envelope parsen). Til dat op naar één base;
   per subsysteem verschilt alleen de INSTRUCTIE + het output-contract + de parse. Nieuwe targets
   (cognition) en nieuwe agents (CodexRunner) zijn dan kleine subclasses.
3. **Per-slot gedrag = de toewijzing, geen fallback (beslist).** Leeg → stap uit; agent → agent-modus;
   model → model-modus. Modaliteit-slots: CLI-agent niet toewijsbaar (afdwingen in UI/registry). Dus
   ook de kleine decision-slots (memory_decision/auto_decision/router) krijgen een echte agent-modus
   (envelope, tolerant parsen) wanneer er een agent op staat — geen stille terugval meer.

**Provider-agnostisch (Codex-ready):** de agent-specifieke details (CLI-commando, auth-env, model-coercion,
gateway) horen achter de provider/runner-abstractie, niet in de subsysteem-branches. Een nieuwe CLI-agent
toevoegen = een provider met `is_cli_agent=True` + een runner-subclass, geen wijziging in chat/workflow/
cognition-code.

---

### IMPLEMENTATIEPLAN (gefaseerd) — agent-mode framework
_Feature-branch only, decide+log. Experimenteren/live-calls via het platform is toegestaan (Nick's zege,
2026-07-11), ook met een cloud-model, MITS binnen de vaste account-limieten (kunnen opraken — spaarzaam,
liefst kleine/goedkope modellen voor experimenten). Elke fase: implementeren + tests + waar zinvol één
live-verificatie, dan pas door. Volgorde is bewust: eerst primitieven, dan refactor zonder gedragswijziging,
dan pas nieuwe targets._

**Betrokken bestanden (referentie):**
- `src/services/providers/base.py` — `ChatProvider` (capability toevoegen).
- `src/services/providers/claude_code_provider.py` — `is_cli_agent=True` + CLI-details (bestaat al: env,
  gateway, `claude_code_model`, `chat_stream_events`, envelope-idee).
- `src/services/providers/llm_router.py` — `chat_provider_and_model`, `chat_provider_type`,
  `ask_orchestration_async`, `_dispatch_chat` (mode-probe).
- `src/services/providers/registry_service.py` — `resolve_slot` (+ toewijzings-guard voor modaliteit).
- `src/services/assistants/orchestration/pipeline_runner.py` (~968-1090) — chat option-A branch.
- `src/services/assistants/claude_code_chat_agent.py` + `src/services/workflows/claude_code_operation_runner.py`
  — bestaande runners → optillen naar base.
- `src/services/system_cognition/{system_cognition_service,system_pipeline_runner,factory,dispatcher}.py`.
- `src/services/web_search_service.py` — `_claude_code`.
- FE: `lovely-landing-project/src/.../AIModelsSection.tsx` (routing/slots + modaliteit-guard).

**Fase 0 — Primitieven (capability + modus + policy), geen gedragswijziging.**
- `ChatProvider.is_cli_agent: bool = False`; in `ClaudeCodeChatProvider` op `True`. (Optioneel
  `execution_mode`-property afgeleid hiervan; providers blijven verder gelijk.)
- Router-helper `slot_mode(role) -> "agent" | "model" | None` (None = leeg slot = uit), o.b.v.
  `resolve_slot` + `provider.is_cli_agent`.
- `CAP_CLASS`-map: welke rollen **outsourceable** zijn (planner, cognition, memory_decision, auto_decision,
  router, workflow, generators) vs **modality-only** (embeddings, transcription, realtime/live, image, tts).
- Tests: unit voor `slot_mode` (leeg/model/agent) + CAP_CLASS. Nog geen call-site die 't gebruikt.
- Acceptatie: bestaand gedrag 100% ongewijzigd; nieuwe helpers getest.

**Fase 1 — Gedeelde `CliAgentRunner` base (refactor, gedrag-behoudend).**
- Nieuw `src/services/providers/cli_agent_runner.py`: gemeenschappelijke logica uit de twee runners —
  provider bouwen (via registry + `is_cli_agent`), gateway `--mcp-config` schrijven/opruimen, env,
  spawnen, `run()`/`run_stream_events()`, `parse_envelope()` (til `_parse_envelope` hierheen).
- Subclass-API: `build_instruction(ctx)`, `output_contract`, `parse_result(text)`. Per subsysteem
  verschilt alleen dit.
- Herschrijf `ClaudeCodeChatAgent` en `ClaudeCodeOperationRunner` als dunne subclasses. **Geen**
  functionele wijziging.
- Tests: bestaande claude_code/workflow-suite groen. Live: één chat-turn + één workflow-stap draaien.
- Acceptatie: identiek gedrag chat + workflow; code ~gedeeld.

**Fase 2 — Capability-based dispatch (verwijder `== "claude_code"`) + no-fallback + modaliteit-guard.**
- pipeline_runner chat-branch: `if provider.is_cli_agent` i.p.v. `_cc_type == "claude_code"`.
- web_search: `_claude_code` → generiek `_cli_agent` (elke `is_cli_agent`-provider met native web).
- Workflow-engine-selectie afstemmen op de modus (engine "claude_code" → capability/`agent`), zodat een
  toekomstige CLI-agent dezelfde engine-weg volgt.
- **No-fallback afdwingen:** leeg slot = stap uit (generaliseer het bestaande `chat.memory_decision`
  "unassigned = off"-patroon); geen stille terugval meer.
- **Modaliteit-guard bij toewijzing:** `registry_service`/toewijs-endpoint weigert een `is_cli_agent`-
  provider op een modality-only slot; FE (`AIModelsSection`) biedt 'm daar niet aan + toont per slot de
  actieve modus (model/agent/uit) en het "waarom".
- Tests: dispatch kiest agent o.b.v. capability; modaliteit-toewijzing geweigerd (unit + API).
- Acceptatie: niets hangt meer aan de string `"claude_code"` in de dispatch-paden.

**Fase 3 — Cognition als agent-target (de blackbox).** (detail-item hieronder)
- `CognitionAgentRunner(CliAgentRunner)`: instructie = "beslis memory/belief/curiosity + retourneer
  envelope"; contract `{decision, memories[], beliefs[], curiosity[]}`; mag via mcp__nd3x bestaande
  memories opvragen om te dedupliceren.
- `SystemCognitionService`/dispatcher: `slot_mode("cognition")` → agent ⇒ blackbox, model ⇒ bestaande
  structured pijplijn, leeg ⇒ uit. Orchestrator schrijft de envelope weg via de bestaande repos.
- Tests: envelope-parse + persist (mocked). Live (spaarzaam, klein model waar mogelijk): één turn agent-
  cognition, verifieer dat memories/beliefs correct in de DB komen + in de volgende turn geïnjecteerd.
- Acceptatie: met een CLI-agent op de cognition-slot ontstaan/injecteren memories betrouwbaar.

**Fase 4 — Decision-slots als agent-modus (memory_decision / auto_decision / router).**
- Kleine agent-instructie + envelope per slot; dispatch wanneer `slot_mode==agent`. Leeg=uit, model=huidige
  structured call. (Let op kosten: subprocess per beslissing — daarom is "leeg=uit" belangrijk en zet je
  alleen een agent als je dat écht wilt.)
- Tests + één live per slot.

**Fase 5 — Afronding: UI, docs, Codex-proof.**
- FE routing-scherm: modus-badge per slot (model/agent/uit) + korte uitleg van het waarom.
- Docs/README-sectie: model vs agent, no-fallback, capability-klassen, "hoe voeg ik een CLI-agent toe"
  (= provider met `is_cli_agent` + `CliAgentRunner`-subclass). Optioneel: een dummy 2e CLI-agent-provider
  als rooktest dat niets Claude-specifiek is blijven hangen.
- Acceptatie: een tweede (hypothetische) CLI-agent zou zonder subsysteem-wijziging werken.

**Risico's/aandachtspunten:** (1) refactor-fase 1 mag géén gedrag wijzigen — leun op de bestaande suite +
live smoke. (2) envelope-parse moet tolerant blijven (`_parse_envelope` bestaat al). (3) modaliteit-guard
ook server-side afdwingen, niet alleen FE. (4) experimenteer-budget bewaken (account-limieten).

## Claude Code als "blackbox" cognition-pad (agent doet memory/belief-extractie zelf)
_Aangedragen 2026-07-11 (Nick). ✅ **GEBOUWD** (commit a3b997e, agent-mode Fase 3):
`services/system_cognition/cognition_agent_runner.py` (CliAgentRunner-subclass) + dispatcher-tak;
wanneer de cognition-slot een CLI-agent is draait het blackbox-pad (agent beslist + extraheert
memory/belief/curiosity in één pass, envelope wordt gepersisteerd), anders de bestaande structured
pijplijn, leeg = uit. Getest (tests/test_cognition_agent.py, groen)._

**Context/bevinding:** cognition draait nu via `openai_service.ask_orchestration_async(json_schema=…)`
op de slots **chat.cognition** + **chat.memory_decision** — een structured-output pijplijn met
meerdere LLM-calls per turn (turn-interpretatie → memory-write → belief → curiosity). De
`claude_code`-provider heeft `supports_structured_output=False` en z'n `.chat()` **negeert
response_format**, dus de json_schema wordt gedropt → geen enforcement → onbetrouwbare JSON.
Cognition via de HUIDIGE pijplijn op claude_code werkt dus niet betrouwbaar (los van subprocess-
latency per call).

**Idee (Nick):** net als bij chat option-A — geef claude_code NIET de meerstaps-pijplijn, maar
één **blackbox-instructie**: "hier is de turn (vraag + antwoord [+ trace]); beslis zelf of er iets
onthouden moet worden (memory/belief/curiosity), zo ja extraheer het in DEZE JSON-vorm en geef
het terug." Opus is sterk genoeg om die beslissing + extractie in één agentische pass te doen. De
orchestrator persisteert het resultaat alleen nog (DB) voor injectie in een volgende turn.

**Waarom dit past:**
- Zelfde patroon als de workflow-`_HANDOFF_INSTRUCTION`/envelope: agentic run eindigt met één
  JSON-envelope; tolerant parsen (geen schema-enforcement nodig). Dat werkt al.
- MINDER calls dan de huidige pijplijn (1 agentische pass i.p.v. meerdere), dus subprocess-latency
  valt mee.
- Bonus: de agent kan via de mcp__nd3x-gateway bestaande memories opvragen om te dedupliceren —
  een echt voordeel t.o.v. de huidige stateless pijplijn.

**Ontwerp-schets:**
- Per-provider split (zoals option-A voor chat): als de cognition-slot → claude_code, gebruik het
  blackbox-cognition-pad; anders de bestaande structured pijplijn.
- Output-contract = een gedefinieerde envelope `{memories:[…], beliefs:[…], curiosity:[…], decision:…}`
  die de orchestrator in de bestaande repos (MemoryRepository/BeliefRepository/…) wegschrijft.
- Hergebruik de envelope-parser uit `claude_code_operation_runner._parse_envelope`.
- Draait als achtergrond-taak (dispatcher), net als nu — alleen de "brain" verandert.
Aandachtspunten: de instructie moet de memory/belief-criteria bevatten (wat de router nu doet:
"durable preference/rule/decision" vs "volatile lookup"); dedupe tegen bestaande memories; kosten.

## Skills-overzicht: skills met niet-bestaande tools ook selecteerbaar maken — ✅ ONDERZOCHT (al graceful)
_Aangedragen 2026-07-11. Onderzocht + geverifieerd 2026-07-17: **er is geen blokkade** — het gedrag is
al " negeer de ontbrekende tools, hou de skill"._
Bevinding (alle relevante lagen nagelopen): `skill_tool` heeft `ondelete=CASCADE` én
`SkillToolRepository.get_for_skill` doet een **inner join op Tool**, dus een verwijderde tool laat nooit
een dangling-referentie achter — de tool valt gewoon weg. Runtime laadt met `enabled_only=True` (missing
én disabled tools worden overgeslagen). `render_skill_catalog` en de router laten een skill toe op
**enabled-status + naam**, nooit op "heeft de tools nog". FE-overzicht (SkillsSection/AssistantSkillsPanel)
blokkeert niets op tool-basis (er is enkel een "Without tools"-filter). Een skill met 0 tools is boven-
dien legitiem (instructie/file-only), dus een "kapot"-markering zou juist misleiden. **Conclusie:** geen
productiewijziging nodig; regressietest toegevoegd (`tests/test_skill_missing_tools_graceful.py`) die dit
vastzet: missing (dangling link) + disabled tool → runtime houdt alleen de live enabled tool; een
toolloze skill blijft in de selecteerbare catalogus.

## Background Agents dispatchen met een eigen, instelbaar orchestrator-model (slot `chat.background`) — ✅ GEBOUWD (2026-07-17)
_Aangedragen + beslist + gebouwd 2026-07-17 (commit 56c683c). Fase 1 (slot + resolutie + no-fallback)
af; suite 703 groen._

**Gebouwd:** nieuw OUTSOURCEABLE slot `chat.background` (capability_router.ALL_SLOTS + execution_mode.
CAP_CLASS + models.provider.ROUTING_SLOTS). `agent_tools.resolve_background_model()` (per-call model >
slot > **weigeren**). Gate in `agent__dispatch` (ná de depth-guard) en `task__create` (vóór het spawnen,
fail-fast). Het opgeloste model wordt de `forced_model` van de subagent-run, dus de hele background-run
draait erop — en als het een CLI-agent-model is draait de background-run **automatisch in agent-modus**
(Fase 2 uit het agent-mode-framework). FE: ROUTING_SLOTS + SLOT_HINTS (Agent-groep, advanced).
docs/guide/agent.md bijgewerkt. Tests: tests/test_background_slot.py.
**Uitrol-notitie (belangrijk):** dit is een gedragswijziging — `agent__dispatch`/`task__create` weigeren
nu tot `chat.background` is toegewezen (of een `model` wordt meegegeven). Bewust, no-fallback; staat in
de foutmelding + docs. Nick moet dus in AI Models → Routing een model op **Background agents** zetten.

_Oorspronkelijk onderzoek + ontwerp hieronder (referentie)._

**Doel:** background agents kunnen dispatchen (fire-and-forget subagent-runs) waarbij instelbaar
is **welk model de orchestrator van zo'n background run aandrijft** — los van het voorgrond-model.
Conform het slot-principe: géén hardcoded modellen, de toewijzing ís de configuratie.

**Kernbevinding — het dispatchen bestaat al, alleen het eigen slot ontbreekt:**
- `agent__dispatch` (`src/services/builtin/tools/agent_tools.py:110`) draait een verse subagent
  (eigen `subagent-<uuid>`-thread, schone context, condensed handoff, `SUBAGENT_MAX_DEPTH`=3,
  parallelle dispatches) via `run_ask_orchestrator(...)`.
- `task__create/status/result/list` (`src/services/builtin/tools/background_tasks.py`) is het
  fire-and-forget-laagje eromheen: detached `asyncio.create_task` → `agent_dispatch`, `bg-<hex12>`-id,
  `background_tasks`-tabel + `background_task_router.py` + takenpaneel in de FE, drain-notificaties
  in de agent-loop, restore-on-boot, `BACKGROUND_TASK_MAX_ACTIVE`=16.
- **De gap:** een background run resolvet nu gewoon `chat.planner` (zelfde slot als de voorgrond;
  `ask_job_callbacks.py`, `provider_factory.role_to_slot`). Het `model`-argument op beide tools is
  een per-call override (`agent_tools.py:158`), geen configuratie.
- Motivatie uit de code zelf (`background_tasks.py:228`): op een LOKAAL model queuet de background
  task achter de eigen stappen van de parent ("one model, one queue") — een **eigen slot** dat naar
  een ándere provider wijst is precies hoe je echte parallelliteit krijgt.

**Kernontwerp:**
1. **Nieuw slot `chat.background`** in `capability_router.ALL_SLOTS` (geen DB-migratie nodig —
   `capability_assignments` is keyed op slot-string) + classificeren als **OUTSOURCEABLE** in
   `execution_mode.CAP_CLASS`. Daarmee geldt het agent-mode-framework automatisch: **een CLI-agent
   (claude_code, straks codex) op `chat.background` ⇒ background runs draaien in agent-modus** —
   dat is de "Orchestrator mode"-instelbaarheid die Nick wil, langs beide assen (welk model / welke modus).
2. **Resolutie in de dispatch-keten:** `agent_dispatch`/`task_create` markeren de run als background
   (payload-vlag of `role="background:"`); `role_to_slot` mapt die naar `chat.background`; per-call
   `model`-override blijft werken (bestaand `forced_model`-mechanisme).
3. **Leeg slot = stuklopen — GEEN fallback (beslist, Nick 2026-07-17).** Onbezet `chat.background` ⇒
   `task__create`/`agent__dispatch` weigeren met een duidelijke fout ("wijs een model toe aan
   chat.background"), consistent met het no-fallback-kernprincipe uit het agent-mode-item: de
   toewijzing ís de configuratie, het systeem gokt niet. Let op bij uitrol: background dispatch
   doet het pas weer nádat het slot is toegewezen — dat is bedoeld gedrag, in de foutmelding en
   docs benoemen.
4. **FE:** routing-scherm (`AIModelsSection.tsx`) toont het nieuwe slot met uitleg + modus-badge
   (checken of de slot-lijst dynamisch uit de BE komt of hardcoded is).

**Fasering (klein):**
- **Fase 1 — slot + resolutie.** `ALL_SLOTS` + `CAP_CLASS` + role-mapping + background-vlag in de
  dispatch-keten; unit-tests (leeg=weigert met duidelijke fout, toegewezen=eigen model, override wint).
  Acceptatie: `task__create` op een tweede provider draait aantoonbaar op dat model terwijl de
  voorgrond op `chat.planner` blijft.
- **Fase 2 — agent-modus op het slot.** `slot_mode("chat.background")==agent` ⇒ de background run
  via de CLI-agent-weg (afhankelijk van agent-mode Fase 2; anders parkeren tot die af is).
  Acceptatie: CLI-agent toegewezen ⇒ background run = agent-run met handoff-envelope.
- **Fase 3 — FE + docs.** Slot in routing-scherm + `docs/guide/ai-models.md`/`agent.md` bijwerken.

**Betrokken bestanden (referentie):** `src/services/providers/capability_router.py:31` (`ALL_SLOTS`),
`src/services/providers/execution_mode.py:50` (`CAP_CLASS`), `src/services/providers/provider_factory.py:41`
(`role_to_slot`), `src/services/builtin/tools/agent_tools.py`, `src/services/builtin/tools/background_tasks.py`,
`src/services/assistants/ask_job_callbacks.py`, FE `lovely-landing-project/src/.../AIModelsSection.tsx`.

## Azure AI Foundry als LLM-provider (`azure_foundry`) — ✅ OPGELOST (2026-07-17)
_Aangedragen + beslist + gebouwd + **live geverifieerd** 2026-07-17. Fase 1+2 af; suite 694 groen._

**Live smoke (echte Azure-resource, 2026-07-17):** resource `nd3x-foundry-weu` aangemaakt in de
Beeminds Playground-sub (rg `rg-nd3x-ai`, West Europe, kind AIServices) met 3 deployments:
`gpt-5-mini`, `deepseek-v3-2` (DeepSeek-V3.2) en `text-embedding-3-small`. Via de ND3X-adapters
geverifieerd: chat ✓, streaming ✓, **niet-OpenAI-model (DeepSeek) ✓**, embeddings ✓, Discover ✓
(alle drie endpoint-aliassen — cognitiveservices/openai/services.ai — serveren de v1-route).
Discover verbeterd n.a.v. de smoke: leest eerst de échte **deployments**
(`/openai/deployments?api-version=2023-03-15-preview` — ids = deployment-namen), v1-catalogus
alleen als fallback (die geeft het hele Foundry-aanbod, niet de resource-deployments).

**Nog te doen door Nick zelf:** provider registreren in de draaiende ND3X-instantie (AI Models →
Cloud → Azure AI Foundry: base URL `https://nd3x-foundry-weu.cognitiveservices.azure.com` + key
uit `az cognitiveservices account keys list -g rg-nd3x-ai -n nd3x-foundry-weu`) en desgewenst een
slot toewijzen. **Fase 3 (Entra ID keyless) blijft optioneel/ongebouwd.**

**Doel:** Azure AI Foundry-deployments als volwaardige provider in de registry, zodat álle
Foundry-modellen (Azure OpenAI-modellen én DeepSeek/Grok/MAI/Llama/Phi/Mistral) toewijsbaar zijn
op routing-slots — chat, embeddings, en per-model capabilities.

**Extern onderzoek (2026-07-17) — de weg is gunstig:**
- Sinds aug 2025 heeft Foundry een **v1 GA API die OpenAI-compatibel is**: standaard `openai`-SDK
  met `base_url="https://<resource>.openai.azure.com/openai/v1/"` (of
  `https://<resource>.services.ai.azure.com/openai/v1/`) — **geen `api-version` meer, geen
  `AzureOpenAI()`-client meer nodig.**
- **`model` = de deployment-naam** (niet de modelnaam) — belangrijk voor `ProviderModel.model_id`.
- **Auth:** Azure API-key als `api_key`, óf keyless Entra ID: `get_bearer_token_provider(
  DefaultAzureCredential(), "https://ai.azure.com/.default")` als `api_key` (auto token-refresh zit
  nu in de OpenAI-client). Voor een server: service principal via env
  (`AZURE_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_CLIENT_SECRET`) — géén device-code/loopback nodig.
- Niet-OpenAI-modellen (DeepSeek, Grok, MAI-DS-R1, …) draaien via **dezelfde** v1
  chat-completions-route, incl. streaming/function calling waar het model dat kan.
- ⚠️ **`azure-ai-inference` SDK is deprecated** (retired 26-08-2026) — NIET op bouwen.
- Bronnen: learn.microsoft.com → `foundry/foundry-models/concepts/endpoints`,
  `foundry/openai/api-version-lifecycle`, `foundry/how-to/model-inference-to-openai-migration`.

**Bevinding codebase — er is nog géén Azure-provider, wel een perfecte template:**
- `openai_compatible_provider.py` is exact het juiste patroon (`AsyncOpenAI(base_url=..., api_key=...)`,
  chat/stream/embeddings/transcription/speech) en noemt Azure al in z'n docstring. Dankzij de v1 API is
  een Foundry-adapter bijna 1:1 dit patroon — geen `api-version` query-param of `api-key`-header-hack meer.
- De string `"azure_openai"` komt al anticiperend voor in `vision_capability.py:23`,
  `web_search_capability.py:16` en `web_search_service.py:56`, maar niets construeert een provider.
- Registratiepunten voor een nieuw type: `models/provider.py:35` (`PROVIDER_TYPES`),
  `provider_factory.py:62/130` (`_build_chat_provider`/`_build_embedding_provider`),
  `provider_presets.py` (`PRESETS`), `model_discovery.py:67`.
- Credentials horen in de DB (`Provider.api_key_encrypted`, Fernet) — bestaand patroon, geen env.
- **Los houden van** de bestaande Azure-login voor Fabric (`fabric_data_agent`, device-code) — andere
  resource/scope, niet koppelen.

**Kernontwerp:**
1. **Nieuw `provider_type = "azure_foundry"` (beslist, Nick 2026-07-17)** — subclass/variant van de
   openai_compatible-adapter (zelfde OpenAI-SDK-pad). De capability-helpers die nu anticiperend
   `"azure_openai"` noemen worden uitgebreid met `"azure_foundry"` (het type dekt méér dan alleen
   OpenAI-modellen).
2. **Preset** in `PRESETS`: base_url-template `https://<resource>.openai.azure.com/openai/v1/`
   (resource-naam invullen), `needs_base_url`, capabilities chat+embeddings. In de FE-uitleg expliciet:
   "model-id = je deployment-naam".
3. **Fase 1 auth = API-key** (past in `api_key_encrypted`). **Entra ID keyless als latere fase**
   (token-provider i.p.v. statische key; vergt een provider-config-keuze in `config_json` +
   `azure-identity`-dependency — alleen doen als Nick het nodig heeft).
4. **Model discovery:** onderzoeken of `GET {base_url}/models` op de v1-route de deployments teruggeeft;
   zo ja → branch in `model_discovery.py`, zo nee → handmatig modellen toevoegen (bestaande flow, net
   als openai_compatible).
5. **Niet doen:** Foundry op het legacy OpenAI-base-path (`openai_service.py`, Responses API) — dat pad
   blijft OpenAI-only; Foundry loopt volledig via de provider-adapter.

**Fasering (klein):**
- **Fase 1 — adapter + registratie.** Provider-class, `PROVIDER_TYPES`, factory-branches, preset,
  capability-helpers; unit-tests (mock base_url) + één live smoke op Nicks Foundry-resource
  (chat + streaming + embeddings, en één niet-OpenAI-deployment als die er is).
  Acceptatie: Foundry-provider aanmaken via `/admin/providers`, model toewijzen aan een slot, turn draait.
- **Fase 2 — discovery + polish.** Deployment-listing (indien API het geeft), vision/web-search-flags
  per model, `docs/guide/ai-models.md` bijwerken.
- **Fase 3 (optioneel) — Entra ID keyless.** Token-provider-auth als alternatief voor de API-key.

**Betrokken bestanden (referentie):** `src/services/providers/openai_compatible_provider.py` (template),
`src/services/providers/provider_factory.py:62,130`, `src/models/provider.py:35`,
`src/services/providers/provider_presets.py`, `src/services/providers/model_discovery.py:67`,
`src/services/providers/vision_capability.py:23`, `src/services/providers/web_search_capability.py:16`,
`src/services/web_search_service.py:56`.
