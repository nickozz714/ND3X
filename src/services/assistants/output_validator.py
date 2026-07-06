from __future__ import annotations

from typing import Any

from jsonschema import validate

from component.logging import get_logger


log = get_logger(__name__)


class AssistantOutputValidator:
    def validate(self, schema: dict[str, Any] | str, obj: Any) -> None:
        log.debugx(
            "Assistant output validatie gestart",
            has_schema=bool(schema),
            schema_is_dict=isinstance(schema, dict),
            object_type=type(obj).__name__,
        )

        if not schema or not isinstance(schema, dict):
            log.debugx(
                "Assistant output validatie overgeslagen: schema ontbreekt of is geen dict",
                has_schema=bool(schema),
                schema_type=type(schema).__name__,
            )
            return

        validate(instance=obj, schema=schema)

        log.debugx(
            "Assistant output validatie succesvol afgerond",
            schema_keys=list(schema.keys()),
            object_type=type(obj).__name__,
        )

    def ensure_router_shape(self, obj: dict[str, Any]) -> None:
        log.debugx(
            "Router shape validatie gestart",
            object_keys=list(obj.keys()) if isinstance(obj, dict) else None,
            mode=obj.get("mode") if isinstance(obj, dict) else None,
        )

        mode = obj.get("mode")
        allowed = {"single", "multi", "ask_user", "direct_answer", "workflow_offer", "workflow_trigger"}
        if mode not in allowed:
            log.warningx(
                "Router shape validatie mislukt: ongeldige router mode",
                mode=mode,
                allowed_modes=sorted(allowed),
            )
            raise ValueError("Invalid router mode")

        log.debugx(
            "Router shape validatie succesvol afgerond",
            mode=mode,
        )