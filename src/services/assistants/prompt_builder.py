from __future__ import annotations

import json
from typing import Any, Iterable

from component.logging import get_logger
from services.assistants.runtime_config import AssistantConfig, ToolConfig


log = get_logger(__name__)


class PromptBuilder:
    @staticmethod
    def _safe_json(value: Any) -> str:
        log.debugx(
            "Waarde veilig naar JSON serialiseren gestart",
            value_type=type(value).__name__,
        )
        try:
            result = json.dumps(value, ensure_ascii=False, indent=2)
            log.debugx(
                "Waarde succesvol naar JSON geserialiseerd",
                value_type=type(value).__name__,
                result_length=len(result),
            )
            return result
        except Exception:
            log.warningx(
                "Waarde naar JSON serialiseren mislukt, str(value) wordt gebruikt",
                value_type=type(value).__name__,
            )
            return str(value)

    @staticmethod
    def _light_schema_summary(schema: Any) -> str:
        """Terse one-line field list derived from the planner JSON schema, used in
        light mode instead of the full schema dump (the provider enforces the real
        schema via structured outputs; this line is the human-readable reminder)."""
        try:
            props = (schema or {}).get("properties") or {}
            required = set((schema or {}).get("required") or [])
            parts: list[str] = []
            for name, spec in props.items():
                enum = (spec or {}).get("enum") if isinstance(spec, dict) else None
                hint = f"({'|'.join(str(e) for e in enum)})" if enum else ""
                parts.append(f"{name}{'*' if name in required else ''}{hint}")
            if not parts:
                return "return one JSON object matching the enforced schema"
            return "one JSON object with fields: " + ", ".join(parts)
        except Exception:  # noqa: BLE001 — summary is best-effort
            return "return one JSON object matching the enforced schema"

    @staticmethod
    def _brief_args(argument: Any) -> str:
        """One-line argument summary from a JSON-schema: ``cmd*, cwd`` (``*`` =
        required). Used in light mode so the model still knows each tool's
        parameter names without the full schema dump."""
        try:
            props = (argument or {}).get("properties") or {}
            required = set((argument or {}).get("required") or [])
            if not props:
                return ""
            return ", ".join(
                f"{name}*" if name in required else name for name in props.keys()
            )
        except Exception:  # noqa: BLE001 — schema summary is best-effort
            return ""

    def render_tool_manifest(
        self, tools: Iterable[ToolConfig], *, compact: bool = False, args_brief: bool = False
    ) -> str:
        """Render tools for the planner. ``compact`` (used for the always-available
        builtins) prints only ``tool_id``, ``name`` and ``description`` — NOT the full
        JSON argument schema — to keep the prompt small. The full schema is rendered
        only for the tools of a SELECTED skill (the set the agent is actively working
        with), so it knows the exact arguments when it calls them. ``args_brief``
        adds a one-line parameter-name summary to a compact manifest (light mode)."""
        log.debugx("Tool manifest renderen gestart", compact=compact)
        lines: list[str] = []
        tool_count = 0

        for tool in tools:
            if not getattr(tool, "is_enabled", True):
                continue

            tool_count += 1

            lines.append(f"- tool_id={tool.id}, name={tool.name}")
            lines.append(f"  description: {tool.description}")
            if not compact:
                lines.append(f"  args: {self._safe_json(tool.argument)}")
                if tool.tool_instructions:
                    lines.append(f"  rules: {tool.tool_instructions}")
            elif args_brief:
                brief = self._brief_args(tool.argument)
                if brief:
                    lines.append(f"  args: {brief}")

            lines.append("")

        result = "\n".join(lines).strip()

        log.debugx(
            "Tool manifest renderen afgerond",
            tool_count=tool_count,
            result_length=len(result),
        )
        return result

    def render_skill_catalog(self, assistant: AssistantConfig) -> str:
        """Selectable domain skills (name + description only) for the merged agent loop —
        what the agent chooses from via action='select_skills'. System/runtime skills and
        builtin tools are excluded (they apply automatically)."""
        lines: list[str] = []
        for skill in assistant.skills or []:
            if not getattr(skill, "is_enabled", True):
                continue
            if getattr(skill, "is_system", False) or getattr(skill, "is_runtime", False):
                continue
            lines.append(f"- {skill.name}: {skill.description or ''}")
        return "\n".join(lines) or "(no selectable skills)"

    def render_always_on_tools_block(self, assistant: AssistantConfig, *, compact: bool = False) -> str:
        """The always-available builtin tools, rendered WITH full arg schemas.

        This block is STATIC across the agent loop (it doesn't depend on which skill
        is selected), so the pipeline puts it in the system `instructions` — sent once
        per request rather than re-embedded in every hop's user turn. On the OpenAI
        Responses session it isn't chained (so it isn't duplicated across hops); on
        Anthropic it's part of the cached system prefix. Returns "" when there are no
        always-on tools. ``compact`` (light mode) drops the full arg schemas in favor
        of a one-line parameter summary per tool."""
        always_on = [t for t in (getattr(assistant, "tools", None) or []) if getattr(t, "is_enabled", True)]
        if not always_on:
            return ""
        lines: list[str] = ["## Always-available builtin tools (no skill selection needed)"]
        # Intent → tool bridge: tool names are terse, so map common requests to the
        # right builtin so the agent uses them instead of claiming "no tool available".
        lines.append(
            "Use these directly (by tool_id) for the user's documents, files, shell and PDFs — "
            "they need NO skill selection. Do NOT say a capability is missing if it's covered "
            "here, and do NOT ask the user for details a tool can default:\n"
            "- Write/create/save a document → `text__ingest` (a default location is used — "
            "infer a sensible title; never ask the user for a path/filename). Edit → `text__update`.\n"
            "- Read a document → `text__get_file`; search documents → `text__search`; list → `text__list_files`; delete → `text__delete`.\n"
            "- Run a shell command → `system__shell_exec`; inspect/preview a file → `file_inspect`/`file_preview`; render a PDF → `pdf__render`."
        )
        # Full arg schemas: this block is sent once (in instructions), so the agent
        # gets the exact arguments for each builtin without per-hop duplication.
        # Light mode keeps only names + descriptions + brief parameter lists.
        lines.append(self.render_tool_manifest(always_on, compact=compact, args_brief=compact))
        return "\n".join(lines).strip()

    def render_skill_manifest(
            self,
            assistant: AssistantConfig,
            *,
            selected_skill_names: list[str] | None = None,
            is_workflow: bool = False,
            include_always_on: bool = True,
            light: bool = False,
    ) -> str:
        from services.assistants.runtime.system_skills import system_skill_applies

        selected = set(selected_skill_names or [])
        lines: list[str] = []

        # Always-available builtin tools. Static across the loop, so the merged-agent
        # planner path renders them ONCE in the system instructions instead — callers
        # there pass include_always_on=False. Other callers keep them inline.
        if include_always_on:
            block = self.render_always_on_tools_block(assistant, compact=light)
            if block:
                lines.append(block)
                lines.append("")

        for skill in assistant.skills or []:
            if not getattr(skill, "is_enabled", True):
                continue

            is_system = bool(getattr(skill, "is_system", False))

            # Flow separation: workflow-only contracts are skipped on chat turns and
            # chat-only contracts on workflow turns; "shared" contracts always apply.
            if is_system and not system_skill_applies(skill.name, is_workflow=is_workflow):
                continue

            if not is_system and skill.name not in selected:
                continue

            # Light mode: the verbose orchestrator_* contracts are replaced by the
            # distilled LIGHT_MODE_CONTRACT block that build_planner_prompt injects.
            # Other system/runtime skills stay.
            if light and is_system and skill.name.startswith("orchestrator_"):
                continue

            is_runtime = bool(getattr(skill, "is_runtime", False))
            kind = "system" if is_system else ("runtime" if is_runtime else "selected")

            lines.append(f"## Skill: {skill.name} ({kind})")

            if skill.description:
                lines.append(f"Description: {skill.description}")

            if skill.instructions:
                lines.append("")
                lines.append("Instructions:")
                lines.append(skill.instructions)

            if getattr(skill, "skill_files", None):
                lines.append("")
                lines.append("Attached skill files (metadata only; contents are not inlined):")
                lines.append(self._safe_json({
                    "skill_files_root": getattr(skill, "skill_files_root", None),
                    "skill_files": [
                        {
                            "relative_path": f.relative_path,
                            "runtime_path": f.runtime_path,
                            "content_type": f.content_type,
                            "size_bytes": f.size_bytes,
                            "checksum_sha256": f.checksum_sha256,
                            "is_executable": f.is_executable,
                        }
                        for f in (getattr(skill, "skill_files", None) or [])
                    ],
                }))

            if skill.tools:
                lines.append("")
                lines.append("Allowed tools:")
                # Light mode: only the actively SELECTED skills keep full arg
                # schemas; system/runtime skill tools get the brief summary.
                skill_compact = light and kind != "selected"
                lines.append(self.render_tool_manifest(
                    skill.tools, compact=skill_compact, args_brief=skill_compact,
                ))

            lines.append("")

        return "\n".join(lines).strip()

    def render_workflow_catalog(self, workflows) -> str:
        log.debugx(
            "Workflow catalog renderen gestart",
            has_workflows=bool(workflows),
        )
        lines: list[str] = []
        workflow_count = 0

        for workflow in workflows or []:
            workflow_count += 1
            if isinstance(workflow, dict):
                workflow_id = workflow.get("id")
                name = workflow.get("name")
                description = workflow.get("description")
                schedule_cron = workflow.get("schedule_cron")
            else:
                workflow_id = workflow.id
                name = workflow.name
                description = workflow.description
                schedule_cron = workflow.schedule_cron

            log.debugx(
                "Workflow toevoegen aan catalog",
                workflow_id=workflow_id,
                name=name,
                has_description=bool(description),
                has_schedule=bool(schedule_cron),
                source_type=type(workflow).__name__,
            )

            lines.append(f"- id={workflow_id}, name={name}")
            lines.append(f"  description: {description}")

            if schedule_cron:
                lines.append(f"  schedule: {schedule_cron}")

            lines.append("")

        result = "\n".join(lines).strip()
        log.debugx(
            "Workflow catalog renderen afgerond",
            workflow_count=workflow_count,
            result_length=len(result),
        )
        return result

    def render_assistant_catalog(self, assistants: Iterable[Any]) -> str:
        log.debugx("Assistant catalog renderen gestart")

        def get_value(obj: Any, key: str, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        lines: list[str] = []
        assistant_count = 0

        for assistant in assistants or []:
            assistant_count += 1

            assistant_id = get_value(assistant, "id")
            assistant_name = get_value(assistant, "name", "")
            description = get_value(assistant, "description", "") or ""
            assistant_type = get_value(assistant, "assistant_type", "") or ""
            routing_tags = get_value(assistant, "routing_tags", []) or []
            priority = get_value(assistant, "priority", None)
            skills = get_value(assistant, "skills", []) or []

            tags = ", ".join(str(tag) for tag in routing_tags if str(tag).strip())

            selectable_skills = []
            for skill in skills:
                skill_is_system = get_value(skill, "is_system", False)
                skill_is_enabled = get_value(skill, "is_enabled", True)

                # list_router_selectable() geeft normaal al alleen normale skills terug,
                # maar dit houdt de renderer veilig als dat later verandert.
                if skill_is_system:
                    continue
                if not skill_is_enabled:
                    continue

                selectable_skills.append(skill)

            log.debugx(
                "Assistant toevoegen aan catalog",
                assistant_id=assistant_id,
                assistant_name=assistant_name,
                assistant_type=assistant_type,
                has_description=bool(description),
                routing_tags=routing_tags,
                priority=priority,
                skill_count=len(selectable_skills),
                skill_names=[
                    get_value(skill, "name", "")
                    for skill in selectable_skills
                ],
            )

            lines.append(f"- id={assistant_id}, name={assistant_name}")

            if assistant_type:
                lines.append(f"  type: {assistant_type}")

            if description:
                lines.append(f"  description: {description}")
            else:
                lines.append("  description: ")

            if tags:
                lines.append(f"  domains: {tags}")

            if priority is not None:
                lines.append(f"  priority: {priority}")

            if selectable_skills:
                lines.append("  skills:")
                for skill in selectable_skills:
                    skill_name = get_value(skill, "name", "")
                    skill_display_name = get_value(skill, "display_name", None)
                    skill_description = get_value(skill, "description", "") or ""
                    skill_priority = get_value(skill, "priority", None)
                    tool_count = get_value(skill, "tool_count", None)

                    if tool_count is None:
                        skill_tools = get_value(skill, "tools", []) or []
                        tool_count = len(skill_tools)

                    lines.append(f"    - name={skill_name}")

                    if skill_display_name:
                        lines.append(f"      display_name: {skill_display_name}")

                    if skill_description:
                        lines.append(f"      description: {skill_description}")

                    if skill_priority is not None:
                        lines.append(f"      priority: {skill_priority}")

                    lines.append(f"      tool_count: {tool_count}")
            else:
                lines.append("  skills: []")

            lines.append("")

        result = "\n".join(lines).strip()

        log.debugx(
            "Assistant catalog renderen afgerond",
            assistant_count=assistant_count,
            result_length=len(result),
        )

        return result

    def build_router_prompt(
            self,
            *,
            assistant: AssistantConfig,
            available_assistants: list[AssistantConfig],
            available_workflows: list[Any],
            question: str,
            payload: dict[str, Any],
    ) -> str:
        log.infox(
            "Router prompt bouwen gestart",
            assistant_id=assistant.id,
            assistant_name=assistant.name,
            available_assistant_count=len(available_assistants),
            available_workflow_count=len(available_workflows),
            question_length=len(question or ""),
            payload_keys=list(payload.keys()),
        )

        router_memory_context = payload.get("_router_memory_context")

        # Keep memory internals out of the generic payload dump.
        # Router may only see router memory context, and only in the explicit block below.
        hidden_payload_keys = {
            "_router_memory_context",
            "_router_memory_context_injected",
            "_planner_memory_context",
            "_planner_memory_context_injected",
            "_memory",
            "_beliefs",
            "_system_context",
            "_active_conversation_state",
        }

        payload_for_prompt = {
            k: v
            for k, v in (payload or {}).items()
            if k not in hidden_payload_keys
        }

        router_memory_block = ""
        if router_memory_context:
            log.debugx(
                "Router memory context toevoegen aan router prompt",
                assistant_id=assistant.id,
                memory_type=type(router_memory_context).__name__,
            )
            router_memory_block = (
                "Router memory context:\n"
                "Use this only to improve assistant, workflow, or skill selection. "
                "Do not use it as domain knowledge. Do not answer the user from it.\n"
                f"{self._safe_json(router_memory_context)}\n\n"
            )
        active_conversation_state = payload.get("_active_conversation_state")
        active_conversation_block = ""
        if active_conversation_state:
            log.debugx(
                "Active conversation state toevoegen aan router prompt",
                assistant_id=assistant.id,
                state_type=type(active_conversation_state).__name__,
            )
            active_conversation_block = (
                "Active conversation state:\n"
                "Use this before routing to resolve follow-ups, confirmations, frustration, "
                "pronouns, and references to previous assistant messages. "
                "If the user is responding to an open question or continuing a previous task, "
                "prefer staying with the relevant assistant and continue the existing task.\n"
                f"{self._safe_json(active_conversation_state)}\n\n"
            )
        from services.assistants.runtime.system_assistants import capabilities_primer_for_type
        router_capabilities_block = capabilities_primer_for_type(getattr(assistant, "assistant_type", "router"))
        router_capabilities_section = f"{router_capabilities_block}\n\n" if router_capabilities_block else ""
        result = (
            "Choose which assistant or sequence of assistants should handle the request.\n\n"
            f"{router_capabilities_section}"
            "Schema (return JSON matching this):\n"
            f"{self._safe_json(assistant.schema)}\n\n"
            "Available assistants:\n"
            f"{self.render_assistant_catalog(available_assistants)}\n\n"
            "Available workflows:\n"
            f"{self.render_workflow_catalog(available_workflows)}\n\n"
            "Routing guidance:\n"
            "- Use mode=direct_answer for basic questions you can answer yourself with no skill/assistant/tool (greetings, small talk, simple/test messages, general knowledge). Put the reply in `answer`.\n"
            "- Reserve mode=ask_user for genuinely ambiguous requests; do not over-ask on trivial messages.\n"
            "- Use assistants for immediate conversational or interactive requests.\n"
            "- Use workflows for long-running, scheduled, repeatable, or background jobs.\n"
            "- Use mode=workflow_trigger when the user explicitly asks to run an existing workflow.\n"
            "- Use mode=workflow_offer when a workflow seems more suitable but user confirmation is appropriate.\n"
            "- Use Router memory context only for selecting the right assistant/workflow/skill.\n\n"
            "Payload guidance:\n"
            "- current_assistant: currently active assistant, if any\n"
            "- force_assistant: hard override assistant, if any\n"
            "- allow_multi_assistant: whether multi-step assistant workflows are allowed\n"
            "- previous_router_plan: prior router output, if any\n"
            "- previous_step_results: executed step outputs, if any. Treat successful entries as completed work.\n"
            "- completed_steps: step numbers that already completed successfully. Never repeat these unless force_rerun is explicit.\n"
            "- remaining_steps: unresolved steps from the prior plan, if any. Prefer continuing these instead of rebuilding the full plan.\n"
            "- router_replan_reason: why the orchestrator re-entered the router, if any.\n\n"
            f"{router_memory_block}"
            f"{active_conversation_block}"
            f"Question:\n{question}\n\n"
            f"Payload:\n{self._safe_json(payload_for_prompt)}\n"
        )

        log.infox(
            "Router prompt bouwen afgerond",
            assistant_id=assistant.id,
            assistant_name=assistant.name,
            result_length=len(result),
            payload_without_memory_keys=list(payload_for_prompt.keys()),
            has_router_memory_context=bool(router_memory_context),
        )
        return result

    @staticmethod
    def _connected_fabric_agents_block() -> str:
        """List enabled Fabric Data Agents so the planner knows which to pass to the
        `fabric_data_agent_query` tool. Best-effort; empty when none configured."""
        try:
            from db.database import SessionLocal
            from services.fabric.fabric_data_agent_service import FabricDataAgentService
            db = SessionLocal()
            try:
                agents = FabricDataAgentService(db).list_enabled()
            finally:
                db.close()
            if not agents:
                return ""
            lines = ["\nConnected Fabric data agents (query with the fabric_data_agent_query tool, agent=<name>):"]
            for a in agents:
                desc = (a.description or a.display_name or "").strip().replace("\n", " ")
                lines.append(f"- {a.name}: {desc[:200]}" if desc else f"- {a.name}")
            return "\n".join(lines) + "\n"
        except Exception:  # noqa: BLE001 — manifest hint is best-effort
            return ""

    def build_planner_prompt(
            self,
            *,
            assistant: AssistantConfig,
            question: str,
            payload: dict[str, Any],
    ) -> str:
        log.infox(
            "Planner prompt bouwen gestart",
            assistant_id=assistant.id,
            assistant_name=assistant.name,
            question_length=len(question or ""),
            payload_keys=list(payload.keys()),
            tool_count=len(assistant.tools or []),
            has_legacy_memory="_memory" in payload,
            has_planner_memory_context="_planner_memory_context" in payload,
            has_router_memory_context="_router_memory_context" in payload,
        )

        planner_memory_context = payload.get("_planner_memory_context")
        legacy_memory_context = payload.get("_memory")

        # Keep memory internals out of the generic payload dump.
        # Planner may only see planner memory context, and only in the explicit block below.
        hidden_payload_keys = {
            "_memory",
            "_beliefs",
            "_system_context",
            "_planner_memory_context",
            "_planner_memory_context_injected",
            "_router_memory_context",
            "_router_memory_context_injected",
            "_active_conversation_state",
            "_stateful_continuation",
            "_history_anchor",
            # Rendered as an explicit correction block below, not as payload noise.
            "_plan_validation_feedback",
        }

        # §6 — loop accumulators. The per-hop "_last_*" are exact subsets of the "_acc_*".
        if payload.get("_history_anchor"):
            # Anchor for the structured transcript path: the prior hops are sent as real
            # conversation turns (assistant tool calls + user observations), so the prompt
            # itself must carry NO accumulators — just rules + manifest + schema + question.
            hidden_payload_keys |= {
                "_acc_tool_calls", "_acc_tool_results", "_acc_docs",
                "_last_tool_calls", "_last_tool_results", "_last_docs",
            }
        elif payload.get("_stateful_continuation"):
            # Stateful continuation: the model already holds every prior hop in its
            # server-side session, so send ONLY the new observation. Drop the full "_acc_*"
            # dump (the O(n^2) growth) and keep the "_last_*" delta. No info loss — the loop
            # logic still reads "_acc_*" from the payload; this only trims the prompt.
            hidden_payload_keys |= {"_acc_tool_calls", "_acc_tool_results", "_acc_docs"}
        else:
            # Stateless hop (first pass / no session memory): dump the full accumulators and
            # drop the "_last_*" duplicates so the latest call/result/docs aren't sent twice.
            hidden_payload_keys |= {"_last_tool_calls", "_last_tool_results", "_last_docs"}

        payload_for_prompt = {
            k: v
            for k, v in (payload or {}).items()
            if k not in hidden_payload_keys
        }

        memory_block = ""

        if planner_memory_context:
            log.debugx(
                "Planner memory context toevoegen aan planner prompt",
                assistant_id=assistant.id,
                memory_type=type(planner_memory_context).__name__,
            )
            memory_block = (
                "\nPlanner memory context:\n"
                "Use this context only as relevant background for the current user request. "
                "Do not repeat it unless needed. Treat beliefs as tentative.\n"
                f"{self._safe_json(planner_memory_context)}\n"
            )

        elif legacy_memory_context:
            log.debugx(
                "Legacy memory context toevoegen aan planner prompt",
                assistant_id=assistant.id,
                memory_type=type(legacy_memory_context).__name__,
            )
            memory_block = (
                "\nMemory context:\n"
                "Use this context only as relevant background for the current user request.\n"
                f"{self._safe_json(legacy_memory_context)}\n"
            )

        selected_skill_names = payload.get("_selected_skill_names") or []
        is_workflow = bool(payload.get("_workflow_background"))
        # Light mode (small/local models): compact prompt — prefill dominates their
        # step latency. Resolved per turn by the pipeline (per-model prompt_mode,
        # auto = local).
        light = bool(payload.get("_light_mode"))
        # The always-on builtin manifest is NOT inlined here — the pipeline puts it in
        # the system instructions so it's sent once per request (not re-embedded in
        # every hop's user turn / accumulated in the OpenAI session). Only the
        # selected/system skills' tools (which change per hop) stay in the user prompt.
        skill_manifest = self.render_skill_manifest(
            assistant,
            selected_skill_names=selected_skill_names,
            is_workflow=is_workflow,
            include_always_on=False,
            light=light,
        )

        # Merged agent loop: when no domain skill is selected yet, show the catalog so the
        # agent can pick one itself (action='select_skills') as its first step — instead of a
        # separate selector call. Builtin tools are always available, so a trivial turn can
        # just answer. Once a skill is selected, this block disappears and its tools appear.
        needs_skill_selection = bool(payload.get("_needs_skill_selection")) and not selected_skill_names
        skill_catalog_block = ""
        select_skills_rule = ""
        if needs_skill_selection:
            skill_catalog_block = (
                "\nSkill catalog — load a skill with action='select_skills' (selected_skill_names) "
                "before using its tools:\n"
                f"{self.render_skill_catalog(assistant)}\n"
            )
            select_skills_rule = (
                "- To use a domain skill's tools, FIRST return action='select_skills' with the "
                "skill name(s) from the catalog below; their tools become available on the next "
                "step. Builtin tools above need no selection. If the message is trivial or general "
                "knowledge, just return action='final'.\n"
            )
        # Corrective feedback from plan validation: the previous reply parsed but
        # failed the conformity gate — tell the model exactly what to fix.
        validation_feedback_block = ""
        _validation_feedback = payload.get("_plan_validation_feedback")
        if _validation_feedback:
            problems = "\n".join(f"- {p}" for p in _validation_feedback)
            validation_feedback_block = (
                "\nYOUR PREVIOUS REPLY WAS REJECTED. Fix these problems and return a "
                f"corrected JSON object:\n{problems}\n"
            )
        fabric_agents_block = self._connected_fabric_agents_block()
        active_conversation_state = payload.get("_active_conversation_state")
        active_conversation_block = ""
        if active_conversation_state:
            log.debugx(
                "Active conversation state toevoegen aan planner prompt",
                assistant_id=assistant.id,
                state_type=type(active_conversation_state).__name__,
            )
            active_conversation_block = (
                "\nActive conversation state:\n"
                "Use this to resolve what the current user message refers to. "
                "This is recent visible chat state, not long-term memory. "
                "If the user is answering a previous assistant question, continue that task.\n"
                f"{self._safe_json(active_conversation_state)}\n"
            )
        from services.assistants.runtime.system_assistants import capabilities_primer_for_type
        capabilities_block = capabilities_primer_for_type(getattr(assistant, "assistant_type", "planner"))
        capabilities_section = f"{capabilities_block}\n\n" if capabilities_block else ""

        # Optional "for dummies" guidance for less-capable models. Enabled per-turn
        # via payload["_extra_guidance"], which the pipeline resolves from the
        # planner model's per-model flag (AI Models → Routing) OR a per-session
        # override toggled in the Chat tile. Placed at the very top for salience.
        extra_guidance_section = ""
        try:
            if bool(payload.get("_extra_guidance")):
                from services.assistants.runtime.system_assistants import EXTRA_GUIDANCE_PRIMER
                if EXTRA_GUIDANCE_PRIMER:
                    extra_guidance_section = f"{EXTRA_GUIDANCE_PRIMER}\n\n"
        except Exception:  # noqa: BLE001 — guidance is best-effort, never break prompt build
            extra_guidance_section = ""

        # Goal mode (/goal): don't stop until the goal is demonstrably achieved.
        # Top placement for maximum salience, above everything else.
        try:
            if bool(payload.get("_goal_mode")):
                from services.assistants.runtime.system_assistants import GOAL_MODE_CONTRACT
                if GOAL_MODE_CONTRACT:
                    extra_guidance_section = f"{GOAL_MODE_CONTRACT}\n\n{extra_guidance_section}"
        except Exception:  # noqa: BLE001 — goal block is best-effort
            pass

        # Flow-specific rule: a workflow step is fully autonomous and must never ask the
        # user; interactive chat may ask. (The schema still allows ask_user for chat.)
        if is_workflow:
            missing_info_rule = (
                "- You are running autonomously as a workflow step and CANNOT ask the user. "
                "If required information is missing, resolve it via the allowed tools, make a "
                "safe and explicitly-stated assumption, or return action='final' clearly "
                "stating what is missing. Never use action='ask_user'.\n"
            )
        else:
            missing_info_rule = (
                "- Prefer to ACT. If you can proceed with a sensible, explicitly-stated assumption "
                "(a default filename/location, a reasonable interpretation, an inferred title), do "
                "that instead of asking. Only use action='ask_user' when you are genuinely blocked "
                "and cannot proceed safely — never ask for details a tool can default (e.g. a "
                "document location). When asked to create and save something, generate it and save "
                "it in one go.\n"
                "- Narrate as you work: set `say` to one short, plain sentence for the user on each "
                "step (what you're doing, what you found, an error + how you're recovering). Use "
                "say:\"\" on trivial/routine steps. Keep internal rationale in `reason`.\n"
                "- For a large, ambiguous, destructive, or long-horizon request, return "
                "action='propose_plan' with a brief numbered plan in final_answer and wait for the "
                "user to approve before doing the work. Skip the plan for straightforward requests.\n"
            )

        if light:
            # Compact variant: the distilled core contract replaces the verbose
            # rules + orchestrator contracts + capabilities primer, and the full
            # schema dump becomes a terse field list (the provider enforces the
            # actual JSON schema via structured outputs).
            from services.assistants.runtime.system_assistants import LIGHT_MODE_CONTRACT
            if is_workflow:
                light_flow_rule = missing_info_rule
            else:
                light_flow_rule = (
                    "- Prefer to ACT with sensible, stated assumptions instead of asking. For a "
                    "big, destructive or ambiguous request, return action='propose_plan' with a "
                    "short numbered plan in final_answer and wait for approval.\n"
                )
            result = (
                f"{extra_guidance_section}"
                f"{LIGHT_MODE_CONTRACT}\n\n"
                f"{select_skills_rule}"
                f"{light_flow_rule}\n"
                f"Schema: {self._light_schema_summary(assistant.schema)}\n\n"
                "Active skills and allowed tools (builtin tools are in your system instructions):\n"
                f"{skill_manifest}\n"
                f"{skill_catalog_block}"
                f"{fabric_agents_block}"
                f"{memory_block}"
                f"{active_conversation_block}"
                f"{validation_feedback_block}"
                f"User question:\n{question}\n\n"
                f"Payload:\n{self._safe_json(payload_for_prompt)}\n"
            )
        else:
            result = (
                f"{extra_guidance_section}"
                "You will decide what to do next using the always-available builtin tools "
                "(listed in your system instructions) plus the active skills and tools listed below.\n\n"
                "Rules:\n"
                "- Every dynamic tool call MUST include tool_id.\n"
                "- You may call the always-available builtin tools (in the system instructions) at "
                "any time, plus any tools listed under the active skills below.\n"
                "- Do not use tools from inactive or unselected skills.\n"
                f"{select_skills_rule}"
                f"{missing_info_rule}"
                "- Use Planner memory context only as background for this assistant's planning. Do not treat it as a tool result.\n\n"
                f"{capabilities_section}"
                "Schema (return JSON matching this):\n"
                f"{self._safe_json(assistant.schema)}\n\n"
                "Active skills and allowed tools:\n"
                f"{skill_manifest}\n"
                f"{skill_catalog_block}"
                f"{fabric_agents_block}"
                f"{memory_block}"
                f"{active_conversation_block}"
                f"{validation_feedback_block}"
                f"User question:\n{question}\n\n"
                f"Payload:\n{self._safe_json(payload_for_prompt)}\n"
            )

        log.infox(
            "Planner prompt bouwen afgerond",
            assistant_id=assistant.id,
            assistant_name=assistant.name,
            result_length=len(result),
            payload_without_memory_keys=list(payload_for_prompt.keys()),
            has_planner_memory_context=bool(planner_memory_context),
            has_legacy_memory_context=bool(legacy_memory_context),
        )
        return result

    def build_final_answer_prompt(
        self,
        *,
        assistant: AssistantConfig,
        question: str,
        payload: dict[str, Any],
    ) -> str:
        log.infox(
            "Final answer prompt bouwen gestart",
            assistant_id=assistant.id,
            assistant_name=assistant.name,
            question_length=len(question or ""),
            payload_keys=list(payload.keys()),
        )
        result = (
            "You are the final answering assistant.\n"
            "Write ONLY the final user-facing answer (markdown). No JSON.\n\n"
            f"User question:\n{question}\n\n"
            f"Context:\n{self._safe_json(payload)}\n"
        )
        log.infox(
            "Final answer prompt bouwen afgerond",
            assistant_id=assistant.id,
            assistant_name=assistant.name,
            result_length=len(result),
        )
        return result