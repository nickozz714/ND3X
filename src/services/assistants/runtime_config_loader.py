from __future__ import annotations

from typing import Any, Dict, List

from component.logging import get_logger
from services.assistants.assistant_service import AssistantService
from services.assistants.runtime_config import AssistantConfig, SkillConfig, SkillFileConfig, ToolConfig

from repository.assistant_skill_repository import AssistantSkillRepository
from repository.skill_repository import SkillRepository
from repository.skill_tool_repository import SkillToolRepository
from services.assistants.skill_file_service import SkillFileService

log = get_logger(__name__)


class AssistantRuntimeConfigLoader:
    def __init__(self, assistant_service: AssistantService):
        log.debugx(
            "AssistantRuntimeConfigLoader initialiseren",
            has_assistant_service=assistant_service is not None,
        )
        self.assistant_service = assistant_service

        # AssistantService heeft in jouw services vrijwel zeker een repository met db.
        # We halen de actieve SQLAlchemy Session hieruit zodat de skill repositories
        # dezelfde sessie gebruiken.
        self.db = self._resolve_db_from_assistant_service(assistant_service)

        log.debugx(
            "AssistantRuntimeConfigLoader geïnitialiseerd",
            has_db=self.db is not None,
        )

    @staticmethod
    def _resolve_db_from_assistant_service(assistant_service: AssistantService):
        repo = getattr(assistant_service, "repository", None)
        db = getattr(repo, "db", None)

        if db is not None:
            return db

        # Fallback als AssistantService zelf db bewaart.
        db = getattr(assistant_service, "db", None)
        if db is not None:
            return db

        raise RuntimeError(
            "AssistantRuntimeConfigLoader kan geen db sessie vinden. "
            "Verwacht assistant_service.repository.db of assistant_service.db."
        )

    # ------------------------------------------------------------------
    # Tool / Skill conversion
    # ------------------------------------------------------------------
    def _tool_to_config(self, tool) -> ToolConfig:
        log.debugx(
            "Tool converteren naar ToolConfig gestart",
            tool_id=getattr(tool, "id", None),
            tool_name=getattr(tool, "name", None),
            tool_type=getattr(tool, "type", None),
            is_enabled=getattr(tool, "is_enabled", True),
        )

        result = ToolConfig(
            id=getattr(tool, "id", None),
            name=getattr(tool, "name", "") or "",
            remote_name=getattr(tool, "remote_name", None),
            description=getattr(tool, "description", "") or "",
            argument=getattr(tool, "argument", None) or {},
            output_schema=getattr(tool, "output_schema", None),
            annotations=getattr(tool, "annotations", None) or {},
            meta=getattr(tool, "meta", None) or {},
            type=getattr(tool, "type", "") or "",
            tool_instructions=getattr(tool, "tool_instructions", "") or "",
            is_enabled=bool(getattr(tool, "is_enabled", True)),
        )

        log.debugx(
            "Tool converteren naar ToolConfig afgerond",
            tool_id=result.id,
            tool_name=result.name,
            tool_type=result.type,
            is_enabled=result.is_enabled,
        )
        return result

    def _skill_to_config(self, skill, tools=None) -> SkillConfig:
        log.debugx(
            "Skill converteren naar SkillConfig gestart",
            skill_id=getattr(skill, "id", None),
            skill_name=getattr(skill, "name", None),
            is_system=getattr(skill, "is_system", False),
            is_enabled=getattr(skill, "is_enabled", True),
            tool_count=len(tools or []),
        )

        skill_file_service = SkillFileService(self.db)
        skill_file_manifest = skill_file_service.manifest_metadata_for_skill(int(skill.id))

        # System/runtime skills are code-authoritative: their description +
        # instructions come from code, not the database.
        from services.assistants.runtime.system_skills import skill_override
        _override = skill_override(getattr(skill, "name", "") or "")
        _instructions = _override["instructions"] if _override else (getattr(skill, "instructions", "") or "")
        _description = (
            _override["description"] if _override and _override.get("description")
            else (getattr(skill, "description", "") or "")
        )

        result = SkillConfig(
            id=int(skill.id),
            name=skill.name,
            display_name=getattr(skill, "display_name", None),
            description=_description,
            instructions=_instructions,
            input_schema=getattr(skill, "input_schema", None),
            output_schema=getattr(skill, "output_schema", None),
            is_system=bool(getattr(skill, "is_system", False)),
            is_runtime=bool(getattr(skill, "is_runtime", False)),
            is_enabled=bool(getattr(skill, "is_enabled", True)),
            priority=int(getattr(skill, "priority", 100) or 100),
            source=getattr(skill, "source", "local") or "local",
            source_name=getattr(skill, "source_name", None),
            version=getattr(skill, "version", "1.0.0") or "1.0.0",
            tools=[self._tool_to_config(t) for t in (tools or [])],
            skill_files_root=skill_file_manifest["skill_files_root"],
            skill_files=[
                SkillFileConfig(**item)
                for item in skill_file_manifest["skill_files"]
            ],
        )

        log.debugx(
            "Skill converteren naar SkillConfig afgerond",
            skill_id=result.id,
            skill_name=result.name,
            is_system=result.is_system,
            tool_count=len(result.tools),
        )
        return result

    def _load_tools_for_skill(self, skill_id: int) -> list[Any]:
        skill_tool_repo = SkillToolRepository(self.db)

        rows = skill_tool_repo.get_for_skill(
            skill_id,
            enabled_only=True,
        )

        return [tool for _, tool in rows]

    def _tool_ids_in_selectable_skills(self) -> set[int]:
        """Tool ids provided by at least one enabled, **user-selectable** skill
        (non-system, non-runtime — the same set the agent may pick via
        `select_skills`). Such tools can be loaded on demand, so they need not be
        injected into every planner prompt as always-on builtins."""
        from models.skill import Skill
        from models.skill_tool import SkillTool
        try:
            rows = (
                self.db.query(SkillTool.tool_id)
                .join(Skill, Skill.id == SkillTool.skill_id)
                .filter(
                    Skill.is_enabled == True,   # noqa: E712
                    Skill.is_system == False,   # noqa: E712
                    Skill.is_runtime == False,  # noqa: E712
                )
                .all()
            )
            return {r[0] for r in rows if r[0] is not None}
        except Exception:  # noqa: BLE001 — never break config build on this optimisation
            return set()

    def _load_builtin_always_on_tools(self) -> list[ToolConfig]:
        """Enabled Builtin-server tools the agent can use on every turn without first
        selecting a skill (shell, file read/inspect, web search, agent/task ops, …).

        To keep the planner prompt lean, builtin tools that are ALSO reachable via an
        enabled, user-selectable skill are TRIMMED here — the agent loads those on
        demand with `select_skills` instead of carrying them in every prompt (e.g. the
        9 `transfer_*` tools live in the `transfer_route_building` skill; `pdf__render`
        in `pdf_document_rendering`). This is safe by construction: a tool is only
        dropped when a selectable skill provides it, so it's never made unreachable.
        Tools whose only skill is runtime/system/disabled stay always-on.
        Execution still guards mutating tools (confirmation) and runs by verified
        tool_id."""
        from repository.tool_repository import ToolRepository
        from models.mcp_server import MCPServer

        server = (
            self.db.query(MCPServer)
            .filter(MCPServer.name == "Builtin", MCPServer.is_enabled == True)  # noqa: E712
            .first()
        )
        if server is None:
            log.warningx("Builtin MCP server niet gevonden — geen always-on tools")
            return []
        tools = ToolRepository(self.db).get_all_for_server(server.id, only_enabled=True)
        on_demand = self._tool_ids_in_selectable_skills()

        # Core builtin capabilities stay always-on even if a selectable skill also
        # lists them — these are fundamental and we never want to make the agent
        # select a skill to read a file, save/search its scratch store, run a shell
        # command, search the web, or query data.
        core_prefixes = (
            "text__", "file_", "json_inspect", "system__shell_exec",
            "web_search", "fabric_data_agent_query", "agent__", "task__",
        )

        def _is_core(name: str) -> bool:
            name = (name or "").strip()
            return any(name == c or name.startswith(c) for c in core_prefixes)

        kept = [
            t for t in tools
            if getattr(t, "id", None) not in on_demand or _is_core(getattr(t, "name", ""))
        ]
        log.debugx(
            "Always-on builtin tools getrimmed",
            total=len(tools),
            always_on=len(kept),
            moved_behind_skills=len(tools) - len(kept),
        )
        return [self._tool_to_config(t) for t in kept]

    def _load_system_skills(self) -> list[SkillConfig]:
        skill_repo = SkillRepository(self.db)

        out: list[SkillConfig] = []

        for skill in skill_repo.get_system_skills():
            tools = self._load_tools_for_skill(skill.id)
            out.append(self._skill_to_config(skill, tools=tools))

        log.debugx(
            "System skills geladen",
            count=len(out),
            skill_names=[s.name for s in out],
        )
        return out

    def _load_runtime_skills(self) -> list[SkillConfig]:
        skill_repo = SkillRepository(self.db)

        out: list[SkillConfig] = []

        for skill in skill_repo.get_runtime_skills():
            tools = self._load_tools_for_skill(skill.id)
            out.append(self._skill_to_config(skill, tools=tools))

        log.debugx(
            "Runtime skills geladen",
            count=len(out),
            skill_names=[s.name for s in out],
        )
        return out

    def _load_assistant_skills(self, assistant_id: int) -> list[SkillConfig]:
        assistant_skill_repo = AssistantSkillRepository(self.db)

        out: list[SkillConfig] = []

        rows = assistant_skill_repo.get_for_assistant(
            assistant_id,
            enabled_only=True,
        )

        for _, skill in rows:
            tools = self._load_tools_for_skill(skill.id)
            out.append(self._skill_to_config(skill, tools=tools))

        log.debugx(
            "Assistant skills geladen",
            assistant_id=assistant_id,
            count=len(out),
            skill_names=[s.name for s in out],
        )
        return out

    def _load_normal_skills(self) -> list[SkillConfig]:
        """All enabled, non-system, non-runtime skills — the single agent's catalog."""
        skill_repo = SkillRepository(self.db)

        out: list[SkillConfig] = []
        seen: set[str] = set()
        for skill in skill_repo.get_all(skip=0, limit=10000, include_disabled=False):
            if getattr(skill, "is_system", False) or getattr(skill, "is_runtime", False):
                continue
            if skill.name in seen:
                continue
            seen.add(skill.name)
            tools = self._load_tools_for_skill(skill.id)
            out.append(self._skill_to_config(skill, tools=tools))

        log.debugx(
            "Normale skills geladen (single-agent catalogus)",
            count=len(out),
            skill_names=[s.name for s in out],
        )
        return out

    def _attach_skills_to_config(self, config: AssistantConfig) -> AssistantConfig:
        system_skills = self._load_system_skills()
        runtime_skills = self._load_runtime_skills()

        assistant_skills: list[SkillConfig] = []
        if getattr(config, "id", None):
            assistant_skills = self._load_assistant_skills(int(config.id))

        by_name: dict[str, SkillConfig] = {}

        # Normale assistant skills
        for skill in assistant_skills:
            by_name[skill.name] = skill

        # System skills zijn altijd actief.
        # Als er per ongeluk een assistant skill dezelfde naam heeft,
        # wint de system skill.
        for skill in system_skills:
            by_name[skill.name] = skill

        for skill in runtime_skills:
            by_name[skill.name] = skill

        config.skills = sorted(
            by_name.values(),
            key=lambda s: (not s.is_system, not s.is_runtime, s.priority, s.name.lower()),
        )

        # Belangrijk: assistant_tool is vanaf nu legacy en niet meer runtime-authority.
        config.tools = []

        log.debugx(
            "Skills aan AssistantConfig gekoppeld",
            assistant_id=config.id,
            assistant_name=config.name,
            skill_count=len(config.skills),
            system_skill_count=len([s for s in config.skills if s.is_system]),
            selected_skill_count=len([s for s in config.skills if not s.is_system]),
            tool_count=len(config.tools),
        )

        return config

    # ------------------------------------------------------------------
    # Assistant conversion
    # ------------------------------------------------------------------
    def _assistant_to_config(self, assistant) -> AssistantConfig:
        log.debugx(
            "Assistant converteren naar AssistantConfig gestart",
            assistant_id=getattr(assistant, "id", None),
            assistant_name=getattr(assistant, "name", None),
            assistant_type=getattr(assistant, "assistant_type", "planner"),
            is_active=getattr(assistant, "is_active", None),
            is_router_selectable=getattr(assistant, "is_router_selectable", True),
            source_tool_count=len(getattr(assistant, "tools", []) or []),
        )

        result = AssistantConfig(
            id=getattr(assistant, "id", None),
            name=getattr(assistant, "name", "") or "",
            description=getattr(assistant, "description", "") or "",
            instruction=getattr(assistant, "instruction", "") or "",
            schema=getattr(assistant, "schema", None) or {},
            assistant_type=getattr(assistant, "assistant_type", "planner") or "planner",
            routing_tags=getattr(assistant, "routing_tags", []) or [],
            model=getattr(assistant, "model", None),
            temperature=getattr(assistant, "temperature", None),
            priority=getattr(assistant, "priority", 100) or 100,
            is_active=bool(getattr(assistant, "is_active", True)),
            is_router_selectable=bool(getattr(assistant, "is_router_selectable", True)),

            # Legacy path uit:
            tools=[],

            # Wordt direct hieronder gevuld.
            skills=[],
        )

        result = self._attach_skills_to_config(result)

        # Code-authoritative schema (router/planner/final_answer) en instructie
        # (router/final_answer) afdwingen; DB-waarden worden hiervoor genegeerd.
        from services.assistants.runtime.system_assistants import apply_system_overrides
        result = apply_system_overrides(result)

        log.debugx(
            "Assistant converteren naar AssistantConfig afgerond",
            assistant_id=result.id,
            assistant_name=result.name,
            assistant_type=result.assistant_type,
            skill_count=len(result.skills),
            tool_count=len(result.tools),
            priority=result.priority,
            is_active=result.is_active,
            is_router_selectable=result.is_router_selectable,
        )
        return result

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def get_by_id(self, assistant_id: int) -> AssistantConfig:
        log.infox(
            "Runtime assistant config ophalen op ID gestart",
            assistant_id=assistant_id,
        )

        obj = self.assistant_service.get_with_relations(assistant_id)
        result = self._assistant_to_config(obj)

        log.infox(
            "Runtime assistant config ophalen op ID afgerond",
            assistant_id=assistant_id,
            assistant_name=result.name,
            assistant_type=result.assistant_type,
            skill_count=len(result.skills),
            tool_count=len(result.tools),
        )
        return result

    def get_by_name(self, name: str) -> AssistantConfig:
        log.infox(
            "Runtime assistant config ophalen op naam gestart",
            name=name,
        )

        items = self.assistant_service.get_all_with_relations(skip=0, limit=1000)

        log.debugx(
            "Assistants geladen voor lookup op naam",
            name=name,
            count=len(items) if items is not None else None,
        )

        for obj in items:
            if obj.name == name:
                result = self._assistant_to_config(obj)

                log.infox(
                    "Runtime assistant config gevonden op naam",
                    name=name,
                    assistant_id=result.id,
                    assistant_type=result.assistant_type,
                    skill_count=len(result.skills),
                    tool_count=len(result.tools),
                )
                return result

        log.warningx(
            "Runtime assistant config niet gevonden op naam",
            name=name,
            checked_count=len(items) if items is not None else None,
        )
        raise ValueError(f"Assistant not found by name: {name}")

    # Workflow-specific explicit lookup. It does not filter by is_active,
    # is_router_selectable, or assistant_type. This lets a stored workflow run the
    # exact configured assistant, including router and final_answer assistants.
    def get_by_id_for_workflow(self, assistant_id: int) -> AssistantConfig:
        log.infox(
            "Workflow runtime assistant config ophalen op ID gestart",
            assistant_id=assistant_id,
        )

        obj = self.assistant_service.get_with_relations(assistant_id)
        result = self._assistant_to_config(obj)

        log.infox(
            "Workflow runtime assistant config ophalen op ID afgerond",
            assistant_id=assistant_id,
            assistant_name=result.name,
            assistant_type=result.assistant_type,
            skill_count=len(result.skills),
            tool_count=len(result.tools),
        )
        return result

    def get_by_name_for_workflow(self, name: str) -> AssistantConfig:
        log.infox(
            "Workflow runtime assistant config ophalen op naam gestart",
            name=name,
        )

        items = self.assistant_service.get_all_with_relations(skip=0, limit=1000)

        for obj in items:
            if obj.name == name:
                result = self._assistant_to_config(obj)

                log.infox(
                    "Workflow runtime assistant config gevonden op naam",
                    name=name,
                    assistant_id=result.id,
                    assistant_type=result.assistant_type,
                    skill_count=len(result.skills),
                    tool_count=len(result.tools),
                )
                return result

        log.warningx(
            "Workflow runtime assistant config niet gevonden op naam",
            name=name,
            checked_count=len(items) if items is not None else None,
        )
        raise ValueError(f"Assistant not found by name: {name}")

    # ------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------
    def list_for_workflow_catalog(self) -> List[AssistantConfig]:
        log.infox("Workflow assistant catalog ophalen gestart")

        items = self.assistant_service.get_all_with_relations(skip=0, limit=1000)

        result = sorted(
            [self._assistant_to_config(a) for a in items],
            key=lambda x: (x.priority, x.name.lower()),
        )

        log.infox(
            "Workflow assistant catalog ophalen afgerond",
            count=len(result),
            assistant_names=[a.name for a in result],
        )
        return result

    def list_active(self) -> List[AssistantConfig]:
        log.infox("Actieve assistant configs ophalen gestart")

        items = self.assistant_service.get_all_with_relations(skip=0, limit=1000)

        result = [
            self._assistant_to_config(a)
            for a in items
            if getattr(a, "is_active", True)
        ]

        log.infox(
            "Actieve assistant configs ophalen afgerond",
            count=len(result),
            assistant_names=[a.name for a in result],
        )
        return result

    def list_router_selectable(self) -> List[Dict[str, Any]]:
        """
        Router payload moet niet alleen assistants bevatten,
        maar ook de skills waaruit de router verplicht moet kiezen.
        """
        log.infox("Router-selecteerbare assistant configs ophalen gestart")

        assistants = sorted(
            [
                a for a in self.list_active()
                if a.is_router_selectable and a.assistant_type == "planner"
            ],
            key=lambda x: (x.priority, x.name.lower()),
        )

        result: List[Dict[str, Any]] = []

        for assistant in assistants:
            result.append(
                {
                    "id": assistant.id,
                    "name": assistant.name,
                    "description": assistant.description,
                    "assistant_type": assistant.assistant_type,
                    "routing_tags": assistant.routing_tags,
                    "priority": assistant.priority,
                    "skills": [
                        {
                            "id": skill.id,
                            "name": skill.name,
                            "display_name": skill.display_name,
                            "description": skill.description,
                            "priority": skill.priority,
                            "tool_count": len(skill.tools),
                        }
                        for skill in assistant.skills
                        if skill.is_enabled and not skill.is_system
                    ],
                }
            )

        log.infox(
            "Router-selecteerbare assistant configs ophalen afgerond",
            count=len(result),
            assistant_names=[a["name"] for a in result],
        )
        return result

    def list_agent_skill_catalog(self) -> List[Dict[str, Any]]:
        """
        Single-agent model: the agent selects from ONE flat skill catalog instead
        of router-chosen per-assistant skill sets. Returns every enabled, non-system
        skill (deduplicated by name) with the description it is selected on and its
        tool_count. System/runtime skills are excluded (injected automatically).
        """
        log.infox("Agent skill-catalogus ophalen gestart")

        # The catalog is exactly the agent's own selectable skills, so it tracks the
        # DB-managed "Agent" (or the synthesized fallback) without divergence.
        agent = self.get_single_agent()
        result = [
            {
                "id": skill.id,
                "name": skill.name,
                "display_name": skill.display_name,
                "description": skill.description,
                "priority": skill.priority,
                "tool_count": len(skill.tools),
            }
            for skill in agent.skills
            if not skill.is_system and not skill.is_runtime
        ]
        result.sort(key=lambda s: (s["priority"], s["name"]))

        log.infox(
            "Agent skill-catalogus ophalen afgerond",
            skill_count=len(result),
            skill_names=[s["name"] for s in result],
        )
        return result

    # ------------------------------------------------------------------
    # Special assistants
    # ------------------------------------------------------------------
    def get_single_agent(self) -> AssistantConfig:
        """
        Single-agent model: ONE agent whose skill catalog is every enabled non-system
        skill. Which skill(s) apply is decided per turn (chat: the skill-selection
        step; workflow: the operation's pre-given skill) and passed via
        _selected_skill_names — the pipeline resolves/guards/injects them as before.
        Prefers a DB-managed "Agent" assistant row (its attached skills); the
        instruction is sourced from the editable repo markdown file
        runtime/system_specs/agent.instruction.md (UI edits write that file). Falls
        back to a synthesized agent over all enabled skills if no Agent row exists.
        """
        from pathlib import Path
        spec = Path(__file__).resolve().parent / "runtime" / "system_specs" / "agent.instruction.md"
        file_instruction = None
        try:
            file_instruction = (spec.read_text(encoding="utf-8").strip() or None)
        except Exception:
            file_instruction = None

        try:
            cfg = self.get_by_name("Agent")
        except Exception:
            cfg = None
        if cfg is not None:
            if file_instruction:
                cfg.instruction = file_instruction
            # Builtin tools are always available, independent of skill selection.
            cfg.tools = self._load_builtin_always_on_tools()
            return cfg

        log.infox("Single-agent runtime config opbouwen gestart")

        config = AssistantConfig(
            id=None,
            name="Agent",
            description="The single workspace agent (skill-driven).",
            instruction=(
                "You are the assistant for this workspace. You have been given the "
                "skill(s) relevant to the user's request together with their tools. "
                "Use them to fulfil the request — call tools by verified tool_id, never "
                "claim a mutation succeeded unless its tool call succeeded — then write a "
                "clear final answer. Follow the system contracts."
            ),
            schema={},
            assistant_type="planner",
            routing_tags=[],
            model=None,
            temperature=None,
            priority=0,
            is_active=True,
            is_router_selectable=False,
            tools=[],
            skills=[],
        )

        by_name: dict[str, SkillConfig] = {}
        for skill in self._load_normal_skills():
            by_name[skill.name] = skill
        for skill in self._load_system_skills():  # system skills always win on name clash
            by_name[skill.name] = skill
        for skill in self._load_runtime_skills():
            by_name[skill.name] = skill

        config.skills = sorted(
            by_name.values(),
            key=lambda s: (not s.is_system, not s.is_runtime, s.priority, s.name.lower()),
        )

        config.tools = self._load_builtin_always_on_tools()

        from services.assistants.runtime.system_assistants import apply_system_overrides
        config = apply_system_overrides(config)  # planner schema (code-authoritative)

        log.infox(
            "Single-agent runtime config opgebouwd",
            skill_count=len(config.skills),
            normal_skill_count=len([s for s in config.skills if not s.is_system and not s.is_runtime]),
            system_skill_count=len([s for s in config.skills if s.is_system]),
        )
        return config

    def get_router(self) -> AssistantConfig:
        log.infox("Actieve router assistant ophalen gestart")

        routers = [
            a for a in self.list_active()
            if a.assistant_type == "router"
        ]

        log.debugx(
            "Router assistants gefilterd",
            count=len(routers),
            router_names=[a.name for a in routers],
        )

        if not routers:
            log.errorx("Geen actieve router assistant gevonden")
            raise ValueError("No active router assistant found")

        result = sorted(routers, key=lambda x: (x.priority, x.name.lower()))[0]

        log.infox(
            "Actieve router assistant geselecteerd",
            assistant_id=result.id,
            assistant_name=result.name,
            priority=result.priority,
            skill_count=len(result.skills),
        )
        return result

    def get_final_answer(self) -> AssistantConfig:
        log.infox("Actieve final-answer assistant ophalen gestart")

        finalizers = [
            a for a in self.list_active()
            if a.assistant_type == "final_answer"
        ]

        log.debugx(
            "Final-answer assistants gefilterd",
            count=len(finalizers),
            assistant_names=[a.name for a in finalizers],
        )

        if not finalizers:
            log.errorx("Geen actieve final-answer assistant gevonden")
            raise ValueError("No active final-answer assistant found")

        result = sorted(finalizers, key=lambda x: (x.priority, x.name.lower()))[0]

        log.infox(
            "Actieve final-answer assistant geselecteerd",
            assistant_id=result.id,
            assistant_name=result.name,
            priority=result.priority,
            skill_count=len(result.skills),
        )
        return result