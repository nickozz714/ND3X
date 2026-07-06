from __future__ import annotations

from typing import Any, Dict, Iterable

RUNTIME_FILE_SKILL_NAME = "runtime_file_artifact_inspection"
INTENT_TERMS = [
    "bestand", "file", "document", "download", "inspecteer", "analyseer", "lees", "zoek in", "csv", "json",
    "notebook", "ipynb", "parquet", "excel", "pdf", "zip", "code", "artifact", "content_ref", "local_path",
]


def _contains_term(text: str) -> bool:
    t = (text or "").lower()
    return any(term in t for term in INTENT_TERMS)


def _has_artifact_markers(obj: Any) -> bool:
    if isinstance(obj, dict):
        keys = set(obj.keys())
        if {"content_ref", "local_path"} & keys:
            return True
        if "artifacts" in keys and obj.get("artifacts"):
            return True
        if obj.get("inspection_level") in {"preview_only", "artifact_only"}:
            return True
        if obj.get("full_content_available_to_llm") is False and ({"mime_type", "size_bytes", "content_ref", "local_path"} & keys):
            return True
        return any(_has_artifact_markers(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_artifact_markers(v) for v in obj)
    return False


def should_attach_file_artifact_runtime_skill(*, question: str, payload: Dict[str, Any]) -> bool:
    if _contains_term(question or ""):
        return True
    if _has_artifact_markers(payload or {}):
        return True
    return False


def resolve_effective_selected_skills(*, base_selected_skill_names: Iterable[str], assistant_skills: Iterable[Any], question: str, payload: Dict[str, Any]) -> list[str]:
    selected = [str(x).strip() for x in (base_selected_skill_names or []) if str(x).strip()]
    seen = set(selected)

    runtime_names = [getattr(s, "name", "") for s in (assistant_skills or []) if getattr(s, "is_enabled", True) and getattr(s, "is_runtime", False)]
    if RUNTIME_FILE_SKILL_NAME in runtime_names and should_attach_file_artifact_runtime_skill(question=question, payload=payload):
        if RUNTIME_FILE_SKILL_NAME not in seen:
            selected.append(RUNTIME_FILE_SKILL_NAME)
    return selected
