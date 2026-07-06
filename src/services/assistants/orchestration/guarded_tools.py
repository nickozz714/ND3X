from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from component.logging import get_logger

log = get_logger(__name__)

GUARD_TYPE_CONFIRMATION_REQUIRED = "confirmation_required"
MAX_SHELL_TIMEOUT_SECONDS = 120.0

GUARDED_TOOL_POLICIES: Dict[str, Dict[str, Any]] = {
    "system__shell_exec": {
        "guard_type": GUARD_TYPE_CONFIRMATION_REQUIRED,
        "risk_level": "high",
        "message": "Shell execution requires confirmation.",
        "confirmation_prompt": "Run this shell command?",
        "display_args": ["command", "timeout"],
        "max_timeout_seconds": MAX_SHELL_TIMEOUT_SECONDS,
    }
}

DANGEROUS_SHELL_PATTERNS = (
    "rm -rf /",
    "rm -fr /",
    "mkfs",
    "shutdown",
    "reboot",
    ":(){ :|:& };:",
)


@dataclass(frozen=True)
class GuardedToolValidation:
    tool_call: Dict[str, Any]
    display: Dict[str, Any]
    tool_call_hash: str


def get_guarded_tool_policy(tool_name: str) -> Optional[Dict[str, Any]]:
    policy = GUARDED_TOOL_POLICIES.get((tool_name or "").strip())
    return deepcopy(policy) if policy else None


def is_guarded_tool(tool_name: str) -> bool:
    return get_guarded_tool_policy(tool_name) is not None


def canonical_tool_call_payload(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    tool_id = tool_call.get("tool_id")
    try:
        tool_id = int(tool_id) if tool_id is not None else None
    except (TypeError, ValueError):
        pass
    return {
        "tool": (tool_call.get("tool") or "").strip(),
        "tool_id": tool_id,
        "args": args,
    }


def canonical_tool_call_json(tool_call: Dict[str, Any]) -> str:
    return json.dumps(
        canonical_tool_call_payload(tool_call),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def tool_call_hash(tool_call: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_tool_call_json(tool_call).encode("utf-8")).hexdigest()


def _coerce_shell_timeout(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("system__shell_exec timeout must be numeric.")
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError("system__shell_exec timeout must be numeric.")
    if timeout <= 0:
        raise ValueError("system__shell_exec timeout must be greater than 0.")
    if timeout > MAX_SHELL_TIMEOUT_SECONDS:
        raise ValueError(
            f"system__shell_exec timeout must be <= {int(MAX_SHELL_TIMEOUT_SECONDS)} seconds."
        )
    return timeout


def validate_guarded_tool_call(tool_call: Dict[str, Any]) -> GuardedToolValidation:
    tool = (tool_call.get("tool") or "").strip()
    policy = get_guarded_tool_policy(tool)
    if not policy:
        raise ValueError(f"Tool {tool!r} is not guarded.")

    normalized = deepcopy(tool_call)
    args = normalized.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError(f"{tool} args must be an object.")

    if tool == "system__shell_exec":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("system__shell_exec command must be a non-empty string.")
        lowered_command = command.strip().lower()
        if any(pattern in lowered_command for pattern in DANGEROUS_SHELL_PATTERNS):
            raise ValueError("system__shell_exec command was rejected by shell safety policy.")
        timeout = _coerce_shell_timeout(args.get("timeout"))
        if timeout is not None:
            args = {**args, "timeout": timeout}
            normalized["args"] = args

    display = {
        key: args.get(key)
        for key in policy.get("display_args") or []
        if key in args
    }
    return GuardedToolValidation(
        tool_call=normalized,
        display=display,
        tool_call_hash=tool_call_hash(normalized),
    )


def build_tool_confirmation_pending_action(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    validation = validate_guarded_tool_call(tool_call)
    normalized = validation.tool_call
    tool = (normalized.get("tool") or "").strip()
    policy = get_guarded_tool_policy(tool) or {}
    pending = {
        "type": "tool_confirmation",
        "status": "pending",
        "guard_type": policy.get("guard_type"),
        "tool": tool,
        "tool_id": normalized.get("tool_id"),
        "risk_level": policy.get("risk_level"),
        "message": policy.get("message"),
        "confirmation_prompt": policy.get("confirmation_prompt"),
        "display": validation.display,
        "tool_call_hash": validation.tool_call_hash,
        "tool_call": normalized,
        "tool_calls": [normalized],
        "prompt": _format_tool_confirmation_prompt(policy, validation.display),
    }
    log.infox(
        "Guarded tool confirmation pending opgebouwd",
        tool=tool,
        tool_id=normalized.get("tool_id"),
        risk_level=policy.get("risk_level"),
        tool_call_hash=validation.tool_call_hash,
    )
    return pending


def _format_tool_confirmation_prompt(policy: Dict[str, Any], display: Dict[str, Any]) -> str:
    lines = [
        policy.get("message") or "Tool execution requires confirmation.",
        policy.get("confirmation_prompt") or "Run this tool?",
        "",
    ]
    for key, value in display.items():
        lines.append(f"- {key}: {value}")
    lines += ["", "Reply **yes** to confirm, or **no** to cancel."]
    return "\n".join(lines)


def first_guarded_tool_call(tool_calls: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for tc in tool_calls or []:
        if isinstance(tc, dict) and is_guarded_tool((tc.get("tool") or "").strip()):
            return tc
    return None


def verify_pending_tool_confirmation(pending_action: Dict[str, Any]) -> GuardedToolValidation:
    if (pending_action or {}).get("type") != "tool_confirmation":
        raise ValueError("Pending action is not a tool confirmation.")
    tool_call = pending_action.get("tool_call")
    if not isinstance(tool_call, dict):
        raise ValueError("Pending tool confirmation is missing the original tool call.")
    validation = validate_guarded_tool_call(tool_call)
    expected = pending_action.get("tool_call_hash")
    if not expected or expected != validation.tool_call_hash:
        raise ValueError("Pending tool confirmation hash mismatch; please request a new confirmation.")
    return validation


def guard_trace_data(pending_action: Dict[str, Any], *, confirmed: Optional[bool] = None) -> Dict[str, Any]:
    display = pending_action.get("display") or {}
    data = {
        "tool": pending_action.get("tool"),
        "risk_level": pending_action.get("risk_level"),
        "command_preview": display.get("command"),
        "timeout": display.get("timeout"),
        "tool_call_hash": pending_action.get("tool_call_hash"),
    }
    if confirmed is not None:
        data["confirmed"] = bool(confirmed)
        data["cancelled"] = not bool(confirmed)
    return data
