from component.logging import get_logger
from services.assistants.orchestration.metadata import LOOKUP_TOOLS
from services.assistants.orchestration.placeholder import _find_placeholder_result_indices


log = get_logger(__name__)


def _plan_has_tool(tool_calls, name: str) -> bool:
    log.debugx(
        "Controleren of plan tool bevat gestart",
        tool_call_count=len(tool_calls or []),
        target_tool=name,
    )
    result = any((tc.get("tool") or "").strip() == name for tc in (tool_calls or []))
    log.debugx(
        "Controleren of plan tool bevat afgerond",
        target_tool=name,
        result=result,
    )
    return result

def _guard_lookup_placeholders(tool_calls):
    log.infox(
        "Lookup placeholder guard gestart",
        tool_call_count=len(tool_calls or []),
        lookup_tools=list(LOOKUP_TOOLS),
    )

    lookup_indices = {i for i, tc in enumerate(tool_calls) if (tc.get("tool") or "").strip() in LOOKUP_TOOLS}
    max_lookup_index = max(lookup_indices) if lookup_indices else -1
    tool = None
    result = True
    used = None
    args = None

    log.debugx(
        "Lookup tool indices bepaald",
        lookup_indices=sorted(lookup_indices),
        max_lookup_index=max_lookup_index,
    )

    for i, tc in enumerate(tool_calls):
        args = tc.get("args") or {}
        used = _find_placeholder_result_indices(args)

        log.debugx(
            "Tool call placeholders gecontroleerd",
            index=i,
            tool=(tc.get("tool") or "").strip(),
            arg_keys=list(args.keys()) if isinstance(args, dict) else None,
            used_placeholder_indices=sorted(used) if used is not None else None,
            max_lookup_index=max_lookup_index,
        )

        if max_lookup_index >= 0 and any(idx <= max_lookup_index for idx in used):
            tool = (tc.get("tool") or "").strip()
            result = False
            log.warningx(
                "Lookup placeholder guard geblokkeerd",
                index=i,
                tool=tool,
                used_placeholder_indices=sorted(used),
                max_lookup_index=max_lookup_index,
            )
            break

    log.infox(
        "Lookup placeholder guard afgerond",
        result=result,
        blocked_tool=tool,
        max_lookup_index=max_lookup_index,
        used_placeholder_indices=sorted(used) if used is not None else None,
    )
    return tool, args, used, result, max_lookup_index