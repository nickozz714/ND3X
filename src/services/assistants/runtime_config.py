from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolConfig:
    id: Optional[int] = None
    name: str = ""
    remote_name: Optional[str] = None
    description: str = ""
    argument: dict[str, Any] = field(default_factory=dict)
    output_schema: Optional[dict[str, Any]] = None
    annotations: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    type: str = ""
    tool_instructions: str = ""
    is_enabled: bool = True


@dataclass
class SkillFileConfig:
    relative_path: str
    runtime_path: str
    content_type: Optional[str] = None
    size_bytes: int = 0
    checksum_sha256: str = ""
    is_executable: bool = False


@dataclass
class SkillConfig:
    id: int
    name: str
    display_name: Optional[str] = None
    description: str = ""
    instructions: str = ""
    input_schema: Optional[dict[str, Any]] = None
    output_schema: Optional[dict[str, Any]] = None
    is_system: bool = False
    is_runtime: bool = False
    is_enabled: bool = True
    priority: int = 100
    source: str = "local"
    source_name: Optional[str] = None
    version: str = "1.0.0"
    tools: list[ToolConfig] = field(default_factory=list)
    skill_files_root: Optional[str] = None
    skill_files: list[SkillFileConfig] = field(default_factory=list)


@dataclass
class AssistantConfig:
    id: Optional[int] = None
    name: str = ""
    description: str = ""
    instruction: str = ""
    schema: dict[str, Any] = field(default_factory=dict)

    assistant_type: str = "planner"
    routing_tags: list[str] = field(default_factory=list)

    model: Optional[str] = None
    temperature: Optional[float] = None
    priority: int = 100

    is_active: bool = True
    is_enabled: bool = True
    is_router_selectable: bool = True

    # New runtime authority
    skills: list[SkillConfig] = field(default_factory=list)

    # Deprecated runtime path. Keep for compatibility, but authorization should not use it.
    tools: list[ToolConfig] = field(default_factory=list)