from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Set

from component.logging import get_logger
from services.assistants.runtime_config import AssistantConfig

log = get_logger(__name__)

DEFAULT_MUTATION_TOOLS = {
    "text_update",
    "text_delete",
}


class AssistantToolGuard:
    def is_mutation_tool(self, tool_name: str) -> bool:
        return (tool_name or "").strip() in DEFAULT_MUTATION_TOOLS

    def _active_skills(
        self,
        assistant: AssistantConfig,
        selected_skill_names: Optional[list[str]] = None,
    ):
        selected = set(selected_skill_names or [])
        active = []

        for skill in assistant.skills or []:
            if not getattr(skill, "is_enabled", True):
                continue

            if getattr(skill, "is_system", False):
                active.append(skill)
                continue

            if getattr(skill, "is_runtime", False) and skill.name in selected:
                active.append(skill)
                continue

            if skill.name in selected:
                active.append(skill)

        return active

    def allowed_skill_names_for(self, assistant: AssistantConfig) -> Set[str]:
        return {
            skill.name
            for skill in assistant.skills or []
            if getattr(skill, "is_enabled", True)
            and not getattr(skill, "is_system", False)
        }

    def assert_selected_skills_allowed(
        self,
        assistant: AssistantConfig,
        selected_skill_names: list[str],
    ) -> None:
        if not selected_skill_names:
            raise ValueError(
                f"Assistant '{assistant.name}' requires one or more selected skills."
            )

        allowed = self.allowed_skill_names_for(assistant)
        missing = [name for name in selected_skill_names if name not in allowed]

        if missing:
            raise ValueError(
                f"Assistant '{assistant.name}' is not allowed to use skill(s): {missing}. "
                f"Allowed skills: {sorted(allowed)}"
            )

    def allowed_tool_ids_for(
        self,
        assistant: AssistantConfig,
        *,
        selected_skill_names: Optional[list[str]] = None,
    ) -> Set[int]:
        out: Set[int] = set()

        # Always-on builtin tools (config.tools) are callable on every turn, regardless of
        # which skills are selected — they're shown in the manifest as "always-available",
        # so the guard must allow them too.
        for tool in getattr(assistant, "tools", None) or []:
            if getattr(tool, "is_enabled", True) and getattr(tool, "id", None) is not None:
                out.add(int(tool.id))

        for skill in self._active_skills(
            assistant,
            selected_skill_names=selected_skill_names,
        ):
            for tool in skill.tools or []:
                if getattr(tool, "is_enabled", True) and getattr(tool, "id", None) is not None:
                    out.add(int(tool.id))

        return out

    def allowed_tool_names_for(
        self,
        assistant: AssistantConfig,
        *,
        selected_skill_names: Optional[list[str]] = None,
    ) -> Set[str]:
        out: Set[str] = set()

        # Always-on builtin tools (see allowed_tool_ids_for).
        for tool in getattr(assistant, "tools", None) or []:
            if getattr(tool, "is_enabled", True):
                name = (getattr(tool, "name", "") or "").strip()
                if name:
                    out.add(name)

        for skill in self._active_skills(
            assistant,
            selected_skill_names=selected_skill_names,
        ):
            for tool in skill.tools or []:
                if getattr(tool, "is_enabled", True):
                    name = (getattr(tool, "name", "") or "").strip()
                    if name:
                        out.add(name)

        return out

    def assert_tools_allowed(
        self,
        assistant: AssistantConfig,
        tool_calls: Iterable[Dict[str, Any]],
        *,
        selected_skill_names: Optional[list[str]] = None,
    ) -> None:
        selected_skill_names = selected_skill_names or []

        allowed_ids = self.allowed_tool_ids_for(
            assistant,
            selected_skill_names=selected_skill_names,
        )
        allowed_names = self.allowed_tool_names_for(
            assistant,
            selected_skill_names=selected_skill_names,
        )

        for tc in tool_calls:
            tool = (tc.get("tool") or "").strip()
            tool_id = tc.get("tool_id")

            if tool_id is None:
                raise ValueError(
                    f"Planner returned a dynamic tool call without tool_id for tool={tool!r}."
                )

            try:
                tool_id_int = int(tool_id)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Planner returned invalid tool_id={tool_id!r} for tool={tool!r}."
                )

            if tool_id_int not in allowed_ids:
                raise ValueError(
                    f"Assistant '{assistant.name}' is not allowed to call tool_id={tool_id_int} "
                    f"with selected skills={selected_skill_names}. "
                    f"Allowed tool ids: {sorted(allowed_ids)}"
                )

            if tool and allowed_names and tool not in allowed_names:
                raise ValueError(
                    f"Assistant '{assistant.name}' is not allowed to call tool={tool!r} "
                    f"with selected skills={selected_skill_names}. "
                    f"Allowed tools: {sorted(allowed_names)}"
                )