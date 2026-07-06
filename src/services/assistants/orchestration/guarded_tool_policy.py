from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

SUPPORTED_OPERATORS = {"equals", "starts_with", "contains", "does_not_contain", "regex"}
SKILL_FILES_ROOT_VAR = "${skill_files_root}"


@dataclass(frozen=True)
class WorkflowGuardedToolPolicyDecision:
    tool: str
    allowed: bool
    auto_confirmed: bool
    on_denied: str
    reason: str
    matched_allow_rule: Optional[Dict[str, Any]] = None
    matched_deny_rule: Optional[Dict[str, Any]] = None

    def trace_data(self, *, command: str, working_dir: Optional[str]) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "command_preview": _command_preview(command),
            "working_dir": working_dir,
            "decision": "allowed" if self.allowed else "denied",
            "matched_allow_rule": self.matched_allow_rule,
            "matched_deny_rule": self.matched_deny_rule,
            "auto_confirmed": self.auto_confirmed,
            "denial_reason": None if self.allowed else self.reason,
        }


def _command_preview(command: str, *, max_len: int = 200) -> str:
    compact = " ".join(str(command or "").split())
    if len(compact) <= max_len:
        return compact
    return f"{compact[: max_len - 3]}..."


def _resolve_policy_value(value: Any, variables: Dict[str, Optional[str]]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if SKILL_FILES_ROOT_VAR in value:
        replacement = variables.get("skill_files_root")
        if not replacement:
            return None
        return value.replace(SKILL_FILES_ROOT_VAR, replacement)
    if "${" in value:
        return None
    return value


def _sanitize_rule(rule: Dict[str, Any], resolved_value: str) -> Dict[str, Any]:
    return {
        "operator": rule.get("operator"),
        "value": _command_preview(resolved_value),
    }


def _match_rule(candidate: str, rule: Dict[str, Any], variables: Dict[str, Optional[str]]) -> tuple[bool, Optional[Dict[str, Any]]]:
    if not isinstance(rule, dict):
        return False, None
    operator = rule.get("operator")
    if operator not in SUPPORTED_OPERATORS:
        return False, None
    value = _resolve_policy_value(rule.get("value"), variables)
    if value is None:
        return False, None

    matched = False
    if operator == "equals":
        matched = candidate == value
    elif operator == "starts_with":
        matched = candidate.startswith(value)
    elif operator == "contains":
        matched = value in candidate
    elif operator == "does_not_contain":
        matched = value not in candidate
    elif operator == "regex":
        try:
            matched = re.search(value, candidate) is not None
        except re.error:
            matched = False

    return matched, _sanitize_rule(rule, value) if matched else None


def _matches_allowed_working_dir(working_dir: Optional[str], allowed: list[Any], variables: Dict[str, Optional[str]]) -> bool:
    if not allowed:
        return True
    if not working_dir:
        return False

    try:
        normalized_working_dir = str(Path(working_dir).expanduser().resolve(strict=False))
    except (TypeError, OSError):
        return False

    for entry in allowed:
        resolved = _resolve_policy_value(entry, variables)
        if resolved is None:
            continue
        try:
            normalized_allowed = str(Path(resolved).expanduser().resolve(strict=False))
        except (TypeError, OSError):
            continue
        if normalized_working_dir == normalized_allowed:
            return True
    return False


def evaluate_workflow_guarded_tool_policy(
    *,
    tool: str,
    tool_args: Dict[str, Any],
    execution_policy: Optional[Dict[str, Any]],
    skill_files_root: Optional[str] = None,
) -> WorkflowGuardedToolPolicyDecision:
    tool = (tool or "").strip()
    args = tool_args or {}
    variables = {"skill_files_root": skill_files_root}

    policy = ((execution_policy or {}).get("guarded_tools") or {}).get(tool)
    if not isinstance(policy, dict):
        return WorkflowGuardedToolPolicyDecision(
            tool=tool,
            allowed=False,
            auto_confirmed=False,
            on_denied="fail",
            reason="missing guarded tool workflow policy",
        )

    on_denied = policy.get("on_denied") if policy.get("on_denied") in {"fail", "pause"} else "fail"
    if policy.get("auto_confirm") is not True:
        return WorkflowGuardedToolPolicyDecision(
            tool=tool,
            allowed=False,
            auto_confirmed=False,
            on_denied=on_denied,
            reason="workflow policy does not enable auto_confirm",
        )

    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return WorkflowGuardedToolPolicyDecision(tool=tool, allowed=False, auto_confirmed=False, on_denied=on_denied, reason="missing command")
    command = command.strip()

    deny_rules = policy.get("deny") or []
    if isinstance(deny_rules, list):
        for rule in deny_rules:
            matched, safe_rule = _match_rule(command, rule, variables)
            if matched:
                return WorkflowGuardedToolPolicyDecision(
                    tool=tool,
                    allowed=False,
                    auto_confirmed=False,
                    on_denied=on_denied,
                    reason="deny rule matched",
                    matched_deny_rule=safe_rule,
                )

    allow_rules = policy.get("allow") or []
    if not isinstance(allow_rules, list) or not allow_rules:
        return WorkflowGuardedToolPolicyDecision(
            tool=tool,
            allowed=False,
            auto_confirmed=False,
            on_denied=on_denied,
            reason="no allow rules configured",
        )

    matched_allow = None
    for rule in allow_rules:
        matched, safe_rule = _match_rule(command, rule, variables)
        if matched:
            matched_allow = safe_rule
            break

    if matched_allow is None:
        return WorkflowGuardedToolPolicyDecision(
            tool=tool,
            allowed=False,
            auto_confirmed=False,
            on_denied=on_denied,
            reason="no allow rule matched",
        )

    allowed_working_dirs = policy.get("allowed_working_dirs") or []
    if allowed_working_dirs and not _matches_allowed_working_dir(args.get("working_dir") or args.get("cwd"), allowed_working_dirs, variables):
        return WorkflowGuardedToolPolicyDecision(
            tool=tool,
            allowed=False,
            auto_confirmed=False,
            on_denied=on_denied,
            reason="working_dir is not allowed by workflow policy",
            matched_allow_rule=matched_allow,
        )

    max_timeout = policy.get("max_timeout_seconds")
    if max_timeout is not None:
        try:
            max_timeout_value = float(max_timeout)
            timeout_value = float(args.get("timeout") if args.get("timeout") is not None else 60.0)
        except (TypeError, ValueError):
            return WorkflowGuardedToolPolicyDecision(
                tool=tool,
                allowed=False,
                auto_confirmed=False,
                on_denied=on_denied,
                reason="timeout is not numeric",
                matched_allow_rule=matched_allow,
            )
        if timeout_value > max_timeout_value:
            return WorkflowGuardedToolPolicyDecision(
                tool=tool,
                allowed=False,
                auto_confirmed=False,
                on_denied=on_denied,
                reason="timeout exceeds workflow policy maximum",
                matched_allow_rule=matched_allow,
            )

    return WorkflowGuardedToolPolicyDecision(
        tool=tool,
        allowed=True,
        auto_confirmed=True,
        on_denied=on_denied,
        reason="allowed by workflow policy",
        matched_allow_rule=matched_allow,
    )
