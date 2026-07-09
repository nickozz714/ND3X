from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict, List, Set
from urllib.parse import urlparse

import httpx

from component.config import settings
from component.logging import get_logger
from repository.workflow_repository import WorkflowRepository
from repository.workflow_run_repository import WorkflowRunRepository
from services.assistants.orchestration.formatting import _preview
from services.assistants.orchestration.guarded_tools import tool_call_hash
from services.mail.notifications import send_system_notification
from services.workflows.assistant_operation_runner import AssistantOperationRunner

logger = logging.getLogger(__name__)
log = get_logger(__name__)

class WorkflowCancelled(Exception):
    pass


class WorkflowInputMappingError(Exception):
    def __init__(self, message: str, *, input_payload: Dict[str, Any], output_payload: Dict[str, Any], trace: list):
        super().__init__(message)
        self.input_payload = input_payload
        self.output_payload = output_payload
        self.trace = trace


class WorkflowOperationOutputError(Exception):
    def __init__(self, message: str, *, output_payload: Dict[str, Any], trace: list):
        super().__init__(message)
        self.output_payload = output_payload
        self.trace = trace


class WorkflowExecutor:
    def __init__(
        self,
        *,
        workflow_repository: WorkflowRepository,
        run_repository: WorkflowRunRepository,
        assistant_runner: AssistantOperationRunner,
        prompt_variable_resolver=None,
        claude_code_runner=None,
    ):
        log.infox(
            "WorkflowExecutor initialiseren",
            has_workflow_repository=workflow_repository is not None,
            has_run_repository=run_repository is not None,
            has_assistant_runner=assistant_runner is not None,
            has_prompt_variable_resolver=prompt_variable_resolver is not None,
            workflow_repository_type=type(workflow_repository).__name__,
            run_repository_type=type(run_repository).__name__,
            assistant_runner_type=type(assistant_runner).__name__,
        )
        self.workflow_repository = workflow_repository
        self.run_repository = run_repository
        self.assistant_runner = assistant_runner
        self.prompt_variable_resolver = prompt_variable_resolver
        # Optional alternative engine: run an assistant activity as an autonomous
        # Claude Code CLI task (config.execution.engine == "claude_code").
        self.claude_code_runner = claude_code_runner
        log.infox("WorkflowExecutor geïnitialiseerd")


    def _append_trace_event(self, trace: list, event_type: str, data: Dict[str, Any], *, level: str = "info") -> None:
        trace.append({
            "type": event_type,
            "level": level,
            "summary": event_type.replace("_", " "),
            "data": data,
        })

    def _utc_iso(self, value: datetime | None = None) -> str:
        return (value or datetime.utcnow()).replace(microsecond=0).isoformat() + "Z"

    def _compact_preview(self, value: Any, limit: int = 300) -> str:
        text = "" if value is None else str(value)
        text = text.replace("\r", " ").replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def _compact_json_preview(self, value: Any, limit: int = 1000) -> Any:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            return self._compact_preview(value, limit)
        if len(text) <= limit:
            return value
        return {"preview": self._compact_preview(text, limit), "truncated": True}

    def _waiting_policy_summary(self, operation) -> Dict[str, Any] | None:
        config = operation.config if isinstance(getattr(operation, "config", None), dict) else {}
        raw = config.get("waiting_policy")
        if not raw:
            return None
        if not isinstance(raw, dict):
            return {"on_timeout": "fail", "invalid": True, "error": "waiting_policy must be an object"}
        on_timeout = str(raw.get("on_timeout") or "fail").strip().lower()
        if on_timeout not in {"fail", "cancel", "keep_waiting"}:
            on_timeout = "fail"
        summary: Dict[str, Any] = {"on_timeout": on_timeout}
        try:
            timeout_minutes = float(raw.get("timeout_minutes"))
            if timeout_minutes <= 0:
                raise ValueError("timeout_minutes must be positive")
            summary["timeout_minutes"] = timeout_minutes
        except Exception:
            if "timeout_minutes" in raw:
                summary["invalid"] = True
                summary["error"] = "timeout_minutes must be a positive number"
        return summary

    def _prepare_waiting_pending_state(self, operation, pending_state: Dict[str, Any], trace: list) -> Dict[str, Any]:
        pending_state = dict(pending_state or {})
        now = datetime.utcnow()
        pending_state.setdefault("created_at", self._utc_iso(now))
        policy = self._waiting_policy_summary(operation)
        if policy:
            pending_state["waiting_policy"] = policy
            timeout_minutes = policy.get("timeout_minutes")
            if timeout_minutes:
                expires_at = now + timedelta(minutes=float(timeout_minutes))
                pending_state["expires_at"] = self._utc_iso(expires_at)
                self._append_trace_event(trace, "workflow_waiting_timeout_set", {
                    "operation_id": getattr(operation, "id", None),
                    "expires_at": pending_state["expires_at"],
                    "on_timeout": policy.get("on_timeout"),
                })
        return pending_state

    def _resume_history_item(
        self,
        *,
        pending_state: Dict[str, Any],
        resume: Dict[str, Any],
        run_id: int,
        operation_id: int,
        resume_by: Any = None,
    ) -> Dict[str, Any]:
        actor = None
        if isinstance(resume_by, dict):
            actor = resume_by.get("email") or resume_by.get("id")
        elif resume_by is not None:
            actor = str(resume_by)
        resume_type = (resume or {}).get("type")
        base: Dict[str, Any] = {
            "type": resume_type,
            "at": self._utc_iso(),
            "by": actor,
            "operation_id": operation_id,
            "run_id": run_id,
        }
        if resume_type == "user_input":
            base["answer_preview"] = self._compact_preview((resume or {}).get("answer"), 300)
            base["status"] = "received"
            return base
        if resume_type == "approval":
            display = pending_state.get("display") if isinstance(pending_state.get("display"), dict) else {}
            base.update({
                "approved": bool((resume or {}).get("approved")),
                "tool": pending_state.get("tool"),
                "command_preview": self._compact_preview(display.get("command"), 300),
                "tool_call_hash": pending_state.get("tool_call_hash"),
                "status": "approved" if (resume or {}).get("approved") else "rejected",
            })
            if not (resume or {}).get("approved"):
                base["reason"] = self._compact_preview((resume or {}).get("reason") or "Rejected by user", 300)
            return base
        base["status"] = "received"
        return base

    def _path_get(self, value: Any, path: str | None) -> tuple[bool, Any]:
        if path in (None, ""):
            return True, value
        cur = value
        for part in str(path).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                return False, None
        return True, cur

    def _operation_id_by_key(self, key: Any, context: Dict[str, Any]) -> int | None:
        if key is None:
            return None
        for op in context.get("operations") or []:
            if str(getattr(op, "name", "")) == str(key) or str(getattr(op, "id", "")) == str(key):
                return getattr(op, "id", None)
        return None

    def _operation_output_value(self, out: Any) -> Any:
        """The canonical 'value' of an operation's output, regardless of operation type — so
        a downstream step can just take ${operation.X.output} (e.g. an agent's markdown
        report, a tool's content) without knowing the internal shape. Foolproof default."""
        if not isinstance(out, dict):
            return out
        # Assistant answer (e.g. the generated markdown report).
        if out.get("answer") not in (None, ""):
            return out["answer"]
        # Tool result → unwrap common content keys, else the raw result object.
        if "result" in out:
            res = out["result"]
            if isinstance(res, dict):
                for k in ("content_text", "content", "text", "markdown", "value"):
                    if res.get(k) not in (None, ""):
                        return res[k]
            return res
        dh = out.get("downstream_handoff")
        if isinstance(dh, dict):
            if dh.get("full_answer"):
                return dh["full_answer"]
            if dh.get("summary"):
                return dh["summary"]
        if out.get("mode") == "set_variable":
            return out.get("variables_set")
        return out

    def _resolve_output_path(self, op_output: Any, path: str) -> tuple[bool, Any]:
        """Resolve a path into an operation output, with a canonical `output` / `output.value`
        accessor that maps to the operation's primary value (see _operation_output_value).
        Raw paths (e.g. `answer`, `downstream_handoff.facts`) still work unchanged."""
        p = path or ""
        if p in ("output", "output.value"):
            return True, self._operation_output_value(op_output)
        if p.startswith("output.value."):
            return self._path_get(self._operation_output_value(op_output), p[len("output.value."):])
        if p.startswith("output."):
            return self._path_get(self._operation_output_value(op_output), p[len("output."):])
        return self._path_get(op_output, p)

    def _resolve_reference(self, expression: str, context: Dict[str, Any], *, previous_operation_id: int | None = None) -> tuple[bool, Any]:
        expr = str(expression or "").strip()
        # Secret placeholders survive template resolution untouched: they are only
        # resolved (decrypted) at the outbound boundary by _inject_secrets, so an
        # http_request injects the real value while set_variable / assistant inputs
        # keep the inert ${secret.NAME} literal and the AI never sees the value.
        if expr.startswith("secret.") or expr.startswith("secrets."):
            return True, "${" + expr + "}"
        if expr.startswith("workflow_input"):
            path = expr[len("workflow_input"):].lstrip(".")
            return self._path_get(context.get("input") or {}, path)
        if expr.startswith("workflow_variables"):
            path = expr[len("workflow_variables"):].lstrip(".")
            return self._path_get(context.get("workflow_variables") or {}, path)
        if expr.startswith("previous_operation_output"):
            path = expr[len("previous_operation_output"):].lstrip(".")
            outputs = context.get("operation_outputs") or {}
            prev = outputs.get(previous_operation_id) if previous_operation_id is not None else None
            if prev is None and outputs:
                prev = outputs[sorted(outputs.keys())[-1]]
            return self._resolve_output_path(prev, path)
        if expr.startswith("operation."):
            rest = expr[len("operation."):]
            key, _, path = rest.partition(".")
            op_id = int(key) if key.isdigit() else self._operation_id_by_key(key, context)
            if op_id is None:
                return False, None
            return self._resolve_output_path((context.get("operation_outputs") or {}).get(op_id), path)
        if expr.startswith("operation_output."):
            rest = expr[len("operation_output."):]
            key, _, path = rest.partition(".")
            op_id = int(key) if key.isdigit() else self._operation_id_by_key(key, context)
            if op_id is None:
                return False, None
            return self._resolve_output_path((context.get("operation_outputs") or {}).get(op_id), path)
        if expr.startswith("run_result"):
            path = expr[len("run_result"):].lstrip(".")
            return self._path_get(context.get("run_result") or {}, path)
        return False, None

    def _resolve_template_value(self, value: Any, context: Dict[str, Any], *, previous_operation_id: int | None = None, allow_null: bool = False) -> Any:
        if isinstance(value, dict):
            return {k: self._resolve_template_value(v, context, previous_operation_id=previous_operation_id, allow_null=allow_null) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_template_value(v, context, previous_operation_id=previous_operation_id, allow_null=allow_null) for v in value]
        if not isinstance(value, str):
            return value
        matches = list(re.finditer(r"\$\{([^}]+)\}", value))
        if not matches:
            return value
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            found, resolved = self._resolve_reference(matches[0].group(1), context, previous_operation_id=previous_operation_id)
            if not found and not allow_null:
                raise ValueError(f"Unable to resolve workflow variable expression: {matches[0].group(1)}")
            return resolved if found else None
        result = value
        for match in matches:
            found, resolved = self._resolve_reference(match.group(1), context, previous_operation_id=previous_operation_id)
            if not found and not allow_null:
                raise ValueError(f"Unable to resolve workflow template expression: {match.group(1)}")
            result = result.replace(match.group(0), "" if resolved is None else str(resolved))
        return result

    def _condition_source_value(self, condition: Dict[str, Any], operation: Any, context: Dict[str, Any]) -> tuple[bool, Any]:
        source = condition.get("source") or "workflow_input"
        path = condition.get("path")
        if source == "workflow_input":
            return self._path_get(context.get("input") or {}, path)
        if source == "workflow_variables":
            return self._path_get(context.get("workflow_variables") or {}, path)
        if source == "operation_output":
            op_id = condition.get("operation_id") or self._operation_id_by_key(condition.get("operation_key"), context)
            return self._path_get((context.get("operation_outputs") or {}).get(int(op_id)) if op_id is not None else None, path)
        if source == "previous_operation_output":
            deps = list(getattr(operation, "depends_on", None) or [])
            op_id = int(deps[-1]) if deps else None
            outputs = context.get("operation_outputs") or {}
            value = outputs.get(op_id) if op_id is not None else (outputs.get(sorted(outputs.keys())[-1]) if outputs else None)
            return self._path_get(value, path)
        if source == "run_result":
            return self._path_get(context.get("run_result") or {}, path)
        raise ValueError(f"Unsupported condition source: {source}")

    def _evaluate_condition_operator(self, found: bool, left: Any, operator: str, right: Any) -> bool:
        op = (operator or "equals").strip().lower()
        if op == "exists":
            return found
        if op == "not_exists":
            return not found
        if not found:
            return False
        if op == "equals":
            return left == right
        if op == "not_equals":
            return left != right
        if op == "contains":
            return right in left if isinstance(left, (list, tuple, set, str, dict)) else False
        if op == "not_contains":
            return not (right in left) if isinstance(left, (list, tuple, set, str, dict)) else True
        if op in {"greater_than", "greater_than_or_equal", "less_than", "less_than_or_equal"}:
            try:
                a, b = float(left), float(right)
            except Exception:
                return False
            return {"greater_than": a > b, "greater_than_or_equal": a >= b, "less_than": a < b, "less_than_or_equal": a <= b}[op]
        if op == "in":
            return left in right if isinstance(right, (list, tuple, set, str, dict)) else False
        if op == "not_in":
            return not (left in right) if isinstance(right, (list, tuple, set, str, dict)) else True
        if op == "is_truthy":
            return bool(left)
        if op == "is_falsey":
            return not bool(left)
        raise ValueError(f"Unsupported condition operator: {operator}")

    def _redact_headers(self, headers: Dict[str, Any] | None) -> Dict[str, Any]:
        sensitive = {"authorization", "cookie", "set-cookie", "x-api-key"}
        return {str(k): ("[redacted]" if str(k).lower() in sensitive else v) for k, v in (headers or {}).items()}

    def _inject_secrets(self, value: Any, collected: list[str]) -> Any:
        """Resolve ${secret.NAME} placeholders to decrypted values just before an
        outbound call. Resolved plaintexts are appended to ``collected`` so the
        caller can mask them out of any trace/output the AI could read."""
        from services.secret_service import SecretService
        if isinstance(value, dict):
            return {k: self._inject_secrets(v, collected) for k, v in value.items()}
        if isinstance(value, list):
            return [self._inject_secrets(v, collected) for v in value]
        if not isinstance(value, str) or not SecretService.has_placeholder(value):
            return value
        resolved, values, unresolved = SecretService(self.workflow_repository.db).resolve_placeholders(value)
        if unresolved:
            raise ValueError(f"Unknown secret(s): {', '.join(sorted(set(unresolved)))}")
        collected.extend(values)
        return resolved

    def _mask_secrets(self, text: Any, secret_values: list[str]) -> str:
        out = str(text)
        for sv in secret_values:
            if sv:
                out = out.replace(sv, "[secret]")
        return out

    def _validate_http_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("http_request requires an http(s) URL")
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "0.0.0.0"} or host.endswith(".local"):
            raise ValueError("http_request does not allow localhost/internal URLs")
        try:
            ip = ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("http_request does not allow localhost/internal URLs")
        except ValueError as exc:
            if "does not allow" in str(exc):
                raise

    def _operation_retry_policy(self, operation: Any) -> Dict[str, Any]:
        config = operation.config if isinstance(getattr(operation, "config", None), dict) else {}
        policy = getattr(operation, "retry_policy", None) or config.get("retry_policy") or {}
        return policy if isinstance(policy, dict) else {}

    def _retry_allowed_for_error(self, policy: Dict[str, Any], error: Exception) -> bool:
        if not policy:
            return False
        message = str(error)
        tokens = [str(x).lower() for x in (policy.get("retry_on_error_contains") or [])]
        if tokens and not any(token in message.lower() for token in tokens):
            return False
        return True

    def _resolve_input_mapping_source(self, spec: Dict[str, Any], operation: Any, context: Dict[str, Any]) -> tuple[bool, Any]:
        source = str(spec.get("source") or "static").strip().lower()
        if source == "static":
            return True, spec.get("value")
        path = spec.get("path")
        if source == "workflow_input":
            return self._path_get(context.get("input") or {}, path)
        if source == "workflow_variables":
            return self._path_get(context.get("workflow_variables") or {}, path)
        if source == "operation_output":
            op_id = spec.get("operation_id") or self._operation_id_by_key(spec.get("operation_key"), context)
            if op_id is None:
                return False, None
            return self._path_get((context.get("operation_outputs") or {}).get(int(op_id)), path)
        if source == "previous_operation_output":
            deps = list(getattr(operation, "depends_on", None) or [])
            outputs = context.get("operation_outputs") or {}
            previous_id = int(deps[-1]) if deps else (sorted(outputs.keys())[-1] if outputs else None)
            return self._path_get(outputs.get(previous_id) if previous_id is not None else None, path)
        if source == "for_each_item":
            workflow_input = context.get("input") or {}
            item = workflow_input.get("for_each_item") if isinstance(workflow_input, dict) and "for_each_item" in workflow_input else workflow_input
            return self._path_get(item, path)
        raise ValueError(f"Unsupported input_mapping source: {source}")

    def _resolve_input_mapping(self, operation: Any, context: Dict[str, Any]) -> tuple[Dict[str, Any], list, list, list]:
        config = operation.config or {}
        mapping = config.get("input_mapping")
        if not mapping:
            return {}, [], [], []
        if not isinstance(mapping, dict):
            raise ValueError("operation config.input_mapping must be an object")
        mapped_inputs: Dict[str, Any] = {}
        missing: list[str] = []
        errors: list[Dict[str, Any]] = []
        summaries: list[Dict[str, Any]] = []
        for key, raw_spec in mapping.items():
            spec = raw_spec if isinstance(raw_spec, dict) else {"source": "static", "value": raw_spec}
            source = str(spec.get("source") or "static")
            path = spec.get("path")
            required = bool(spec.get("required", False))
            summary = {"key": key, "source": source, "path": path, "operation_id": spec.get("operation_id"), "operation_key": spec.get("operation_key")}
            summaries.append(summary)
            try:
                found, value = self._resolve_input_mapping_source(spec, operation, context)
            except Exception as exc:
                found, value = False, None
                errors.append({**summary, "error": self._compact_preview(exc, 300)})
            if not found:
                if required:
                    missing.append(str(key))
                    if not any(e.get("key") == key for e in errors):
                        errors.append({**summary, "error": "required input not found"})
                    continue
                mapped_inputs[str(key)] = spec.get("default") if "default" in spec else None
                continue
            mapped_inputs[str(key)] = value
        return mapped_inputs, missing, errors, summaries

    def _validate_output_contract(self, operation: Any, output: Dict[str, Any]) -> Dict[str, Any] | None:
        config = operation.config or {}
        contract = config.get("output_contract") if isinstance(config.get("output_contract"), dict) else None
        if not contract:
            return None
        missing = []
        for path in contract.get("required_paths") or []:
            found, _ = self._path_get(output, path)
            if not found:
                missing.append(path)
        status_path = contract.get("status_path")
        status_value = None
        bad_status = False
        if status_path:
            found, status_value = self._path_get(output, status_path)
            if found and "success_values" in contract:
                bad_status = status_value not in (contract.get("success_values") or [])
        valid = not missing and not bad_status
        validation = {"valid": valid, "missing_paths": missing, "status_path": status_path, "status_value": status_value}
        output["contract_validation"] = validation
        trace = output.setdefault("trace", [])
        self._append_trace_event(trace, "workflow_output_contract_validated", {"operation_id": operation.id, "valid": valid, "missing_paths": missing})
        if not valid and contract.get("fail_on_contract_violation", True):
            self._append_trace_event(trace, "workflow_output_contract_failed", {"operation_id": operation.id, "missing_paths": missing, "status_value": status_value}, level="error")
            raise ValueError(f"output_contract_violation: missing_paths={missing}, status_value={status_value}")
        return validation

    def _apply_agent_loop_state(self, payload: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        state = state or {}
        payload = dict(payload or {})
        if state.get("agent_loop_started"):
            payload["_agent_loop_started"] = True
        if state.get("agent_loop_started_at") is not None:
            payload["_agent_loop_started_at"] = state.get("agent_loop_started_at")
        payload["_agent_loop_iterations"] = int(state.get("iteration_count") or 0)
        payload["_agent_loop_tool_calls"] = int(state.get("tool_call_count") or 0)
        payload["_agent_loop_error_repeats"] = state.get("error_repeats") or {}
        payload["_last_tool_calls"] = state.get("last_tool_calls") or []
        payload["_last_tool_results"] = state.get("last_tool_results") or []
        payload["_last_docs"] = state.get("last_docs") or []
        payload["_acc_tool_calls"] = state.get("acc_tool_calls") or []
        payload["_acc_tool_results"] = state.get("acc_tool_results") or []
        payload["_acc_docs"] = state.get("acc_docs") or []
        if state.get("remaining_eval_hops") is not None:
            payload["_remaining_eval_hops"] = state.get("remaining_eval_hops")
        if state.get("remaining_tool_budget") is not None:
            payload["_remaining_tool_budget"] = state.get("remaining_tool_budget")
        payload["_text_search_used"] = bool(state.get("text_search_used", False))
        return payload

    def _context_from_operation_runs(self, run, workflow, *, extra_statuses: Dict[int, str] | None = None, extra_outputs: Dict[int, Any] | None = None) -> Dict[str, Any]:
        statuses: Dict[int, str] = {}
        outputs: Dict[int, Any] = {}
        workflow_variables = dict(
            ((run.result_payload or {}) if isinstance(getattr(run, "result_payload", None), dict) else {}).get("workflow_variables") or {}
        )
        reconstruction_trace: list = []
        run_with_ops = self.run_repository.get_run_with_operations(run.id)
        operation_runs = list(getattr(run_with_ops, "operation_runs", []) or [])
        operation_runs.sort(key=lambda item: (getattr(item, "id", 0) or 0))
        for op_run in operation_runs:
            status = getattr(op_run, "status", None)
            op_id = getattr(op_run, "workflow_operation_id", None)
            output_payload = op_run.output_payload or {}
            if status == "success":
                statuses[op_id] = "success"
                outputs[op_id] = output_payload
                if isinstance(output_payload, dict) and output_payload.get("mode") == "set_variable" and isinstance(output_payload.get("variables_set"), dict):
                    workflow_variables.update(output_payload.get("variables_set") or {})
            elif status == "failed":
                statuses[op_id] = "failed"
                outputs[op_id] = {"error": op_run.error}
        if workflow_variables:
            self._append_trace_event(reconstruction_trace, "workflow_variables_reconstructed", {
                "workflow_run_id": run.id,
                "variable_names": sorted(workflow_variables.keys()),
            })
        statuses.update(extra_statuses or {})
        outputs.update(extra_outputs or {})
        return {
            "workflow_id": workflow.id,
            "workflow_run_id": run.id,
            "input": run.input_payload or {},
            "operation_outputs": outputs,
            "operation_statuses": statuses,
            "workflow_variables": workflow_variables,
            "workflow_reconstruction_trace": reconstruction_trace,
            "operations": workflow.operations or [],
        }

    async def resume_waiting_operation(self, *, run_id: int, operation_id: int, resume: Dict[str, Any], resume_by: Any = None) -> Dict[str, Any]:
        run = self.run_repository.get_run(run_id)
        if not run:
            raise ValueError("Workflow run not found")
        if run.status in {"success", "failed", "cancelled", "cancel_requested"}:
            raise RuntimeError(f"Workflow run cannot be resumed from status={run.status}")

        op_run = self.run_repository.get_waiting_operation_run(run_id=run_id, operation_id=operation_id)
        if not op_run:
            raise RuntimeError("No pending workflow operation found for resume")

        pending_state = ((op_run.progress_payload or {}).get("pending_state") or {})
        pending_type = pending_state.get("type")
        resume_type = (resume or {}).get("type")
        workflow = self.workflow_repository.get_with_operations(run.workflow_id)
        if not workflow:
            raise ValueError("Workflow not found")
        operation = next((op for op in (workflow.operations or []) if op.id == operation_id), None)
        if not operation:
            raise ValueError("Workflow operation not found")

        if pending_type == "workflow_user_input":
            if resume_type != "user_input":
                raise RuntimeError("Pending operation expects user_input resume")
            answer = (resume or {}).get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("Resume answer is required")
        elif pending_type == "workflow_tool_approval":
            if resume_type != "approval":
                raise RuntimeError("Pending operation expects approval resume")
            if "approved" not in (resume or {}) or not isinstance(resume.get("approved"), bool):
                raise ValueError("Approval resume requires approved boolean")
            if resume.get("approved") is False:
                config = operation.config if isinstance(getattr(operation, "config", None), dict) else {}
                rejection_config = config.get("on_approval_rejected") if isinstance(config.get("on_approval_rejected"), dict) else {}
                rejection_mode = str(rejection_config.get("mode") or "fail").strip().lower() if rejection_config else "fail"
                if rejection_mode != "fail":
                    raise RuntimeError(f"unsupported rejection handling mode: {rejection_mode}")
        else:
            raise RuntimeError(f"Unsupported pending workflow operation type: {pending_type}")

        self.run_repository.mark_running(run_id)
        self.run_repository.mark_operation_running(op_run.id)
        trace = list(op_run.trace or [])
        self._append_trace_event(trace, "workflow_operation_resumed", {"operation_id": operation_id, "resume_type": resume_type})
        history_item = self._resume_history_item(
            pending_state=pending_state,
            resume=resume or {},
            run_id=run_id,
            operation_id=operation_id,
            resume_by=resume_by,
        )
        self.run_repository.append_operation_resume_history(op_run.id, history_item)
        self._append_trace_event(trace, "workflow_resume_history_appended", {
            "operation_id": operation_id,
            "type": history_item.get("type"),
            "status": history_item.get("status"),
        })

        if pending_type == "workflow_user_input":
            answer = (resume or {}).get("answer")
            self._append_trace_event(trace, "workflow_user_input_received", {"operation_id": operation_id, "answer_preview": self._compact_preview(answer, 300)})
            result = await self._resume_user_input(operation, pending_state, answer, run_id, trace)
        elif pending_type == "workflow_tool_approval":
            self._append_trace_event(trace, "workflow_approval_received", {"operation_id": operation_id, "approved": resume.get("approved")})
            if not resume.get("approved"):
                reason = self._compact_preview((resume or {}).get("reason") or "Rejected by user", 300)
                display = pending_state.get("display") if isinstance(pending_state.get("display"), dict) else {}
                rejection_mode = "fail"
                config = operation.config if isinstance(getattr(operation, "config", None), dict) else {}
                rejection_config = config.get("on_approval_rejected") if isinstance(config.get("on_approval_rejected"), dict) else {}
                if rejection_config:
                    rejection_mode = str(rejection_config.get("mode") or "fail").strip().lower()
                if rejection_mode != "fail":
                    raise RuntimeError(f"unsupported rejection handling mode: {rejection_mode}")
                rejection_output = {
                    "status": "rejected",
                    "type": "workflow_tool_approval_rejected",
                    "reason": reason,
                    "tool": pending_state.get("tool"),
                    "command_preview": self._compact_preview(display.get("command"), 300),
                    "tool_call_hash": pending_state.get("tool_call_hash"),
                }
                error = f"Workflow approval rejected: {reason}"
                self._append_trace_event(trace, "workflow_approval_rejected", {
                    "operation_id": operation_id,
                    "tool": pending_state.get("tool"),
                    "command_preview": rejection_output.get("command_preview"),
                    "reason": reason,
                })
                self._append_trace_event(trace, "workflow_rejection_handled", {
                    "operation_id": operation_id,
                    "mode": rejection_mode,
                    "status": "failed",
                })
                self.run_repository.fail_operation_run(op_run.id, error=error, output_payload=rejection_output, trace=trace)
                self.run_repository.mark_failed(run_id, error=error, result_payload=rejection_output)
                return {"status": "failed", "error": error, "output": rejection_output}
            result = await self._resume_tool_approval(operation, pending_state, run_id, trace)
        else:
            raise RuntimeError(f"Unsupported pending workflow operation type: {pending_type}")

        if result.get("mode") == "workflow_waiting":
            pending = result.get("pending_state") or result.get("pending_action") or {}
            pending = self._prepare_waiting_pending_state(operation, pending, result.setdefault("trace", []))
            self.run_repository.mark_waiting_operation_run(
                op_run.id,
                status=result.get("status") or "waiting_for_user_input",
                pending_state=pending,
                trace=result.get("trace"),
                output_payload=result,
            )
            waiting_context = self._context_from_operation_runs(run, workflow, extra_statuses={operation_id: result.get("status") or "waiting_for_user_input"}, extra_outputs={operation_id: result})
            waiting_result = {
                "status": "waiting",
                "workflow_run_id": run_id,
                "waiting_operation_id": operation_id,
                "pending_state": pending,
                "workflow_variables": waiting_context.get("workflow_variables") or {},
            }
            self.run_repository.mark_waiting(run_id, result_payload=waiting_result)
            self._append_trace_event(result.setdefault("trace", []), "workflow_operation_resume_completed", {"status": "waiting"})
            return waiting_result

        if result.get("mode") in {"error", "confirm_action", "ask_user"}:
            self.run_repository.fail_operation_run(op_run.id, error=result.get("answer") or f"Assistant operation stopped with mode={result.get('mode')}")
            self.run_repository.mark_failed(run_id, error=result.get("answer") or "Workflow operation resume failed")
            self._append_trace_event(trace, "workflow_operation_resume_failed", {"mode": result.get("mode")}, level="error")
            return {"status": "failed", "error": result.get("answer")}

        output = {
            "mode": result.get("mode"),
            "answer": result.get("answer"),
            "selected_skill_names": pending_state.get("skill_names") or [],
            "downstream_handoff": result.get("downstream_handoff"),
            "tool_calls": result.get("tool_calls"),
            "tool_results": result.get("tool_results"),
            "docs": result.get("docs"),
            "trace": result.get("trace"),
            "terminal_state": result.get("terminal_state"),
        }
        self.run_repository.finish_operation_run(op_run.id, output_payload=output, trace=result.get("trace"))

        context = self._context_from_operation_runs(run, workflow, extra_statuses={operation_id: "success"}, extra_outputs={operation_id: output})
        remaining = await self._execute_operations(workflow.operations or [], context)
        if isinstance(remaining, dict) and remaining.get("status") == "waiting":
            self.run_repository.mark_waiting(run_id, result_payload=remaining)
            return remaining
        self.run_repository.mark_finished(run_id, result_payload=remaining)
        return remaining

    async def _resume_user_input(self, operation, pending_state: Dict[str, Any], answer: str, run_id: int, trace: list) -> Dict[str, Any]:
        resume_payload = pending_state.get("resume_payload") or {}
        payload = self._apply_agent_loop_state(dict(resume_payload.get("payload") or {}), resume_payload.get("agent_loop_state") or {})
        payload["_workflow_background"] = True
        payload["_workflow_user_answer"] = answer
        payload["_used_evaluate"] = True
        payload["_cancellation_check"] = lambda: self._raise_if_cancel_requested(run_id)
        question = (resume_payload.get("question") or "Continue workflow operation.") + f"\n\nUser input provided for the workflow operation: {answer}"
        return await self.assistant_runner.run(
            assistant_id=operation.operation_ref_id,
            question=question,
            payload=payload,
            workflow_run_id=run_id,
            operation_id=operation.id,
            model=resume_payload.get("model"),
            turn_id=operation.id,
        )

    async def _resume_tool_approval(self, operation, pending_state: Dict[str, Any], run_id: int, trace: list) -> Dict[str, Any]:
        tool_call = pending_state.get("tool_call")
        expected_hash = pending_state.get("tool_call_hash")
        if not isinstance(tool_call, dict) or not expected_hash:
            raise ValueError("Pending approval is missing tool call hash")
        actual_hash = tool_call_hash(tool_call)
        if actual_hash != expected_hash:
            self._append_trace_event(trace, "workflow_approval_hash_mismatch", {"operation_id": operation.id}, level="error")
            raise RuntimeError("Pending approval hash mismatch")
        self._append_trace_event(trace, "workflow_approval_hash_verified", {"operation_id": operation.id, "tool_call_hash": expected_hash})
        runner = self.assistant_runner.pipeline_runner.tool_runner
        trace_fn = self.assistant_runner.pipeline_runner.trace_fn
        tool_results = await runner.execute_tool_calls(
            tool_calls=[tool_call],
            session_id=f"workflow:{run_id}:operation:{operation.id}:resume",
            turn_id=operation.id,
            trace=trace,
            assistant_name=pending_state.get("assistant_name") or f"assistant_{operation.operation_ref_id}",
            trace_fn=trace_fn,
            preview_fn=_preview,
            confirmed_tool_call_hashes={expected_hash},
        )
        self._append_trace_event(trace, "workflow_operation_resumed_after_approval", {"operation_id": operation.id, "tool": tool_call.get("tool")})
        resume_payload = pending_state.get("resume_payload") or {}
        payload = self._apply_agent_loop_state(dict(resume_payload.get("payload") or {}), resume_payload.get("agent_loop_state") or {})
        payload["_workflow_background"] = True
        payload["_used_evaluate"] = True
        payload["_last_tool_calls"] = [tool_call]
        payload["_last_tool_results"] = tool_results
        payload["_acc_tool_calls"] = list(payload.get("_acc_tool_calls") or []) + [tool_call]
        payload["_acc_tool_results"] = list(payload.get("_acc_tool_results") or []) + tool_results
        payload["_cancellation_check"] = lambda: self._raise_if_cancel_requested(run_id)
        return await self.assistant_runner.run(
            assistant_id=operation.operation_ref_id,
            question=resume_payload.get("question") or "Continue workflow operation after approved tool execution.",
            payload=payload,
            workflow_run_id=run_id,
            operation_id=operation.id,
            model=resume_payload.get("model"),
            turn_id=operation.id,
        )

    def _raise_if_cancel_requested(self, run_id: int) -> None:
        log.debugx(
            "Workflow cancel request controleren",
            workflow_run_id=run_id,
        )
        if self.run_repository.is_cancel_requested(run_id):
            log.warningx(
                "Workflow cancel request gedetecteerd",
                workflow_run_id=run_id,
            )
            raise WorkflowCancelled(f"Workflow run cancelled: {run_id}")
        log.debugx(
            "Workflow cancel request niet aanwezig",
            workflow_run_id=run_id,
        )

    async def _run_op_body_with_cancel(self, body_factory, *, run_id: int, timeout_seconds=None):
        """Run an operation body while watching for a cancel request, so a long
        in-flight op (e.g. an LLM call) is interrupted mid-flight — not only at the
        between-operation checkpoints. The watcher polls the shared DB flag (so it
        works across worker processes too) and cancels the body task, which surfaces
        as WorkflowCancelled (handled by the caller)."""
        body_task = asyncio.ensure_future(body_factory())

        async def _watch() -> None:
            while not body_task.done():
                await asyncio.sleep(1.0)
                try:
                    if self.run_repository.is_cancel_requested(run_id):
                        body_task.cancel()
                        return
                except Exception:  # noqa: BLE001 — never let the watcher break the run
                    return

        watch_task = asyncio.ensure_future(_watch())
        try:
            if timeout_seconds:
                return await asyncio.wait_for(body_task, timeout=int(timeout_seconds))
            return await body_task
        except asyncio.CancelledError:
            # Cancelled by the watcher (a cancel was requested), not a timeout.
            raise WorkflowCancelled(f"Workflow run cancelled mid-operation: {run_id}")
        finally:
            watch_task.cancel()

    def _serializable_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        log.debugx(
            "Workflow context serializable maken gestart",
            context_keys=list((context or {}).keys()),
            workflow_id=context.get("workflow_id"),
            workflow_run_id=context.get("workflow_run_id"),
            operation_output_count=len(context.get("operation_outputs") or {}),
            operation_status_count=len(context.get("operation_statuses") or {}),
        )
        result = {
            "workflow_id": context.get("workflow_id"),
            "workflow_run_id": context.get("workflow_run_id"),
            "input": context.get("input") or {},
            "operation_outputs": context.get("operation_outputs") or {},
            "operation_statuses": context.get("operation_statuses") or {},
            "workflow_variables": context.get("workflow_variables") or {},
        }
        log.debugx(
            "Workflow context serializable maken afgerond",
            workflow_id=result.get("workflow_id"),
            workflow_run_id=result.get("workflow_run_id"),
            input_keys=list((result.get("input") or {}).keys()) if isinstance(result.get("input"), dict) else None,
            operation_output_count=len(result.get("operation_outputs") or {}),
            operation_status_count=len(result.get("operation_statuses") or {}),
        )
        return result

    def _result_summary(self, result: Any) -> str | None:
        """Best-effort human-readable result of a workflow run for the completion email:
        an explicit final answer, else the last operation's answer/summary/handoff."""
        if not isinstance(result, dict):
            return self._compact_preview(result, 1500) if result else None
        for key in ("answer", "final_answer", "full_answer", "summary"):
            v = result.get(key)
            if isinstance(v, str) and v.strip():
                return self._compact_preview(v, 1500)
        outputs = result.get("operation_outputs")
        if isinstance(outputs, dict) and outputs:
            try:
                last_key = sorted(outputs.keys(), key=lambda k: int(k))[-1]
            except Exception:
                last_key = list(outputs.keys())[-1]
            out = outputs.get(last_key)
            if isinstance(out, dict):
                for key in ("answer", "full_answer", "summary", "message"):
                    v = out.get(key)
                    if isinstance(v, str) and v.strip():
                        return self._compact_preview(v, 1500)
                dh = out.get("downstream_handoff")
                if isinstance(dh, dict) and isinstance(dh.get("summary"), str) and dh["summary"].strip():
                    return self._compact_preview(dh["summary"], 1500)
            elif isinstance(out, str) and out.strip():
                return self._compact_preview(out, 1500)
        return None

    def _send_parent_workflow_notification(
            self,
            *,
            run,
            workflow,
            status: str,
            result: dict,
            error: str | None = None,
    ) -> None:
        """
        Stuur alleen notificaties voor parent workflow runs.
        Child workflows / for_each runs worden overgeslagen.
        """

        log.infox(
            "Parent workflow notificatie check gestart",
            workflow_run_id=getattr(run, "id", None),
            workflow_id=getattr(workflow, "id", None),
            status=status,
            parent_run_id=getattr(run, "parent_run_id", None),
            result_keys=list((result or {}).keys()) if isinstance(result, dict) else None,
            has_error=bool(error),
        )

        logger.warning(
            "WORKFLOW NOTIFICATION CHECK: run_id=%s workflow_id=%s status=%s parent_run_id=%s",
            getattr(run, "id", None),
            getattr(workflow, "id", None),
            status,
            getattr(run, "parent_run_id", None),
        )

        if getattr(run, "parent_run_id", None) is not None:
            log.infox(
                "Parent workflow notificatie overgeslagen: run is child",
                workflow_run_id=getattr(run, "id", None),
                workflow_id=getattr(workflow, "id", None),
                parent_run_id=getattr(run, "parent_run_id", None),
                status=status,
            )
            logger.warning(
                "WORKFLOW NOTIFICATION SKIPPED: run_id=%s is child run, parent_run_id=%s",
                getattr(run, "id", None),
                getattr(run, "parent_run_id", None),
            )
            return

        log.infox(
            "Parent workflow notificatie wordt verstuurd",
            workflow_run_id=getattr(run, "id", None),
            workflow_id=getattr(workflow, "id", None),
            status=status,
        )

        logger.warning(
            "WORKFLOW NOTIFICATION WILL SEND: run_id=%s status=%s",
            getattr(run, "id", None),
            status,
        )

        workflow_name = getattr(workflow, "name", f"Workflow {workflow.id}")

        if status == "success":
            subject = f"Workflow succeeded: {workflow_name}"
            title = "Workflow succesvol afgerond"
            message = f"De workflow '{workflow_name}' is succesvol afgerond."
        elif status == "failed":
            subject = f"Workflow failed: {workflow_name}"
            title = "Workflow gefaald"
            message = f"De workflow '{workflow_name}' is gefaald."
        else:
            subject = f"Workflow {status}: {workflow_name}"
            title = f"Workflow {status}"
            message = f"De workflow '{workflow_name}' heeft status: {status}."

        # Include the actual run result so the completion email carries the outcome,
        # not just success/failure.
        result_summary = self._result_summary(result)
        if result_summary:
            message = f"{message}\n\nResultaat:\n{result_summary}"

        log.debugx(
            "Parent workflow notificatie inhoud opgebouwd",
            workflow_run_id=getattr(run, "id", None),
            workflow_id=getattr(workflow, "id", None),
            workflow_name=workflow_name,
            subject=subject,
            title=title,
            message_length=len(message or ""),
        )

        try:
            send_system_notification(
                db=self.run_repository.db,
                subject=subject,
                title=title,
                message=message,
                data={
                    "Workflow ID": workflow.id,
                    "Workflow naam": workflow_name,
                    "Workflow run ID": run.id,
                    "Status": status,
                    "Trigger": getattr(run, "trigger_type", None),
                    "Foutmelding": error or "-",
                },
                action_url=f"https://www.nd3x.nl/workflows/runs/{run.id}",
            )
            log.infox(
                "Parent workflow notificatie versturen afgerond",
                workflow_run_id=getattr(run, "id", None),
                workflow_id=getattr(workflow, "id", None),
                status=status,
            )
        except Exception:
            log.warningx(
                "Parent workflow notificatie versturen mislukt",
                workflow_run_id=getattr(run, "id", None),
                workflow_id=getattr(workflow, "id", None),
                status=status,
            )
            logger.exception(
                "Workflow notification mail failed for workflow_run_id=%s",
                run.id,
            )
    def _compact_workflow_previous_outputs(
            self,
            operation_outputs: Dict[Any, Any],
            operation_statuses: Dict[Any, Any],
    ) -> Dict[Any, Any]:
        log.debugx(
            "Workflow previous outputs compact maken gestart",
            operation_output_count=len(operation_outputs or {}),
            operation_status_count=len(operation_statuses or {}),
        )
        compact: Dict[Any, Any] = {}

        for op_id, output in (operation_outputs or {}).items():
            status = (operation_statuses or {}).get(op_id)

            log.debugx(
                "Workflow previous output verwerken",
                operation_id=op_id,
                status=status,
                output_type=type(output).__name__,
                output_keys=list(output.keys()) if isinstance(output, dict) else None,
            )

            if not isinstance(output, dict):
                compact[op_id] = {
                    "operation_id": op_id,
                    "status": status or "unknown",
                    "downstream_handoff": None,
                }
                log.debugx(
                    "Workflow previous output compact gemaakt zonder dict output",
                    operation_id=op_id,
                    status=status or "unknown",
                )
                continue

            handoff = output.get("downstream_handoff")
            if isinstance(handoff, dict):
                compact_handoff = {
                    "summary": handoff.get("summary"),
                    "facts": handoff.get("facts") or {},
                    "artifacts": handoff.get("artifacts") or [],
                    "iterables": handoff.get("iterables") or {},
                    "open_questions": handoff.get("open_questions") or [],
                    "output_ref": handoff.get("output_ref"),
                    "status": handoff.get("status", status or "unknown"),
                }
                log.debugx(
                    "Workflow previous output handoff compact gemaakt",
                    operation_id=op_id,
                    handoff_status=compact_handoff.get("status"),
                    artifact_count=len(compact_handoff.get("artifacts") or []),
                    iterable_keys=list((compact_handoff.get("iterables") or {}).keys()),
                    open_question_count=len(compact_handoff.get("open_questions") or []),
                )
            else:
                compact_handoff = None
                log.debugx(
                    "Workflow previous output bevat geen downstream_handoff",
                    operation_id=op_id,
                )

            compact[op_id] = {
                "operation_id": op_id,
                "status": status or output.get("status") or "unknown",
                "mode": output.get("mode"),
                "downstream_handoff": compact_handoff,
            }

        log.debugx(
            "Workflow previous outputs compact maken afgerond",
            compact_count=len(compact),
            operation_ids=list(compact.keys()),
        )
        return compact

    async def execute_run(self, run_id: int) -> Dict[str, Any]:
        log.infox(
            "Workflow run uitvoeren gestart",
            workflow_run_id=run_id,
        )
        run = self.run_repository.mark_running(run_id)
        if not run:
            log.errorx(
                "Workflow run niet gevonden bij mark_running",
                workflow_run_id=run_id,
            )
            raise ValueError(f"Workflow run not found: {run_id}")

        log.infox(
            "Workflow run gemarkeerd als running",
            workflow_run_id=getattr(run, "id", None),
            workflow_id=getattr(run, "workflow_id", None),
            trigger_type=getattr(run, "trigger_type", None),
            parent_run_id=getattr(run, "parent_run_id", None),
        )

        workflow = self.workflow_repository.get_with_operations(run.workflow_id)
        if not workflow:
            log.errorx(
                "Workflow niet gevonden voor run",
                workflow_run_id=getattr(run, "id", None),
                workflow_id=getattr(run, "workflow_id", None),
            )
            self.run_repository.mark_failed(run_id, error="Workflow not found")
            raise ValueError(f"Workflow not found: {run.workflow_id}")

        log.infox(
            "Workflow geladen voor execution",
            workflow_run_id=getattr(run, "id", None),
            workflow_id=getattr(workflow, "id", None),
            workflow_name=getattr(workflow, "name", None),
            operation_count=len(workflow.operations or []),
        )

        context: Dict[str, Any] = {
            "workflow_id": workflow.id,
            "workflow_run_id": run.id,
            "input": run.input_payload or {},
            "operation_outputs": {},
            "operation_statuses": {},
            "workflow_variables": {},
            "skipped_operation_ids": set(),
            "operations": workflow.operations or [],
        }

        log.debugx(
            "Workflow execution context aangemaakt",
            workflow_run_id=run.id,
            workflow_id=workflow.id,
            input_keys=list((run.input_payload or {}).keys()) if isinstance(run.input_payload or {}, dict) else None,
            operation_count=len(workflow.operations or []),
        )

        try:
            self._raise_if_cancel_requested(run.id)

            result = await self._execute_operations(workflow.operations or [], context)

            self._raise_if_cancel_requested(run.id)

            if isinstance(result, dict) and result.get("status") == "waiting":
                self.run_repository.mark_waiting(run.id, result_payload=result)
                log.infox(
                    "Workflow run wacht op externe input/approval",
                    workflow_run_id=run.id,
                    workflow_id=workflow.id,
                    waiting_operation_id=result.get("waiting_operation_id"),
                )
                return result

            log.infox(
                "Workflow operations afgerond, run markeren als finished",
                workflow_run_id=run.id,
                workflow_id=workflow.id,
                result_status=result.get("status") if isinstance(result, dict) else None,
                operation_status_count=len((result.get("operation_statuses") or {}) if isinstance(result, dict) else {}),
            )
            self.run_repository.mark_finished(run.id, result_payload=result)

            self._send_parent_workflow_notification(
                run=run,
                workflow=workflow,
                status="success",
                result=result,
            )

            log.infox(
                "Workflow run uitvoeren afgerond",
                workflow_run_id=run.id,
                workflow_id=workflow.id,
                status="success",
            )
            return result

        except WorkflowCancelled:
            log.warningx(
                "Workflow run geannuleerd",
                workflow_run_id=run.id,
                workflow_id=workflow.id,
            )
            # Parent/user requested cancellation. Make sure children are also asked to stop.
            self.run_repository.request_cancel_child_runs(run.id)

            result = {
                "status": "cancelled",
                **self._serializable_context(context),
            }

            self.run_repository.mark_cancelled(
                run.id,
                result_payload=result,
            )
            log.warningx(
                "Workflow run gemarkeerd als cancelled",
                workflow_run_id=run.id,
                workflow_id=workflow.id,
            )
            return result

        except asyncio.CancelledError:
            log.warningx(
                "Workflow asyncio task cancelled",
                workflow_run_id=run.id,
                workflow_id=workflow.id,
            )
            # This happens when an in-memory asyncio task is stopped because a for_each sibling failed.
            # Treat it as workflow cancellation so the DB does not remain running.
            self.run_repository.request_cancel_child_runs(run.id)

            result = {
                "status": "cancelled",
                **self._serializable_context(context),
            }

            self.run_repository.mark_cancelled(
                run.id,
                result_payload=result,
            )
            log.warningx(
                "Workflow run gemarkeerd als cancelled door asyncio.CancelledError",
                workflow_run_id=run.id,
                workflow_id=workflow.id,
            )
            raise

        except Exception as exc:
            log.exceptionx(
                "Workflow run uitvoeren mislukt",
                workflow_run_id=run.id,
                workflow_id=workflow.id,
                exception=exc,
            )
            result_payload = self._serializable_context(context)

            self.run_repository.mark_failed(
                run.id,
                error=str(exc),
                result_payload=result_payload,
            )

            log.warningx(
                "Workflow run gemarkeerd als failed",
                workflow_run_id=run.id,
                workflow_id=workflow.id,
                error=str(exc),
            )

            self._send_parent_workflow_notification(
                run=run,
                workflow=workflow,
                status="failed",
                result=result_payload,
                error=str(exc),
            )
            raise

    async def _execute_operations(self, operations: List[Any], context: Dict[str, Any]) -> Dict[str, Any]:
        log.infox(
            "Workflow operations uitvoeren gestart",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_count=len(operations or []),
        )
        op_by_id = {op.id: op for op in operations}
        completed_or_known = set((context.get("operation_statuses") or {}).keys())
        pending: Set[int] = set(op_by_id.keys()) - completed_or_known

        # Edges that are explicitly allowed to continue after a failed operation.
        # Example: (failed_op_id, failure_follow_up_op_id)
        allowed_after_failure: Set[tuple[int, int]] = set()
        self._raise_if_cancel_requested(context["workflow_run_id"])
        while pending:
            self._raise_if_cancel_requested(context["workflow_run_id"])
            ready = [
                op_by_id[op_id]
                for op_id in sorted(pending)
                if self._dependencies_satisfied(
                    op_by_id[op_id],
                    context,
                    allowed_after_failure,
                )
            ]

            log.infox(
                "Workflow operation scheduler tick",
                workflow_run_id=context.get("workflow_run_id"),
                workflow_id=context.get("workflow_id"),
                pending_operation_ids=sorted(pending),
                ready_operation_ids=[op.id for op in ready],
                allowed_after_failure=list(allowed_after_failure),
            )

            if not ready:
                # Skip conditional follow-up branches that were not taken (source
                # finished with the non-matching outcome) so they don't stall the graph.
                dead = [
                    op_by_id[op_id]
                    for op_id in sorted(pending)
                    if self._followup_branch_dead(op_by_id[op_id], context)
                ]
                if dead:
                    for op in dead:
                        context["operation_statuses"][op.id] = "skipped"
                        context["operation_outputs"][op.id] = {"mode": "skipped", "reason": "follow-up branch not taken"}
                        pending.remove(op.id)
                        log.infox(
                            "Workflow follow-up branch overgeslagen (niet genomen)",
                            workflow_run_id=context.get("workflow_run_id"),
                            operation_id=op.id,
                        )
                    continue

                log.errorx(
                    "Workflow graph vastgelopen: geen ready operations",
                    workflow_run_id=context.get("workflow_run_id"),
                    workflow_id=context.get("workflow_id"),
                    pending_operation_ids=sorted(pending),
                    operation_statuses=context.get("operation_statuses") or {},
                )
                raise RuntimeError(f"Workflow graph is stuck. Pending operation ids: {sorted(pending)}")

            log.infox(
                "Ready workflow operations parallel uitvoeren",
                workflow_run_id=context.get("workflow_run_id"),
                ready_count=len(ready),
                ready_operation_ids=[op.id for op in ready],
            )
            results = await asyncio.gather(
                *[self._execute_single_operation(op, context) for op in ready],
                return_exceptions=True,
            )
            self._raise_if_cancel_requested(context["workflow_run_id"])
            for op, result in zip(ready, results):
                pending.remove(op.id)

                if isinstance(result, Exception):
                    log.warningx(
                        "Workflow operation resultaat is Exception",
                        workflow_run_id=context.get("workflow_run_id"),
                        operation_id=op.id,
                        operation_type=getattr(op, "operation_type", None),
                        error=str(result),
                        has_failure_follow_up=bool(op.on_failure_follow_up),
                    )
                    context["operation_statuses"][op.id] = "failed"
                    context["operation_outputs"][op.id] = {"error": str(result)}

                    follow_up = op.on_failure_follow_up

                    if not follow_up:
                        log.errorx(
                            "Workflow operation failed zonder failure follow-up",
                            workflow_run_id=context.get("workflow_run_id"),
                            operation_id=op.id,
                            error=str(result),
                        )
                        raise RuntimeError(
                            f"Workflow operation failed without failure follow-up: "
                            f"operation_id={op.id}, error={result}"
                        )

                    if follow_up in pending:
                        allowed_after_failure.add((op.id, follow_up))

                        deps = set(op_by_id[follow_up].depends_on or [])
                        deps.add(op.id)
                        op_by_id[follow_up].depends_on = list(deps)

                        log.infox(
                            "Failure follow-up toegestaan en dependency toegevoegd",
                            workflow_run_id=context.get("workflow_run_id"),
                            failed_operation_id=op.id,
                            follow_up_operation_id=follow_up,
                            new_dependencies=op_by_id[follow_up].depends_on,
                        )

                else:
                    if isinstance(result, dict) and result.get("mode") == "skipped":
                        context["operation_statuses"][op.id] = "skipped"
                        context["operation_outputs"][op.id] = result
                        log.infox(
                            "Workflow operation overgeslagen",
                            workflow_run_id=context.get("workflow_run_id"),
                            operation_id=op.id,
                            reason=result.get("reason"),
                        )
                        continue

                    if isinstance(result, dict) and result.get("mode") == "workflow_waiting":
                        status = result.get("status") or "waiting"
                        context["operation_statuses"][op.id] = status
                        context["operation_outputs"][op.id] = result
                        waiting_result = {
                            "status": "waiting",
                            "workflow_run_id": context["workflow_run_id"],
                            "waiting_operation_id": op.id,
                            "pending_state": result.get("pending_state") or result.get("pending_action"),
                            "operation_statuses": context["operation_statuses"],
                            "operation_outputs": context["operation_outputs"],
                            "workflow_variables": context.get("workflow_variables") or {},
                        }
                        log.infox(
                            "Workflow operation wacht; workflow run pauzeert",
                            workflow_run_id=context.get("workflow_run_id"),
                            operation_id=op.id,
                            waiting_status=status,
                        )
                        return waiting_result

                    log.infox(
                        "Workflow operation succesvol afgerond",
                        workflow_run_id=context.get("workflow_run_id"),
                        operation_id=op.id,
                        operation_type=getattr(op, "operation_type", None),
                        result_mode=result.get("mode") if isinstance(result, dict) else None,
                        result_status=result.get("status") if isinstance(result, dict) else None,
                    )
                    context["operation_statuses"][op.id] = "success"
                    context["operation_outputs"][op.id] = result
                    if isinstance(result, dict) and result.get("mode") == "set_variable":
                        context.setdefault("workflow_variables", {}).update(result.get("variables_set") or {})
                    if isinstance(result, dict) and result.get("mode") == "condition":
                        to_skip = {int(x) for x in (result.get("skipped_operation_ids") or [])}
                        changed = True
                        while changed:
                            changed = False
                            for candidate_id in list(pending):
                                if candidate_id in to_skip:
                                    continue
                                deps = set(int(d) for d in (op_by_id[candidate_id].depends_on or []))
                                if deps and deps.issubset(to_skip):
                                    to_skip.add(candidate_id)
                                    changed = True
                        for skipped_id in sorted(to_skip):
                            if skipped_id in pending:
                                pending.remove(skipped_id)
                            context["operation_statuses"][skipped_id] = "skipped"
                            context["operation_outputs"][skipped_id] = {"status": "skipped", "reason": "condition_branch_not_selected", "condition_operation_id": op.id}

                    follow_up = op.on_success_follow_up

                    if follow_up and follow_up in pending:
                        deps = set(op_by_id[follow_up].depends_on or [])
                        deps.add(op.id)
                        op_by_id[follow_up].depends_on = list(deps)

                        log.infox(
                            "Success follow-up dependency toegevoegd",
                            workflow_run_id=context.get("workflow_run_id"),
                            operation_id=op.id,
                            follow_up_operation_id=follow_up,
                            new_dependencies=op_by_id[follow_up].depends_on,
                        )

        result = {
            "status": "success",
            "workflow_run_id": context["workflow_run_id"],
            "operation_statuses": context["operation_statuses"],
            "operation_outputs": context["operation_outputs"],
            "workflow_variables": context.get("workflow_variables") or {},
        }

        log.infox(
            "Workflow operations uitvoeren afgerond",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_statuses=context.get("operation_statuses") or {},
            operation_output_count=len(context.get("operation_outputs") or {}),
        )
        return result

    def _dependencies_satisfied(
            self,
            operation: Any,
            context: Dict[str, Any],
            allowed_after_failure: Set[tuple[int, int]],
    ) -> bool:
        statuses = context.get("operation_statuses") or {}

        log.debugx(
            "Workflow operation dependencies controleren",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            depends_on=operation.depends_on or [],
            statuses=statuses,
            allowed_after_failure=list(allowed_after_failure),
        )

        for dep in operation.depends_on or []:
            dep = int(dep)
            status = statuses.get(dep)

            if status == "success":
                log.debugx(
                    "Dependency voldaan door success",
                    workflow_run_id=context.get("workflow_run_id"),
                    operation_id=getattr(operation, "id", None),
                    dependency_id=dep,
                    status=status,
                )
                continue

            if status == "failed" and (dep, operation.id) in allowed_after_failure:
                log.debugx(
                    "Dependency voldaan door toegestane failed follow-up",
                    workflow_run_id=context.get("workflow_run_id"),
                    operation_id=getattr(operation, "id", None),
                    dependency_id=dep,
                    status=status,
                )
                continue

            log.debugx(
                "Dependency niet voldaan",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
                dependency_id=dep,
                status=status,
            )
            return False

        # Follow-up edges (on_success_follow_up / on_failure_follow_up) are implicit,
        # CONDITIONAL dependencies: a follow-up target must wait for its source op and
        # only run on the matching outcome. Without this, a target whose depends_on is
        # empty would be "ready" on the first tick and run in parallel with its source.
        followups = self._incoming_followups(operation, context)
        if followups:
            any_fired = False
            any_pending = False
            for source_id, kind in followups:
                st = statuses.get(source_id)
                if st not in ("success", "failed", "skipped"):
                    any_pending = True
                elif (kind == "success" and st == "success") or (kind == "failure" and st == "failed"):
                    any_fired = True
            if any_pending or not any_fired:
                # Either waiting for the source(s), or every source finished with the
                # non-matching outcome (dead branch — the loop skips it).
                log.debugx(
                    "Follow-up gate niet voldaan",
                    workflow_run_id=context.get("workflow_run_id"),
                    operation_id=getattr(operation, "id", None),
                    followups=followups,
                    any_pending=any_pending,
                    any_fired=any_fired,
                )
                return False

        log.debugx(
            "Alle dependencies voldaan",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
        )
        return True

    def _incoming_followups(self, operation: Any, context: Dict[str, Any]) -> List[tuple[int, str]]:
        """Edges pointing AT this op: (source_op_id, 'success'|'failure')."""
        out: List[tuple[int, str]] = []
        op_id = getattr(operation, "id", None)
        for src in context.get("operations") or []:
            if getattr(src, "on_success_follow_up", None) == op_id:
                out.append((src.id, "success"))
            if getattr(src, "on_failure_follow_up", None) == op_id:
                out.append((src.id, "failure"))
        return out

    def _followup_branch_dead(self, operation: Any, context: Dict[str, Any]) -> bool:
        """True when this op is a follow-up target, all its sources are terminal, and
        none fired its outcome — so the branch was not taken and should be skipped."""
        statuses = context.get("operation_statuses") or {}
        followups = self._incoming_followups(operation, context)
        if not followups:
            return False
        fired = False
        for source_id, kind in followups:
            st = statuses.get(source_id)
            if st not in ("success", "failed", "skipped"):
                return False  # a source is still pending — not dead, just waiting
            if (kind == "success" and st == "success") or (kind == "failure" and st == "failed"):
                fired = True
        return not fired

    def _operation_model_available(self, operation: Any) -> bool:
        """True if an assistant operation can resolve a chat model — either a
        pinned model in config that is registered+enabled, or (no pin) the chat
        capability via assigned routing slots. Non-assistant operations and any
        registry hiccup never block (return True)."""
        if getattr(operation, "operation_type", None) != "assistant":
            return True
        cfg = getattr(operation, "config", None) or {}
        pinned = (cfg.get("model") or "").strip()
        db = getattr(self.run_repository, "db", None)
        if db is None:
            return True
        try:
            if pinned:
                from services.providers.registry_service import ProviderRegistryService
                enabled = {m.model_id for m in ProviderRegistryService(db).list_models(capability="chat") if m.enabled}
                return pinned in enabled
            from services.providers.capability_router import compute_capabilities
            return bool(compute_capabilities(db).get("chat"))
        except Exception:  # noqa: BLE001 — never block execution on a registry hiccup
            return True

    async def _execute_single_operation(self, operation: Any, context: Dict[str, Any]) -> Dict[str, Any]:
        log.infox(
            "Workflow operation uitvoeren gestart",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_id=getattr(operation, "id", None),
            operation_type=getattr(operation, "operation_type", None),
            operation_ref_id=getattr(operation, "operation_ref_id", None),
            timeout_seconds=getattr(operation, "timeout_seconds", None),
        )
        self._raise_if_cancel_requested(context["workflow_run_id"])

        # Theme 4: a model-dependent activity whose model is unset/unavailable is
        # handled per its config: skip-and-continue, or fail. Run-only — never edits
        # the saved workflow definition.
        if not self._operation_model_available(operation):
            behavior = (getattr(operation, "config", None) or {}).get("on_model_unavailable", "fail")
            log.warningx(
                "Workflow operation model niet beschikbaar",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
                behavior=behavior,
            )
            if behavior == "skip":
                return {"status": "skipped", "mode": "skipped", "reason": "model_unavailable", "operation_id": getattr(operation, "id", None)}
            raise RuntimeError(
                f"Workflow operation {getattr(operation, 'id', None)} "
                f"({getattr(operation, 'name', '')}) requires a model that is not available "
                f"(no slot assigned or selected model unavailable)."
            )
        try:
            input_payload = self._build_operation_input(operation, context)
        except WorkflowInputMappingError as exc:
            op_run = self.run_repository.create_operation_run(
                workflow_run_id=context["workflow_run_id"],
                workflow_operation_id=operation.id,
                input_payload=exc.input_payload,
            )
            self.run_repository.fail_operation_run(
                op_run.id,
                error="input_mapping_failed",
                output_payload=exc.output_payload,
                trace=exc.trace,
            )
            raise

        op_run = self.run_repository.create_operation_run(
            workflow_run_id=context["workflow_run_id"],
            workflow_operation_id=operation.id,
            input_payload=input_payload,
        )

        log.infox(
            "Workflow operation run aangemaakt",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=operation.id,
            operation_run_id=getattr(op_run, "id", None),
            input_payload_keys=list(input_payload.keys()) if isinstance(input_payload, dict) else None,
        )

        try:
            async def execute_operation_body():
                log.debugx(
                    "Workflow operation body dispatch",
                    workflow_run_id=context.get("workflow_run_id"),
                    operation_id=operation.id,
                    operation_type=operation.operation_type,
                )
                if operation.operation_type == "assistant":
                    return await self._execute_assistant_operation(operation, input_payload, context)

                if operation.operation_type == "sub_workflow":
                    return await self._execute_sub_workflow_operation(operation, input_payload, context)

                if operation.operation_type == "for_each":
                    return await self._execute_for_each_operation(
                        operation,
                        input_payload,
                        context,
                        parent_operation_run_id=op_run.id,
                    )

                if operation.operation_type == "condition":
                    return await self._execute_condition_operation(
                        operation,
                        input_payload,
                        context,
                        parent_operation_run_id=op_run.id,
                    )

                if operation.operation_type == "set_variable":
                    return await self._execute_set_variable_operation(operation, input_payload, context)

                if operation.operation_type == "new_thread":
                    return await self._execute_new_thread_operation(operation, input_payload, context)

                if operation.operation_type == "merge":
                    return await self._execute_merge_operation(operation, input_payload, context)

                if operation.operation_type in {"wait", "delay"}:
                    return await self._execute_wait_operation(operation, input_payload, context)

                if operation.operation_type == "notification":
                    return await self._execute_notification_operation(operation, input_payload, context)

                if operation.operation_type == "fail":
                    return await self._execute_fail_operation(operation, input_payload, context)

                if operation.operation_type == "http_request":
                    return await self._execute_http_request_operation(operation, input_payload, context)

                if operation.operation_type == "artifact":
                    return await self._execute_artifact_operation(operation, input_payload, context)

                if operation.operation_type == "tool":
                    return await self._execute_tool_operation(operation, input_payload, context)

                if operation.operation_type == "board_pull":
                    return await self._execute_board_pull_operation(operation, input_payload, context)

                log.errorx(
                    "Unsupported workflow operation_type",
                    workflow_run_id=context.get("workflow_run_id"),
                    operation_id=operation.id,
                    operation_type=operation.operation_type,
                )
                raise ValueError(f"Unsupported workflow operation_type: {operation.operation_type}")

            timeout_seconds = getattr(operation, "timeout_seconds", None) or (operation.config or {}).get("timeout_seconds")
            log.infox(
                "Workflow operation uitvoeren",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=operation.id,
                operation_run_id=getattr(op_run, "id", None),
                timeout_seconds=timeout_seconds,
            )
            # Cancel-aware: interrupts a long in-flight op when a cancel is requested.
            # Pass the raw timeout (the helper applies int() for wait_for, matching
            # the prior behavior where int(0.01)==0 → immediate timeout).
            output = await self._run_op_body_with_cancel(
                execute_operation_body,
                run_id=context["workflow_run_id"],
                timeout_seconds=timeout_seconds,
            )

        except WorkflowCancelled:
            log.warningx(
                "Workflow operation geannuleerd door workflow cancel request",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=operation.id,
                operation_run_id=getattr(op_run, "id", None),
            )
            self.run_repository.mark_operation_cancelled(
                op_run.id,
                error="cancelled because workflow run cancellation was requested",
            )
            raise

        except asyncio.CancelledError:
            log.warningx(
                "Workflow operation asyncio task cancelled",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=operation.id,
                operation_run_id=getattr(op_run, "id", None),
            )
            self.run_repository.mark_operation_cancelled(
                op_run.id,
                error="cancelled because workflow task was stopped",
            )
            raise

        except Exception as exc:
            if isinstance(exc, WorkflowOperationOutputError):
                self.run_repository.fail_operation_run(
                    op_run.id,
                    error=str(exc),
                    output_payload=exc.output_payload,
                    trace=exc.trace,
                )
                raise
            policy = self._operation_retry_policy(operation)
            max_attempts = max(1, min(int(policy.get("max_attempts") or 1), 5))
            backoff = max(0.0, float(policy.get("backoff_seconds") or 0))
            retry_trace = []
            last_exc = exc
            output = None
            if self._retry_allowed_for_error(policy, exc) and max_attempts > 1:
                for attempt in range(2, max_attempts + 1):
                    self._append_trace_event(retry_trace, "workflow_operation_retry_scheduled", {"operation_id": operation.id, "attempt": attempt, "max_attempts": max_attempts, "error": self._compact_preview(last_exc, 300)})
                    if backoff:
                        await asyncio.sleep(backoff)
                    self._append_trace_event(retry_trace, "workflow_operation_retry_started", {"operation_id": operation.id, "attempt": attempt, "max_attempts": max_attempts})
                    try:
                        timeout_seconds = getattr(operation, "timeout_seconds", None) or (operation.config or {}).get("timeout_seconds")
                        if timeout_seconds:
                            output = await asyncio.wait_for(execute_operation_body(), timeout=int(timeout_seconds))
                        else:
                            output = await execute_operation_body()
                        if isinstance(output, dict):
                            output.setdefault("trace", []).extend(retry_trace)
                            output["attempt_count"] = attempt
                        break
                    except Exception as retry_exc:
                        last_exc = retry_exc
                else:
                    self._append_trace_event(retry_trace, "workflow_operation_retry_exhausted", {"operation_id": operation.id, "max_attempts": max_attempts, "error": self._compact_preview(last_exc, 300)}, level="error")
                    self.run_repository.fail_operation_run(op_run.id, error=str(last_exc), trace=retry_trace)
                    raise last_exc
            else:
                if isinstance(exc, TimeoutError):
                    self._append_trace_event(retry_trace, "workflow_operation_timeout", {"operation_id": operation.id, "error": self._compact_preview(exc, 300)}, level="error")
                log.exceptionx(
                    "Workflow operation uitvoeren mislukt",
                    workflow_run_id=context.get("workflow_run_id"),
                    operation_id=operation.id,
                    operation_run_id=getattr(op_run, "id", None),
                    operation_type=getattr(operation, "operation_type", None),
                    exception=exc,
                )
                self.run_repository.fail_operation_run(op_run.id, error=str(exc), trace=retry_trace or None)
                raise

        if isinstance(output, dict) and input_payload.get("input_mapping_trace"):
            output.setdefault("trace", [])[:0] = input_payload.get("input_mapping_trace") or []

        if isinstance(output, dict) and output.get("mode") == "workflow_waiting":
            pending_state = output.get("pending_state") or output.get("pending_action") or {}
            waiting_status = output.get("status") or "waiting_for_user_input"
            self.run_repository.mark_waiting_operation_run(
                op_run.id,
                status=waiting_status,
                pending_state=pending_state,
                trace=output.get("trace") if isinstance(output, dict) else None,
                output_payload=output,
            )
            self.run_repository.mark_waiting(context["workflow_run_id"], result_payload={
                "status": "waiting",
                "waiting_operation_id": operation.id,
                "pending_state": pending_state,
                "workflow_variables": context.get("workflow_variables") or {},
            })
            return output

        self._raise_if_cancel_requested(context["workflow_run_id"])

        log.infox(
            "Workflow operation run afronden",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=operation.id,
            operation_run_id=getattr(op_run, "id", None),
            output_type=type(output).__name__,
            output_keys=list(output.keys()) if isinstance(output, dict) else None,
            has_trace=isinstance(output, dict) and bool(output.get("trace")),
        )
        if isinstance(output, dict):
            try:
                self._validate_output_contract(operation, output)
            except Exception as exc:
                self.run_repository.fail_operation_run(
                    op_run.id,
                    error=str(exc),
                    output_payload=output,
                    trace=output.get("trace"),
                )
                raise
        self.run_repository.finish_operation_run(
            op_run.id,
            output_payload=output,
            trace=output.get("trace") if isinstance(output, dict) else None,
        )

        log.infox(
            "Workflow operation uitvoeren afgerond",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=operation.id,
            operation_run_id=getattr(op_run, "id", None),
            output_mode=output.get("mode") if isinstance(output, dict) else None,
            output_status=output.get("status") if isinstance(output, dict) else None,
        )
        return output

    def _build_operation_input(self, operation: Any, context: Dict[str, Any]) -> Dict[str, Any]:
        log.debugx(
            "Workflow operation input bouwen gestart",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            operation_type=getattr(operation, "operation_type", None),
        )
        config = operation.config or {}
        mapped_inputs, missing_inputs, mapping_errors, mapping_summary = self._resolve_input_mapping(operation, context)
        trace = []
        event_data = {
            "operation_id": getattr(operation, "id", None),
            "operation_name": getattr(operation, "name", None),
            "resolved_keys": sorted(mapped_inputs.keys()),
            "missing_keys": missing_inputs,
            "sources": mapping_summary,
        }
        if missing_inputs or mapping_errors:
            self._append_trace_event(trace, "workflow_input_mapping_failed", event_data, level="error")
        elif mapping_summary:
            self._append_trace_event(trace, "workflow_input_mapping_resolved", event_data)
        result = {
            "workflow_input": context.get("input") or {},
            "previous_outputs": self._compact_workflow_previous_outputs(
                context.get("operation_outputs") or {},
                context.get("operation_statuses") or {},
            ),
            "operation_config": config,
            "workflow_variables": context.get("workflow_variables") or {},
            "mapped_inputs": mapped_inputs,
        }
        if trace:
            result["input_mapping_trace"] = trace
        if missing_inputs or mapping_errors:
            output_payload = {
                "status": "failed",
                "error_type": "input_mapping_failed",
                "missing_inputs": missing_inputs,
                "mapping_errors": mapping_errors,
            }
            raise WorkflowInputMappingError(
                "input_mapping_failed",
                input_payload=result,
                output_payload=output_payload,
                trace=trace,
            )
        log.debugx(
            "Workflow operation input bouwen afgerond",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            workflow_input_keys=list((result.get("workflow_input") or {}).keys()) if isinstance(result.get("workflow_input"), dict) else None,
            previous_output_count=len(result.get("previous_outputs") or {}),
            operation_config_keys=list((config or {}).keys()) if isinstance(config, dict) else None,
        )
        return result

    def _validate_condition_branch_ids(self, values: Any, *, branch: str) -> List[int]:
        if values in (None, ""):
            return []
        if not isinstance(values, list):
            raise ValueError(f"condition {branch}_operation_ids must be an array of integers")
        ids: List[int] = []
        for raw in values:
            if isinstance(raw, bool):
                raise ValueError(f"condition branch operation id must be an integer: {raw}")
            if isinstance(raw, int):
                ids.append(raw)
                continue
            if isinstance(raw, str) and raw.strip().isdigit():
                ids.append(int(raw.strip()))
                continue
            raise ValueError(f"condition branch operation id must be an integer: {raw}")
        return ids

    def _validate_condition_branch_workflow_id(self, value: Any, *, field: str) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise ValueError(f"condition {field} must be an integer: {value}")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        raise ValueError(f"condition {field} must be an integer: {value}")

    def _build_condition_child_input(
        self,
        *,
        operation: Any,
        input_payload: Dict[str, Any],
        context: Dict[str, Any],
        condition: Dict[str, Any],
        result: bool,
        matched_value: Any,
        branch_input: Dict[str, Any],
    ) -> Dict[str, Any]:
        child_input: Dict[str, Any] = {}
        if branch_input.get("include_condition_result", True):
            child_input["condition"] = {
                "result": result,
                "selected_branch": "true" if result else "false",
                "matched_value": matched_value,
                "source": condition.get("source"),
                "path": condition.get("path"),
                "operator": condition.get("operator") or "equals",
                "expected_value": condition.get("value"),
                "parent_operation_id": operation.id,
                "parent_workflow_run_id": context.get("workflow_run_id"),
            }
        if branch_input.get("include_workflow_variables", True):
            child_input["workflow_variables"] = dict(context.get("workflow_variables") or {})
        if branch_input.get("include_parent_input", True):
            parent_input = context.get("input") or {}
            child_input["parent_input"] = {k: v for k, v in parent_input.items() if k != "_workflow_stack"} if isinstance(parent_input, dict) else parent_input
        if branch_input.get("include_mapped_inputs", True):
            child_input["mapped_inputs"] = dict(input_payload.get("mapped_inputs") or {})
        workflow_stack = list((context.get("input") or {}).get("_workflow_stack") or [])
        child_input["_workflow_stack"] = [*workflow_stack, int(context["workflow_id"])]
        return child_input

    async def _execute_condition_sub_workflow_branch(
        self,
        operation: Any,
        input_payload: Dict[str, Any],
        context: Dict[str, Any],
        *,
        condition: Dict[str, Any],
        result: bool,
        matched_value: Any,
        trace: list,
        parent_operation_run_id: int | None,
    ) -> Dict[str, Any]:
        config = operation.config or {}
        true_workflow_id = self._validate_condition_branch_workflow_id(config.get("true_workflow_id"), field="true_workflow_id")
        false_workflow_id = self._validate_condition_branch_workflow_id(config.get("false_workflow_id"), field="false_workflow_id")
        selected_branch = "true" if result else "false"
        selected_workflow_id = true_workflow_id if result else false_workflow_id
        if selected_workflow_id is None:
            raise ValueError(f"condition branch workflow is not configured for branch: {selected_branch}")

        workflow_stack = list((context.get("input") or {}).get("_workflow_stack") or [])
        current_workflow_id = int(context["workflow_id"])
        max_depth = int(config.get("max_depth") or 5)
        if len(workflow_stack) >= max_depth:
            raise RuntimeError(f"Condition branch max_depth exceeded: max_depth={max_depth}, stack={workflow_stack}")
        if selected_workflow_id in workflow_stack or selected_workflow_id == current_workflow_id:
            raise RuntimeError(
                f"Condition branch recursive workflow loop detected: selected_workflow_id={selected_workflow_id}, "
                f"current_workflow_id={current_workflow_id}, stack={workflow_stack}"
            )

        selected_workflow = self.workflow_repository.get_by_id(selected_workflow_id)
        if not selected_workflow:
            raise ValueError(f"condition branch workflow not found: {selected_workflow_id}")
        if not getattr(selected_workflow, "is_enabled", True):
            raise ValueError(f"condition branch workflow is disabled: {selected_workflow_id}")

        self._append_trace_event(trace, "workflow_condition_branch_workflow_selected", {
            "operation_id": operation.id,
            "result": result,
            "selected_branch": selected_branch,
            "selected_workflow_id": selected_workflow_id,
        })

        branch_input = config.get("branch_input") if isinstance(config.get("branch_input"), dict) else {}
        child_input = self._build_condition_child_input(
            operation=operation,
            input_payload=input_payload,
            context=context,
            condition=condition,
            result=result,
            matched_value=matched_value,
            branch_input=branch_input,
        )

        self._raise_if_cancel_requested(context["workflow_run_id"])
        child_run = self.run_repository.create_run(
            workflow_id=selected_workflow_id,
            trigger_type="condition_true" if result else "condition_false",
            input_payload=child_input,
            parent_run_id=context["workflow_run_id"],
            parent_operation_run_id=parent_operation_run_id,
        )
        self._append_trace_event(trace, "workflow_condition_branch_child_run_created", {
            "operation_id": operation.id,
            "selected_branch": selected_branch,
            "selected_workflow_id": selected_workflow_id,
            "child_workflow_run_id": child_run.id,
        })

        log.infox(
            "Condition branch child workflow run aangemaakt",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=operation.id,
            selected_branch=selected_branch,
            selected_workflow_id=selected_workflow_id,
            child_workflow_run_id=child_run.id,
        )

        try:
            child_result = await self.execute_run(child_run.id)
        except Exception as exc:
            self._append_trace_event(trace, "workflow_condition_branch_child_run_failed", {
                "operation_id": operation.id,
                "selected_branch": selected_branch,
                "selected_workflow_id": selected_workflow_id,
                "child_workflow_run_id": child_run.id,
                "error": self._compact_preview(exc, 300),
            }, level="error")
            raise RuntimeError(
                f"condition branch child workflow run failed: child_workflow_run_id={child_run.id}, error={exc}"
            ) from exc

        child_status = child_result.get("status") if isinstance(child_result, dict) else "unknown"
        self._append_trace_event(trace, "workflow_condition_branch_child_run_completed", {
            "operation_id": operation.id,
            "result": result,
            "selected_branch": selected_branch,
            "selected_workflow_id": selected_workflow_id,
            "child_workflow_run_id": child_run.id,
            "child_status": child_status,
        })

        child_handoff = child_result.get("downstream_handoff") if isinstance(child_result, dict) else None
        return {
            "mode": "condition",
            "status": "success",
            "evaluated": True,
            "result": result,
            "selected_branch": selected_branch,
            "matched_value": matched_value,
            "condition": {
                "source": condition.get("source"),
                "operation_id": condition.get("operation_id"),
                "path": condition.get("path"),
                "operator": condition.get("operator") or "equals",
            },
            "branch_mode": "sub_workflow",
            "selected_workflow_id": selected_workflow_id,
            "child_workflow_run_id": child_run.id,
            "child_status": child_status,
            "selected_operation_ids": [],
            "skipped_operation_ids": [],
            "downstream_handoff": self._compact_json_preview(child_handoff) if child_handoff else {},
            "trace": trace,
        }

    async def _execute_condition_operation(
        self,
        operation: Any,
        input_payload: Dict[str, Any],
        context: Dict[str, Any],
        *,
        parent_operation_run_id: int | None = None,
    ) -> Dict[str, Any]:
        config = operation.config or {}
        condition = config.get("condition")
        if not isinstance(condition, dict):
            raise ValueError("condition operation requires config.condition")
        branch_mode = str(config.get("branch_mode") or "inline").strip().lower()
        if branch_mode not in {"inline", "sub_workflow"}:
            raise ValueError(f"Unsupported condition branch_mode: {branch_mode}")
        trace: list = []
        if branch_mode == "inline":
            then_ids = self._validate_condition_branch_ids(config.get("then_operation_ids") or [], branch="then")
            else_ids = self._validate_condition_branch_ids(config.get("else_operation_ids") or [], branch="else")
            self._append_trace_event(trace, "workflow_condition_branch_ids_validated", {
                "operation_id": operation.id,
                "then_operation_ids": then_ids,
                "else_operation_ids": else_ids,
            })
        found, matched_value = self._condition_source_value(condition, operation, context)
        result = self._evaluate_condition_operator(found, matched_value, condition.get("operator") or "equals", condition.get("value"))
        self._append_trace_event(trace, "workflow_condition_evaluated", {
            "operation_id": operation.id,
            "operation_name": getattr(operation, "name", None),
            "path": condition.get("path"),
            "operator": condition.get("operator") or "equals",
            "result": result,
            "selected_branch": ("true" if result else "false") if branch_mode == "sub_workflow" else ("then" if result else "else"),
        })
        if branch_mode == "sub_workflow":
            return await self._execute_condition_sub_workflow_branch(
                operation,
                input_payload,
                context,
                condition=condition,
                result=result,
                matched_value=matched_value,
                trace=trace,
                parent_operation_run_id=parent_operation_run_id,
            )
        selected_branch = "then" if result else "else"
        selected_ids = then_ids if result else else_ids
        skipped_ids = else_ids if result else then_ids
        output = {
            "mode": "condition",
            "status": "success",
            "evaluated": True,
            "result": result,
            "selected_branch": selected_branch,
            "matched_value": matched_value,
            "condition": {
                "source": condition.get("source"),
                "operation_id": condition.get("operation_id"),
                "path": condition.get("path"),
                "operator": condition.get("operator") or "equals",
            },
            "branch_mode": "inline",
            "selected_operation_ids": selected_ids,
            "skipped_operation_ids": skipped_ids,
            "trace": trace,
        }
        return output

    async def _execute_set_variable_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        config = operation.config or {}
        variables = config.get("variables")
        if not isinstance(variables, dict):
            raise ValueError("set_variable operation requires config.variables")
        allow_null = bool(config.get("allow_null", False))
        deps = list(getattr(operation, "depends_on", None) or [])
        previous_id = int(deps[-1]) if deps else None
        resolved = {k: self._resolve_template_value(v, context, previous_operation_id=previous_id, allow_null=allow_null) for k, v in variables.items()}
        context.setdefault("workflow_variables", {}).update(resolved)
        output = {"mode": "set_variable", "status": "success", "variables_set": resolved, "trace": []}
        self._append_trace_event(output["trace"], "workflow_variables_set", {"operation_id": operation.id, "variable_names": list(resolved.keys())})
        return output

    async def _execute_board_pull_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Select the top-N items of a board column and emit them as a For-Each
        iterable. This is the 'work the TODO / a subset' primitive: a downstream
        For-Each fans out over the items (each item becomes a child input_payload),
        and each child runs an assistant step (orchestrator OR claude_code engine)
        that does the work and writes the result back via board__update.

        config:
          {"status": "todo",        # which column to pull from
           "limit": 3,              # top-N by priority (the throttle)
           "ready_only": true,      # skip items with unfinished dependencies
           "iterable_name": "items",# For-Each iterable key
           "claim": true}           # move pulled items to 'doing' so a later run
                                    # doesn't pick them up again
        """
        from db.database import SessionLocal
        from services.board_service import BoardService

        config = operation.config or {}
        status = str(config.get("status") or "todo").strip().lower()
        limit = int(config.get("limit") if config.get("limit") is not None else 3)
        ready_only = bool(config.get("ready_only", True))
        iterable_name = str(config.get("iterable_name") or "items").strip() or "items"
        claim = bool(config.get("claim", True))

        with SessionLocal() as db:
            svc = BoardService(db)
            try:
                items = svc.pull(status=status, limit=limit, ready_only=ready_only)
            except ValueError as exc:
                raise ValueError(f"board_pull: {exc}")
            picked = [
                {
                    "board_item_id": it.id,
                    "title": it.title,
                    "description": it.description,
                    "acceptance": it.acceptance,
                    "priority": it.priority,
                    "labels": list(it.labels or []),
                    "depends_on": list(it.depends_on or []),
                }
                for it in items
            ]
            if claim:
                for it in items:
                    svc.move_item(it.id, "doing", actor="agent")

        trace: list = []
        self._append_trace_event(trace, "workflow_board_pull", {
            "operation_id": operation.id, "status": status, "limit": limit,
            "ready_only": ready_only, "claimed": claim, "picked": len(picked),
            "item_ids": [p["board_item_id"] for p in picked],
        })
        return {
            "mode": "board_pull",
            "status": "success",
            "answer": f"Pulled {len(picked)} item(s) from the '{status}' column.",
            "picked_count": len(picked),
            # For-Each reads downstream_handoff.iterables.<name>.
            "downstream_handoff": {
                "summary": f"{len(picked)} board item(s) pulled from '{status}'.",
                "full_answer": None,
                "artifacts": [],
                "facts": {"picked_item_ids": [p["board_item_id"] for p in picked]},
                "iterables": {iterable_name: picked},
                "open_questions": [],
                "status": "success",
            },
            "trace": trace,
        }

    async def _execute_new_thread_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Create a NEW conversation thread (a GUID) and store it as a workflow
        variable so downstream assistant activities can share it (config.thread =
        ${workflow_variables.<name>}). A workflow may create several threads
        (several new_thread ops → several variables) or reuse one. Assistant
        activities WITHOUT a thread keep creating their own isolated thread."""
        import uuid as _uuid
        config = operation.config or {}
        var_name = (config.get("variable") or config.get("name") or "thread").strip() or "thread"
        # A stable, workflow-run-scoped, human-traceable thread id.
        thread_id = f"wf-{context.get('workflow_run_id')}-{_uuid.uuid4().hex[:12]}"
        context.setdefault("workflow_variables", {})[var_name] = thread_id
        output = {
            "mode": "set_variable", "status": "success",
            "variables_set": {var_name: thread_id},
            "thread_id": thread_id, "trace": [],
        }
        self._append_trace_event(output["trace"], "workflow_thread_created",
                                 {"operation_id": operation.id, "variable": var_name, "thread_id": thread_id})
        return output

    async def _execute_tool_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Run a single builtin tool directly as a workflow step (no agent). config.tool_name
        is the builtin tool; config.args is its argument object (values may use ${...}
        references to prior operations / run_result / variables)."""
        config = operation.config or {}
        tool_name = (config.get("tool_name") or config.get("tool") or "").strip()
        if not tool_name:
            raise ValueError("tool operation requires config.tool_name")

        from services.builtin.internal_tool_registry import internal_tool_registry
        if not internal_tool_registry.has_tool(tool_name):
            raise ValueError(f"Unknown builtin tool '{tool_name}'")

        raw_args = config.get("args") if isinstance(config.get("args"), dict) else {}
        deps = list(getattr(operation, "depends_on", None) or [])
        previous_id = int(deps[-1]) if deps else None
        args = {
            k: self._resolve_template_value(v, context, previous_operation_id=previous_id, allow_null=True)
            for k, v in raw_args.items()
        }

        log.infox(
            "Workflow tool-operatie uitvoeren",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            tool=tool_name,
            arg_keys=list(args.keys()),
        )
        result = await internal_tool_registry.call(tool_name, args)

        failed = isinstance(result, dict) and (
            str(result.get("status") or "").lower() in {"error", "failed"} or result.get("ok") is False
        )
        output = {
            "mode": "tool",
            "status": "failed" if failed else "success",
            "tool": tool_name,
            "result": result,
            "downstream_handoff": {
                "summary": f"Ran builtin tool {tool_name}",
                "facts": result if isinstance(result, dict) else {"result": result},
                "status": "failed" if failed else "success",
            },
            "trace": [],
        }
        self._append_trace_event(output["trace"], "workflow_tool_executed", {
            "operation_id": operation.id, "tool": tool_name, "status": output["status"],
        })
        if failed:
            raise RuntimeError(
                f"Builtin tool {tool_name} failed: "
                + str((result or {}).get("error") or (result or {}).get("message") or result)[:300]
            )
        return output

    def _configured_merge_inputs(self, config: Dict[str, Any]) -> list[Dict[str, Any]]:
        if isinstance(config.get("inputs"), list):
            return list(config.get("inputs") or [])
        if config.get("input_operation_id") is not None:
            return [{"operation_id": config.get("input_operation_id"), "path": config.get("path")}]
        return []

    async def _execute_merge_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        config = operation.config or {}
        strategy = config.get("strategy") or "collect"
        output_key = config.get("output_key") or "merged_results"
        strict = bool(config.get("strict", False))
        values, warnings = [], []
        inputs = self._configured_merge_inputs(config)
        if not inputs:
            inputs = [{"operation_id": dep, "path": "downstream_handoff"} for dep in (getattr(operation, "depends_on", None) or [])]
        for item in inputs:
            op_id = item.get("operation_id") or self._operation_id_by_key(item.get("operation_key"), context)
            raw = (context.get("operation_outputs") or {}).get(int(op_id)) if op_id is not None else None
            path = item.get("path")
            if strategy == "collect_handoffs" and not path:
                path = "downstream_handoff"
            if strategy == "collect_facts" and not path:
                path = "downstream_handoff.facts"
            found, value = self._path_get(raw, path)
            if not found:
                warning = f"missing input operation_id={op_id} path={path}"
                warnings.append(warning)
                if strict:
                    raise ValueError(warning)
                continue
            values.append(value)
        if strategy in {"collect", "collect_handoffs", "collect_facts"}:
            merged = values
        elif strategy == "merge_objects":
            merged = {}
            for value in values:
                if isinstance(value, dict):
                    merged.update(value)
                elif strict:
                    raise ValueError("merge_objects requires object inputs")
        elif strategy == "concat_arrays":
            merged = []
            for value in values:
                if isinstance(value, list):
                    merged.extend(value)
                elif strict:
                    raise ValueError("concat_arrays requires array inputs")
        else:
            raise ValueError(f"Unsupported merge strategy: {strategy}")
        output = {"mode": "merge", "status": "success", output_key: merged, "warnings": warnings, "trace": []}
        self._append_trace_event(output["trace"], "workflow_merge_completed", {"operation_id": operation.id, "strategy": strategy, "warnings": warnings})
        return output

    async def _execute_wait_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        config = operation.config or {}
        duration = config.get("duration_seconds")
        until = config.get("until")
        if until:
            text = str(until)
            parsed = datetime.fromisoformat(text[:-1] if text.endswith("Z") else text)
            duration = max(0.0, (parsed - datetime.utcnow()).total_seconds())
        if duration is None:
            raise ValueError("wait operation requires duration_seconds or until")
        duration = float(duration)
        if duration <= 0:
            raise ValueError("wait duration_seconds must be positive")
        max_sleep = float(config.get("max_inline_wait_seconds", 5))
        if duration > max_sleep:
            raise ValueError(f"wait duration exceeds inline limit ({max_sleep} seconds); durable delayed resume is not implemented")
        trace = []
        self._append_trace_event(trace, "workflow_wait_started", {"operation_id": operation.id, "duration_seconds": duration})
        await asyncio.sleep(duration)
        self._append_trace_event(trace, "workflow_wait_completed", {"operation_id": operation.id, "waited_seconds": duration})
        return {"mode": "wait", "status": "success", "waited_seconds": duration, "trace": trace}

    def _collect_previous_errors(self, context: Dict[str, Any]) -> list[str]:
        """Errors recorded by earlier failed operations in this run, as
        '- <op name>: <error>' lines."""
        statuses = context.get("operation_statuses") or {}
        outputs = context.get("operation_outputs") or {}
        ops_by_id = {getattr(op, "id", None): op for op in (context.get("operations") or [])}
        lines: list[str] = []
        for op_id, status in statuses.items():
            if str(status).lower() != "failed":
                continue
            out = outputs.get(op_id)
            if out is None:
                out = outputs.get(str(op_id))
            err = out.get("error") if isinstance(out, dict) else None
            if not err:
                continue
            name = getattr(ops_by_id.get(op_id), "name", None) or f"operation {op_id}"
            lines.append(f"- {name}: {self._compact_preview(err, 500)}")
        return lines

    async def _execute_fail_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Explicitly fail the workflow at this point. Use it (e.g. on an on_failure
        branch) to stop the run with a clear reason. Raising routes through the normal
        failure handling: if this op has an on_failure_follow_up the run continues there,
        otherwise the whole run fails with this message."""
        config = operation.config or {}
        allow_null = bool(config.get("allow_null", True))
        message = str(self._resolve_template_value(
            config.get("message") or "Workflow stopped by a fail operation.",
            context,
            allow_null=allow_null,
        ))
        # Optionally fold in the errors from earlier failed activities so the upstream
        # cause travels with the failure into the run error + completion email.
        if config.get("include_previous_errors"):
            prev = self._collect_previous_errors(context)
            if prev:
                message = f"{message}\n\nErrors from previous activities:\n" + "\n".join(prev)
        error_code = config.get("error_code")
        if error_code:
            error_code = str(self._resolve_template_value(error_code, context, allow_null=allow_null)).strip()
        final_message = f"[{error_code}] {message}" if error_code else message
        log.warningx(
            "Workflow fail-operatie: run wordt expliciet gefaald",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=operation.id,
            error_code=error_code or None,
            message=self._compact_preview(message, 300),
        )
        raise RuntimeError(final_message)

    async def _execute_notification_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        config = operation.config or {}
        channel = str(config.get("channel") or "trace").lower()
        if channel not in {"ui", "trace", "email"}:
            raise ValueError("notification channel must be ui, email or trace")
        allow_null = bool(config.get("allow_null", True))
        subject = self._resolve_template_value(config.get("subject") or "Workflow notification", context, allow_null=allow_null)
        message = self._resolve_template_value(config.get("message") or "", context, allow_null=allow_null)
        severity = str(config.get("severity") or "info").lower()
        fail_on_error = bool(config.get("fail_on_notification_error", False))
        # Optional explicit email recipients (templated). When set, the notification is
        # emailed to exactly these addresses; otherwise the global recipient list is used.
        recipients_cfg = config.get("recipients")
        recipient_emails = None
        if isinstance(recipients_cfg, list):
            resolved = [self._resolve_template_value(r, context, allow_null=allow_null) for r in recipients_cfg]
            recipient_emails = [str(e).strip() for e in resolved if str(e or "").strip()] or None
        subject_preview = self._compact_preview(subject, 300)
        message_preview = self._compact_preview(message, 1000)
        output = {
            "mode": "notification",
            "status": "success",
            "channel": channel,
            "subject": subject_preview,
            "message": message_preview,
            "severity": severity,
            "sent": False,
            "trace": [],
        }
        self._append_trace_event(output["trace"], "workflow_notification_created", {
            "operation_id": operation.id,
            "channel": channel,
            "subject": subject_preview,
            "severity": severity,
        })
        if channel not in ("ui", "email"):
            return output

        notification_data = {
            "Workflow ID": context.get("workflow_id"),
            "Workflow run ID": context.get("workflow_run_id"),
            "Operation ID": operation.id,
            "Severity": severity,
            "Channel": channel,
        }
        action_url = f"https://www.nd3x.nl/workflows/runs/{context.get('workflow_run_id')}"
        try:
            sent = bool(send_system_notification(
                recipients=recipient_emails,
                db=getattr(self.run_repository, "db", None),
                subject=str(subject),
                title=subject_preview or "Workflow notification",
                message=str(message),
                data=notification_data,
                action_url=action_url,
            ))
        except Exception as exc:
            sent = False
            warning = f"workflow notification failed: {self._compact_preview(exc, 300)}"
        else:
            warning = None if sent else "workflow notification failed"

        output["sent"] = sent
        if sent:
            self._append_trace_event(output["trace"], "workflow_notification_sent", {
                "operation_id": operation.id,
                "channel": channel,
                "workflow_run_id": context.get("workflow_run_id"),
            })
            return output

        output["warning"] = warning
        self._append_trace_event(output["trace"], "workflow_notification_failed", {
            "operation_id": operation.id,
            "channel": channel,
            "workflow_run_id": context.get("workflow_run_id"),
            "error": warning,
        }, level="error" if fail_on_error else "warn")
        if fail_on_error:
            raise ValueError(warning)
        return output

    async def _execute_http_request_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        config = operation.config or {}
        method = str(config.get("method") or "GET").upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Unsupported http_request method")
        # Secret values are decrypted only here, at the outbound boundary, and
        # kept in `secrets` so they can be masked out of the trace/output — they
        # must never reach the LLM.
        secrets: list[str] = []
        url = self._inject_secrets(self._resolve_template_value(config.get("url"), context, allow_null=False), secrets)
        self._validate_http_url(str(url))
        timeout = min(float(config.get("timeout_seconds") or getattr(operation, "timeout_seconds", None) or 30), 120.0)
        headers = self._inject_secrets(self._resolve_template_value(config.get("headers") or {}, context, allow_null=True), secrets)
        query = self._inject_secrets(self._resolve_template_value(config.get("query") or {}, context, allow_null=True), secrets)
        body = self._inject_secrets(self._resolve_template_value(config.get("body"), context, allow_null=True), secrets)
        response_mode = str(config.get("response_mode") or "json").lower()
        trace = []
        # Redact known-sensitive header names AND mask any secret value that
        # landed in a non-sensitive header, so the trace never carries plaintext.
        trace_headers = {
            k: (self._mask_secrets(v, secrets) if isinstance(v, str) else v)
            for k, v in self._redact_headers(headers).items()
        }
        self._append_trace_event(trace, "workflow_http_request_started", {"operation_id": operation.id, "method": method, "url": self._mask_secrets(url, secrets), "headers": trace_headers})
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, str(url), headers=headers, params=query, json=body if isinstance(body, (dict, list)) else None, content=None if isinstance(body, (dict, list, type(None))) else str(body))
        except Exception as exc:
            self._append_trace_event(trace, "workflow_http_request_failed", {"operation_id": operation.id, "error": self._mask_secrets(self._compact_preview(exc, 300), secrets)}, level="error")
            raise
        text = resp.text or ""
        truncated = len(text) > 5000
        if response_mode == "status_only":
            response = None
        elif response_mode == "json":
            try:
                response = resp.json()
            except Exception:
                response = {"text_preview": self._compact_preview(text, 1000)}
        elif response_mode == "text":
            response = self._compact_preview(text, 5000)
        else:
            raise ValueError("Unsupported http_request response_mode")
        output = {"mode": "http_request", "status": "success", "http_status": resp.status_code, "response": response, "headers": self._redact_headers(dict(resp.headers)), "truncated": truncated, "trace": trace}
        fail = bool(config.get("fail_on_non_2xx", False)) and not (200 <= resp.status_code < 300)
        fail = fail or resp.status_code in set(config.get("fail_on_status") or [])
        self._append_trace_event(trace, "workflow_http_request_completed" if not fail else "workflow_http_request_failed", {"operation_id": operation.id, "http_status": resp.status_code}, level="error" if fail else "info")
        if fail:
            raise ValueError(f"http_request failed with status {resp.status_code}: {self._mask_secrets(self._compact_preview(text, 300), secrets)}")
        return output

    async def _execute_artifact_operation(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        config = operation.config or {}
        action = config.get("action") or "save_text"
        if action not in {"save_text", "save_json"}:
            raise ValueError("artifact action must be save_text or save_json")
        name = Path(str(config.get("name") or f"workflow_artifact_{operation.id}.txt")).name
        trace: list = []
        content_mode = None
        compat_template_content_from = False
        if "content" in config:
            content_mode = "content"
            content = self._resolve_template_value(config.get("content"), context)
        elif config.get("content_from") is not None:
            content_from = str(config.get("content_from"))
            if "${" in content_from:
                content_mode = "content_from_template_compat"
                compat_template_content_from = True
                content = self._resolve_template_value(content_from, context)
            else:
                content_mode = "content_from"
                found, content = self._resolve_reference(content_from, context)
                if not found:
                    raise ValueError(f"artifact content_from not found: {content_from}")
        else:
            raise ValueError("artifact operation requires content or content_from")
        artifact_trace_data = {
            "operation_id": operation.id,
            "mode": content_mode,
            "content_from": config.get("content_from") if content_mode and content_mode.startswith("content_from") else None,
            "compat_template_content_from": compat_template_content_from,
        }
        if compat_template_content_from:
            artifact_trace_data["warning"] = "content_from used template syntax; prefer content for templates"
        self._append_trace_event(trace, "workflow_artifact_content_resolved", artifact_trace_data, level="warn" if compat_template_content_from else "info")
        if action == "save_json":
            data = json.dumps(content, ensure_ascii=False, indent=2)
            content_type = config.get("content_type") or "application/json"
        else:
            data = str(content)
            content_type = config.get("content_type") or "text/plain"
        root = (Path(settings.FILES_DIR) / "workflows" / str(context.get("workflow_run_id")) / "artifacts").resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = (root / name).resolve()
        if root not in path.parents and path != root:
            raise ValueError("artifact path escapes workflow artifact directory")
        path.write_text(data, encoding="utf-8")
        output = {"mode": "artifact", "status": "success", "artifact": {"name": name, "path": str(path), "content_type": content_type, "size_bytes": len(data.encode('utf-8'))}, "trace": trace}
        self._append_trace_event(output["trace"], "workflow_artifact_saved", {"operation_id": operation.id, "name": name, "content_type": content_type, "size_bytes": output["artifact"]["size_bytes"]})
        return output

    def _resolve_for_each_items_source(self, spec: Dict[str, Any], operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> tuple[bool, Any]:
        source = str(spec.get("source") or "static").strip().lower()
        if source == "mapped_inputs":
            return self._path_get(input_payload.get("mapped_inputs") or {}, spec.get("path"))
        return self._resolve_input_mapping_source(spec, operation, context)

    def _legacy_for_each_items(self, operation: Any, context: Dict[str, Any]) -> tuple[list, str, Dict[str, Any]]:
        config = operation.config or {}
        source = config.get("iterable_source") or {}
        if "operation_id" in source:
            source_operation_id = int(source["operation_id"])
        else:
            source_position = int(source["operation_position"])
            source_operation = next(
                (op for op in context.get("operations", []) if int(getattr(op, "position", -1)) == source_position),
                None,
            )
            if not source_operation:
                raise ValueError(f"Iterable source operation_position not found: {source_position}")
            source_operation_id = source_operation.id
        iterable_name = source.get("name") or "primary"
        source_output = context["operation_outputs"].get(source_operation_id) or {}
        handoff = source_output.get("downstream_handoff") or {}
        iterable = (handoff.get("iterables") or {}).get(iterable_name)
        return iterable, iterable_name, {
            "mode": "iterable_source",
            "source": "operation_output",
            "operation_id": source_operation_id,
            "path": f"downstream_handoff.iterables.{iterable_name}",
        }

    def _resolve_for_each_items(self, operation: Any, input_payload: Dict[str, Any], context: Dict[str, Any]) -> tuple[list, str, Dict[str, Any], list]:
        config = operation.config or {}
        trace: list = []
        if isinstance(config.get("items_source"), dict):
            spec = config.get("items_source") or {}
            source_summary = {
                "mode": "items_source",
                "source": spec.get("source"),
                "path": spec.get("path"),
                "operation_id": spec.get("operation_id"),
                "operation_key": spec.get("operation_key"),
            }
            found, value = self._resolve_for_each_items_source(spec, operation, input_payload, context)
            if not found:
                if spec.get("required", True):
                    output = {
                        "status": "failed",
                        "error_type": "for_each_items_resolution_failed",
                        "source": source_summary,
                        "error": "items_source did not resolve",
                    }
                    self._append_trace_event(trace, "workflow_for_each_items_resolution_failed", {"operation_id": operation.id, **source_summary, "error_type": output["error_type"]}, level="error")
                    raise WorkflowOperationOutputError("for_each_items_resolution_failed", output_payload=output, trace=trace)
                value = spec.get("default", [])
            if isinstance(value, dict) and "items" in value and bool(spec.get("extract_items", True)):
                value = value.get("items")
            iterable_name = spec.get("name") or config.get("result_key") or "items"
        else:
            try:
                value, iterable_name, source_summary = self._legacy_for_each_items(operation, context)
            except Exception as exc:
                output = {
                    "status": "failed",
                    "error_type": "for_each_items_resolution_failed",
                    "source": {"mode": "iterable_source"},
                    "error": self._compact_preview(exc, 300),
                }
                self._append_trace_event(trace, "workflow_for_each_items_resolution_failed", {"operation_id": operation.id, "source": "iterable_source", "error": output["error"]}, level="error")
                raise WorkflowOperationOutputError("for_each_items_resolution_failed", output_payload=output, trace=trace)
        if not isinstance(value, list):
            output = {
                "status": "failed",
                "error_type": "for_each_items_not_array",
                "source": source_summary,
                "resolved_type": type(value).__name__,
            }
            self._append_trace_event(trace, "workflow_for_each_items_resolution_failed", {"operation_id": operation.id, **source_summary, "error_type": output["error_type"], "resolved_type": output["resolved_type"]}, level="error")
            raise WorkflowOperationOutputError("for_each_items_not_array", output_payload=output, trace=trace)
        self._append_trace_event(trace, "workflow_for_each_items_resolved", {
            "operation_id": operation.id,
            "operation_name": getattr(operation, "name", None),
            "source": source_summary,
            "items_count": len(value),
            "used_items_source": isinstance(config.get("items_source"), dict),
        })
        return value, iterable_name, source_summary, trace

    async def _execute_for_each_operation(
            self,
            operation: Any,
            input_payload: Dict[str, Any],
            context: Dict[str, Any],
            *,
            parent_operation_run_id: int,
    ) -> Dict[str, Any]:
        log.infox(
            "ForEach operation uitvoeren gestart",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_id=getattr(operation, "id", None),
            operation_ref_id=getattr(operation, "operation_ref_id", None),
            parent_operation_run_id=parent_operation_run_id,
        )
        config = operation.config or {}

        workflow_stack = list((context.get("input") or {}).get("_workflow_stack") or [])
        current_workflow_id = int(context["workflow_id"])
        target_workflow_id = int(operation.operation_ref_id)

        max_depth = int(config.get("max_depth") or 5)

        log.debugx(
            "ForEach recursion guard controleren",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            workflow_stack=workflow_stack,
            current_workflow_id=current_workflow_id,
            target_workflow_id=target_workflow_id,
            max_depth=max_depth,
        )

        if len(workflow_stack) >= max_depth:
            log.errorx(
                "ForEach max_depth overschreden",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
                max_depth=max_depth,
                workflow_stack=workflow_stack,
            )
            raise RuntimeError(
                f"ForEach max_depth exceeded: max_depth={max_depth}, stack={workflow_stack}"
            )

        if target_workflow_id in workflow_stack or target_workflow_id == current_workflow_id:
            log.errorx(
                "ForEach recursive workflow loop gedetecteerd",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
                target_workflow_id=target_workflow_id,
                current_workflow_id=current_workflow_id,
                workflow_stack=workflow_stack,
            )
            raise RuntimeError(
                f"ForEach recursive workflow loop detected: target_workflow_id={target_workflow_id}, "
                f"current_workflow_id={current_workflow_id}, stack={workflow_stack}"
            )

        items, iterable_name, items_source_summary, for_each_trace = self._resolve_for_each_items(operation, input_payload, context)

        log.infox(
            "ForEach iterable opgehaald",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            iterable_name=iterable_name,
            source=items_source_summary,
            iterable_count=len(items),
        )

        max_concurrency = int(config.get("max_concurrency") or 3)
        failure_strategy = config.get("failure_strategy") or "stop"
        result_key = config.get("result_key") or f"{iterable_name}_results"

        log.infox(
            "ForEach uitvoering configuratie bepaald",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            item_count=len(items),
            max_concurrency=max_concurrency,
            failure_strategy=failure_strategy,
            result_key=result_key,
        )

        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_item(index: int, item: Dict[str, Any]) -> Dict[str, Any]:
            child_run = None

            log.infox(
                "ForEach item uitvoeren gestart",
                workflow_run_id=context.get("workflow_run_id"),
                parent_operation_run_id=parent_operation_run_id,
                operation_id=getattr(operation, "id", None),
                item_index=index,
                item_type=type(item).__name__,
                item_keys=list(item.keys()) if isinstance(item, dict) else None,
            )

            if not isinstance(item, dict):
                log.errorx(
                    "ForEach item is geen JSON object",
                    workflow_run_id=context.get("workflow_run_id"),
                    operation_id=getattr(operation, "id", None),
                    item_index=index,
                    item_type=type(item).__name__,
                )
                raise ValueError(f"ForEach item at index {index} must be a JSON object")

            item_preview = self._compact_json_preview(item, 1000)
            self._append_trace_event(for_each_trace, "workflow_for_each_item_started", {
                "operation_id": operation.id,
                "index": index,
                "total": len(items),
                "item_preview": item_preview,
            })

            async with semaphore:
                try:
                    item_timeout = int(
                        config.get("item_timeout_seconds")
                        or operation.timeout_seconds
                        or 1800
                    )

                    child_input = dict(item)
                    child_input["for_each_item"] = dict(item)
                    child_input["for_each"] = {
                        "index": index,
                        "total": len(items),
                        "parent_operation_id": operation.id,
                        "parent_operation_key": getattr(operation, "name", None),
                    }
                    child_input["_workflow_stack"] = [
                        *workflow_stack,
                        current_workflow_id,
                    ]

                    self._raise_if_cancel_requested(context["workflow_run_id"])

                    log.infox(
                        "ForEach child workflow run aanmaken",
                        workflow_run_id=context.get("workflow_run_id"),
                        operation_id=getattr(operation, "id", None),
                        item_index=index,
                        target_workflow_id=operation.operation_ref_id,
                        item_timeout_seconds=item_timeout,
                        child_input_keys=list(child_input.keys()),
                    )
                    child_run = self.run_repository.create_run(
                        workflow_id=operation.operation_ref_id,
                        trigger_type="for_each",
                        input_payload=child_input,
                        parent_run_id=context["workflow_run_id"],
                        parent_operation_run_id=parent_operation_run_id,
                        parent_item_index=index,
                    )

                    log.infox(
                        "ForEach child workflow run aangemaakt",
                        workflow_run_id=context.get("workflow_run_id"),
                        child_workflow_run_id=getattr(child_run, "id", None),
                        item_index=index,
                    )

                    result = await asyncio.wait_for(
                        self.execute_run(child_run.id),
                        timeout=item_timeout,
                    )

                    log.infox(
                        "ForEach item succesvol afgerond",
                        workflow_run_id=context.get("workflow_run_id"),
                        child_workflow_run_id=getattr(child_run, "id", None),
                        item_index=index,
                        result_status=result.get("status") if isinstance(result, dict) else None,
                    )

                    downstream_handoff = result.get("downstream_handoff") if isinstance(result, dict) else None
                    self._append_trace_event(for_each_trace, "workflow_for_each_item_completed", {
                        "operation_id": operation.id,
                        "index": index,
                        "status": "success",
                        "child_workflow_run_id": child_run.id,
                    })
                    return {
                        "index": index,
                        "status": "success",
                        "child_workflow_run_id": child_run.id,
                        "item_preview": item_preview,
                        "input": item,
                        "downstream_handoff": downstream_handoff,
                        "result_status": result.get("status") if isinstance(result, dict) else None,
                    }

                except WorkflowCancelled as exc:
                    log.warningx(
                        "ForEach item geannuleerd door workflow cancel",
                        workflow_run_id=context.get("workflow_run_id"),
                        child_workflow_run_id=getattr(child_run, "id", None),
                        item_index=index,
                        error=str(exc),
                    )
                    self.run_repository.request_cancel_child_runs(
                        context["workflow_run_id"],
                    )

                    self._append_trace_event(for_each_trace, "workflow_for_each_item_failed", {
                        "operation_id": operation.id,
                        "index": index,
                        "status": "cancelled",
                        "error": self._compact_preview(exc, 300),
                    }, level="warn")
                    return {
                        "index": index,
                        "status": "cancelled",
                        "child_workflow_run_id": getattr(child_run, "id", None),
                        "item_preview": item_preview,
                        "input": item,
                        "error": str(exc),
                    }

                except asyncio.CancelledError:
                    log.warningx(
                        "ForEach item asyncio task cancelled",
                        workflow_run_id=context.get("workflow_run_id"),
                        child_workflow_run_id=getattr(child_run, "id", None),
                        item_index=index,
                    )
                    if child_run is not None:
                        self.run_repository.mark_cancelled(
                            child_run.id,
                            result_payload={
                                "status": "cancelled",
                                "reason": "for_each sibling failure or parent cancellation",
                            },
                        )

                    raise

                except Exception as exc:
                    log.warningx(
                        "ForEach item mislukt",
                        workflow_run_id=context.get("workflow_run_id"),
                        child_workflow_run_id=getattr(child_run, "id", None),
                        item_index=index,
                        failure_strategy=failure_strategy,
                        error=str(exc),
                    )
                    self._append_trace_event(for_each_trace, "workflow_for_each_item_failed", {
                        "operation_id": operation.id,
                        "index": index,
                        "status": "failed",
                        "error": self._compact_preview(exc, 300),
                    }, level="error")
                    if failure_strategy == "stop":
                        self.run_repository.request_cancel_for_each_sibling_runs(
                            parent_run_id=context["workflow_run_id"],
                            parent_operation_run_id=parent_operation_run_id,
                            except_run_id=getattr(child_run, "id", None),
                            reason=f"cancelled because for_each item {index} failed: {exc}",
                        )
                        raise

                    return {
                        "index": index,
                        "status": "failed",
                        "child_workflow_run_id": getattr(child_run, "id", None),
                        "item_preview": item_preview,
                        "input": item,
                        "error": str(exc),
                    }

        tasks = [
            asyncio.create_task(run_item(index, item))
            for index, item in enumerate(items)
        ]

        log.infox(
            "ForEach item tasks aangemaakt",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            task_count=len(tasks),
        )

        results = []

        try:
            for completed in asyncio.as_completed(tasks):
                result = await completed
                results.append(result)

                successes = [r for r in results if r.get("status") == "success"]
                failures = [r for r in results if r.get("status") == "failed"]

                log.infox(
                    "ForEach item resultaat verwerkt",
                    workflow_run_id=context.get("workflow_run_id"),
                    operation_id=getattr(operation, "id", None),
                    parent_operation_run_id=parent_operation_run_id,
                    items_total=len(items),
                    items_done=len(results),
                    items_success=len(successes),
                    items_failed=len(failures),
                    latest_index=result.get("index") if isinstance(result, dict) else None,
                    latest_status=result.get("status") if isinstance(result, dict) else None,
                )

                self.run_repository.update_operation_run_progress(
                    parent_operation_run_id,
                    {
                        "mode": "for_each",
                        "status": "running",
                        "iterable_name": iterable_name,
                        "items_total": len(items),
                        "items_done": len(results),
                        "items_success": len(successes),
                        "items_failed": len(failures),
                        "latest": result,
                        "results": [
                            {
                                "index": r.get("index"),
                                "status": r.get("status"),
                                "child_workflow_run_id": r.get("child_workflow_run_id"),
                                "error": r.get("error"),
                            }
                            for r in sorted(results, key=lambda x: x.get("index", 0))
                        ],
                    },
                )
                self._raise_if_cancel_requested(context["workflow_run_id"])


        except WorkflowCancelled:

            log.warningx(
                "ForEach operation geannuleerd door parent workflow cancel",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
                parent_operation_run_id=parent_operation_run_id,
            )

            self.run_repository.request_cancel_for_each_sibling_runs(

                parent_run_id=context["workflow_run_id"],

                parent_operation_run_id=parent_operation_run_id,

                except_run_id=None,

                reason="cancelled because parent workflow run cancellation was requested",

            )

            for task in tasks:

                if not task.done():
                    task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            raise


        except asyncio.CancelledError:

            log.warningx(
                "ForEach operation asyncio task cancelled",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
                parent_operation_run_id=parent_operation_run_id,
            )

            self.run_repository.request_cancel_for_each_sibling_runs(

                parent_run_id=context["workflow_run_id"],

                parent_operation_run_id=parent_operation_run_id,

                except_run_id=None,

                reason="cancelled because for_each operation task was cancelled",

            )

            for task in tasks:

                if not task.done():
                    task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            raise


        except Exception as exc:

            log.warningx(
                "ForEach operation mislukt",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
                parent_operation_run_id=parent_operation_run_id,
                failure_strategy=failure_strategy,
                error=str(exc),
            )

            if failure_strategy == "stop":
                self.run_repository.request_cancel_for_each_sibling_runs(

                    parent_run_id=context["workflow_run_id"],

                    parent_operation_run_id=parent_operation_run_id,

                    except_run_id=None,

                    reason=f"cancelled because for_each operation failed: {exc}",

                )

            for task in tasks:

                if not task.done():
                    task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            raise

        results = sorted(results, key=lambda r: r["index"])

        successes = [r for r in results if r.get("status") == "success"]
        failures = [r for r in results if r.get("status") == "failed"]

        final_status = "success" if not failures else "partial_success"
        self._append_trace_event(for_each_trace, "workflow_for_each_completed", {
            "operation_id": operation.id,
            "items_count": len(items),
            "success_count": len(successes),
            "failed_count": len(failures),
            "status": final_status,
        })

        log.infox(
            "ForEach operation afgerond",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            parent_operation_run_id=parent_operation_run_id,
            items_total=len(items),
            items_success=len(successes),
            items_failed=len(failures),
            final_status=final_status,
        )

        compact_results = [
            {
                "index": r["index"],
                "item_preview": r.get("item_preview"),
                "status": r["status"],
                "child_workflow_run_id": r.get("child_workflow_run_id"),
                "downstream_handoff": r.get("downstream_handoff"),
                "error": r.get("error"),
            }
            for r in results
        ]
        return {
            "mode": "for_each",
            "status": final_status,
            "iterable_name": iterable_name,
            "items_total": len(items),
            "items_count": len(items),
            "items_success": len(successes),
            "success_count": len(successes),
            "items_failed": len(failures),
            "failed_count": len(failures),
            "results_key": result_key,
            "results": compact_results,
            "trace": for_each_trace,
            "downstream_handoff": {
                "summary": f"Processed {len(successes)} of {len(items)} items.",
                "facts": {
                    "items_total": len(items),
                    "items_success": len(successes),
                    "items_failed": len(failures),
                },
                "artifacts": [],
                "iterables": {
                    result_key: compact_results
                },
            },
        }

    def _summarize_operation_output(self, out: Any) -> str:
        """A short, readable outcome for one completed workflow step, for the run transcript."""
        if not isinstance(out, dict):
            return self._compact_preview(out, 600)
        if out.get("error"):
            return f"FAILED: {str(out.get('error'))[:600]}"
        dh = out.get("downstream_handoff")
        if isinstance(dh, dict) and dh.get("summary"):
            return str(dh["summary"])[:1200]
        if out.get("answer"):
            return str(out["answer"])[:1200]
        if out.get("mode") == "set_variable" and isinstance(out.get("variables_set"), dict):
            return "Set variables: " + ", ".join(sorted((out.get("variables_set") or {}).keys()))
        return self._compact_preview(out, 800)

    def _workflow_run_transcript(self, context: Dict[str, Any]) -> list[dict]:
        """Replay the COMPLETED steps of this workflow run (in definition order) + their
        outcomes as a conversation, so each operation's agent remembers the whole run —
        not just data passed via explicit handoff/input-mapping edges."""
        outputs = context.get("operation_outputs") or {}
        statuses = context.get("operation_statuses") or {}
        messages: list[dict] = []
        for op in (context.get("operations") or []):
            op_id = getattr(op, "id", None)
            if op_id not in outputs:
                continue
            name = getattr(op, "name", None) or f"operation {op_id}"
            op_type = getattr(op, "operation_type", None) or getattr(op, "type", None) or ""
            status = statuses.get(op_id, "success")
            header = f"[Workflow step: {name}" + (f" ({op_type})" if op_type else "") + f" — {status}]"
            messages.append({"role": "assistant", "content": f"{header}\n{self._summarize_operation_output(outputs[op_id])}"})
        return messages

    async def _execute_assistant_operation(
            self,
            operation: Any,
            input_payload: Dict[str, Any],
            context: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = operation.config or {}
        question = config.get("question") or config.get("prompt") or "Execute this workflow operation."

        log.infox(
            "Assistant workflow operation uitvoeren gestart",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_id=getattr(operation, "id", None),
            assistant_id=getattr(operation, "operation_ref_id", None),
            operation_config_keys=list(config.keys()) if isinstance(config, dict) else None,
            question_length=len(question or ""),
        )

        skill_names = config.get("skill_names") or []

        if not isinstance(skill_names, list):
            log.errorx(
                "Assistant workflow operation heeft ongeldige skill_names",
                workflow_run_id=context.get("workflow_run_id"),
                workflow_id=context.get("workflow_id"),
                operation_id=getattr(operation, "id", None),
                assistant_id=getattr(operation, "operation_ref_id", None),
                skill_names_type=type(skill_names).__name__,
                skill_names=skill_names,
            )
            raise RuntimeError(
                f"Workflow operation {operation.id} has invalid config.skill_names. "
                "Expected an array of skill names."
            )

        skill_names = [
            str(name).strip()
            for name in skill_names
            if str(name).strip()
        ]

        # No skill required: the always-on builtin tools (documents, files, shell, PDF) are
        # available on every operation, so an activity can run with zero skills. A skill is
        # only needed for a domain capability beyond the builtins.
        if not skill_names:
            log.infox(
                "Assistant workflow operation zonder skills — draait op builtin tools",
                workflow_run_id=context.get("workflow_run_id"),
                workflow_id=context.get("workflow_id"),
                operation_id=getattr(operation, "id", None),
            )

        log.infox(
            "Assistant workflow operation skills bepaald",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_id=getattr(operation, "id", None),
            assistant_id=getattr(operation, "operation_ref_id", None),
            selected_skill_names=skill_names,
        )

        if self.prompt_variable_resolver:
            log.debugx(
                "Prompt variables resolven voor assistant workflow operation",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
                question_length_before=len(question or ""),
            )
            question = self.prompt_variable_resolver.resolve(question)
            log.debugx(
                "Prompt variables resolved voor assistant workflow operation",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
                question_length_after=len(question or ""),
            )

        run_payload = dict(input_payload)
        run_payload["_selected_skill_names"] = skill_names
        run_payload["_workflow_background"] = True
        # Run-level memory: feed the prior steps + outcomes of THIS run as conversation
        # state so the agent remembers the whole workflow (the planner prompt renders
        # _active_conversation_state). Provider-equal — it's client-side transcript.
        run_transcript = self._workflow_run_transcript(context)
        if run_transcript:
            run_payload["_active_conversation_state"] = {
                "recent_messages": run_transcript,
                "instruction": (
                    "These are the PREVIOUS steps of this workflow run and their outcomes. "
                    "Treat them as established context and build on them — do not redo work "
                    "already done by an earlier step."
                ),
            }
        run_payload["_cancellation_check"] = lambda: self._raise_if_cancel_requested(context["workflow_run_id"])
        run_payload["_workflow_execution_policy"] = (
            config.get("execution_policy") if isinstance(config.get("execution_policy"), dict) else config
        )

        log.infox(
            "Assistant runner aanroepen vanuit workflow operation",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_id=getattr(operation, "id", None),
            assistant_id=getattr(operation, "operation_ref_id", None),
            selected_skill_names=skill_names,
            run_payload_keys=list(run_payload.keys()),
            model=config.get("model"),
        )

        # Optional shared thread: config.thread (a literal or a reference like
        # ${workflow_variables.thread}, typically set by a new_thread operation).
        # When present, this activity runs IN that thread so several activities
        # share one conversation. When absent, the runner creates its own
        # per-operation thread and does NOT reuse it.
        shared_session_id = None
        _thread_cfg = config.get("thread")
        if _thread_cfg not in (None, ""):
            deps = list(getattr(operation, "depends_on", None) or [])
            previous_id = int(deps[-1]) if deps else None
            resolved_thread = self._resolve_template_value(_thread_cfg, context, previous_operation_id=previous_id, allow_null=True)
            if isinstance(resolved_thread, str) and resolved_thread.strip():
                shared_session_id = resolved_thread.strip()

        # Per-operation execution engine. Default "orchestrator" = the ND3X agent
        # loop with ND3X tools (below). "claude_code" runs the step as one
        # autonomous Claude Code CLI task, fully outside the orchestrator; it
        # returns a pipeline-shaped result so all handling below is unchanged.
        _exec_cfg = config.get("execution") if isinstance(config.get("execution"), dict) else {}
        _engine = str(_exec_cfg.get("engine") or "orchestrator").strip().lower()
        if _engine == "claude_code" and self.claude_code_runner is not None:
            log.infox(
                "Assistant workflow operation draait op de claude_code engine",
                workflow_run_id=context.get("workflow_run_id"),
                operation_id=getattr(operation, "id", None),
            )
            result = await self.claude_code_runner.run(
                question=question,
                operation_config=config,
                run_transcript=self._workflow_run_transcript(context),
                model=config.get("model"),
                workflow_run_id=context["workflow_run_id"],
                operation_id=operation.id,
            )
        elif _engine == "claude_code":
            raise RuntimeError(
                "Operation is set to the 'claude_code' engine, but no Claude Code "
                "provider is available. Add and enable it under AI Models, or "
                "switch this step back to the orchestrator engine.")
        else:
            result = await self.assistant_runner.run(
                assistant_id=operation.operation_ref_id,
                question=question,
                payload=run_payload,
                workflow_run_id=context["workflow_run_id"],
                operation_id=operation.id,
                model=config.get("model"),
                session_id=shared_session_id,
            )

        log.infox(
            "Assistant workflow operation runner resultaat ontvangen",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_id=getattr(operation, "id", None),
            assistant_id=getattr(operation, "operation_ref_id", None),
            selected_skill_names=skill_names,
            result_mode=result.get("mode") if isinstance(result, dict) else None,
            answer_length=len((result.get("answer") or "") if isinstance(result, dict) else ""),
            has_downstream_handoff=bool(result.get("downstream_handoff")) if isinstance(result, dict) else None,
        )

        if result.get("mode") == "workflow_waiting":
            pending_state = dict(result.get("pending_action") or {})
            pending_state.update({
                "operation_id": operation.id,
                "operation_name": getattr(operation, "name", None),
                "assistant_id": operation.operation_ref_id,
                "skill_names": skill_names,
            })
            pending_state = self._prepare_waiting_pending_state(operation, pending_state, result.setdefault("trace", []))
            if pending_state.get("type") == "workflow_user_input":
                result["status"] = "waiting_for_user_input"
                self._append_trace_event(result.setdefault("trace", []), "workflow_waiting_for_user_input", {
                    "operation_id": operation.id,
                    "question": pending_state.get("question"),
                })
            elif pending_state.get("type") == "workflow_tool_approval":
                result["status"] = "waiting_for_approval"
                self._append_trace_event(result.setdefault("trace", []), "workflow_waiting_for_approval", {
                    "operation_id": operation.id,
                    "tool": pending_state.get("tool"),
                    "denial_reason": (pending_state.get("policy_decision") or {}).get("denial_reason"),
                })
            result["pending_action"] = pending_state
            result["pending_state"] = pending_state
            return result

        if result.get("mode") == "ask_user":
            raise RuntimeError(f"needs_user_input: {result.get('answer') or 'Assistant operation requested user input.'}")

        if result.get("mode") in {"error", "confirm_action"}:
            log.warningx(
                "Assistant workflow operation gestopt door result mode",
                workflow_run_id=context.get("workflow_run_id"),
                workflow_id=context.get("workflow_id"),
                operation_id=getattr(operation, "id", None),
                assistant_id=getattr(operation, "operation_ref_id", None),
                selected_skill_names=skill_names,
                result_mode=result.get("mode"),
                answer=result.get("answer"),
            )
            raise RuntimeError(
                result.get("answer")
                or f"Assistant operation stopped with mode={result.get('mode')}"
            )

        # The agent can finish (mode=final) yet self-report that it did NOT complete the
        # task via downstream_handoff.status. Only "success" counts as success: both
        # "failed" and "partial" (the task wasn't fully done) fail the operation instead of
        # silently passing, which also lets on_failure follow-ups fire.
        handoff = result.get("downstream_handoff")
        handoff_status = (handoff.get("status") if isinstance(handoff, dict) else None) or ""
        if handoff_status.strip().lower() in {"failed", "partial"}:
            log.warningx(
                "Assistant workflow operation zelf-gerapporteerd als niet-voltooid via downstream_handoff",
                workflow_run_id=context.get("workflow_run_id"),
                workflow_id=context.get("workflow_id"),
                operation_id=getattr(operation, "id", None),
                handoff_status=handoff_status,
            )
            raise RuntimeError(
                (result.get("answer") or "").strip()
                or (handoff.get("summary") if isinstance(handoff, dict) else None)
                or f"Assistant operation not completed (downstream_handoff.status={handoff_status})."
            )

        output = {
            "mode": result.get("mode"),
            "answer": result.get("answer"),
            "selected_skill_names": skill_names,
            "downstream_handoff": result.get("downstream_handoff"),
            "tool_calls": result.get("tool_calls"),
            "tool_results": result.get("tool_results"),
            "docs": result.get("docs"),
            "trace": result.get("trace"),
            "terminal_state": result.get("terminal_state"),
            "budget_reason": result.get("budget_reason"),
            "last_error": result.get("last_error"),
        }

        log.infox(
            "Assistant workflow operation uitvoeren afgerond",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_id=getattr(operation, "id", None),
            assistant_id=getattr(operation, "operation_ref_id", None),
            selected_skill_names=skill_names,
            output_mode=output.get("mode"),
            tool_call_count=len(output.get("tool_calls") or []),
            tool_result_count=len(output.get("tool_results") or []),
            doc_count=len(output.get("docs") or []),
            trace_count=len(output.get("trace") or []),
            has_downstream_handoff=bool(output.get("downstream_handoff")),
        )

        return output

    async def _execute_sub_workflow_operation(
            self,
            operation: Any,
            input_payload: Dict[str, Any],
            context: Dict[str, Any],
    ) -> Dict[str, Any]:
        log.infox(
            "Sub-workflow operation uitvoeren gestart",
            workflow_run_id=context.get("workflow_run_id"),
            workflow_id=context.get("workflow_id"),
            operation_id=getattr(operation, "id", None),
            child_workflow_id=getattr(operation, "operation_ref_id", None),
            input_payload_keys=list(input_payload.keys()) if isinstance(input_payload, dict) else None,
        )
        child_run = self.run_repository.create_run(
            workflow_id=operation.operation_ref_id,
            trigger_type="sub_workflow",
            input_payload=input_payload,
            parent_run_id=context["workflow_run_id"],
        )

        log.infox(
            "Sub-workflow child run aangemaakt",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            child_workflow_id=getattr(operation, "operation_ref_id", None),
            child_workflow_run_id=getattr(child_run, "id", None),
        )

        result = await self.execute_run(child_run.id)

        log.infox(
            "Sub-workflow operation uitvoeren afgerond",
            workflow_run_id=context.get("workflow_run_id"),
            operation_id=getattr(operation, "id", None),
            child_workflow_run_id=getattr(child_run, "id", None),
            result_status=result.get("status") if isinstance(result, dict) else None,
        )
        return result