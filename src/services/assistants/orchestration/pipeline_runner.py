from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from component.config import settings
from component.logging import get_logger
from services.assistants.orchestration.orchestrator_utils import _plan_has_tool
from services.assistants.orchestration.pending import build_mutation_confirmation_prompt
from services.assistants.orchestration.guarded_tools import (
    build_tool_confirmation_pending_action,
    guard_trace_data,
    is_guarded_tool,
    tool_call_hash,
    validate_guarded_tool_call,
)
from services.assistants.orchestration.guarded_tool_policy import evaluate_workflow_guarded_tool_policy
from services.assistants.orchestration.tool_execution import _tool_call_name
from services.assistants.orchestration.runtime_skill_injection import resolve_effective_selected_skills
from services.assistants.orchestration.documents import (
    build_docs_for_tool_calls,
    format_return_file_answer,
)
from services.assistants.orchestration.formatting import (
    _preview,
    _compact_tool_call,
    _compact_tool_result,
    _compact_doc,
    _extract_final_answer_if_json,
    _looks_like_planner_json,
    _fallback_no_evidence_message,
    _extract_downstream_handoff,
    _assistant_name,
    build_result,
    _coerce_plan_to_dict,
)

log = get_logger(__name__)

ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]


def _selected_skill_files_root(assistant: Any, selected_skill_names: list[str]) -> Optional[str]:
    selected = set(selected_skill_names or [])
    for skill in getattr(getattr(assistant, "config", None), "skills", []) or []:
        if selected and getattr(skill, "name", None) not in selected:
            continue
        root = getattr(skill, "skill_files_root", None)
        if root:
            return str(root)
    return None


def _backfill_tool_ids(assistant: Any, tool_calls: list[dict]) -> list[dict]:
    """Resolve each tool call's ``tool_id`` from the tool *name* whenever the name
    maps to exactly one id in the manifest the model was shown.

    The agent is *required* to return ``tool_id`` (planner schema), but models — weak
    AND strong — sometimes drop it or hallucinate a wrong one (e.g.
    ``fabric_data_agent_query`` with id 2 instead of the real 317), which the tool
    guard would then block. Resolving by the (authoritative) tool name fills a missing
    id and corrects a present-but-wrong one. This is always safe: a correct id resolves
    to itself (no-op). Ambiguous names (same name, multiple ids) and internal tools
    (no DB id) are left untouched; a genuinely unresolvable dynamic call still hard-stops
    downstream. Names are indexed from the loaded skills' tools plus the always-on
    builtin tools.
    """
    cfg = getattr(assistant, "config", None)

    def _index(tool) -> None:
        tid = getattr(tool, "id", None)
        tname = (getattr(tool, "name", "") or "").strip()
        if tname and isinstance(tid, int) and not isinstance(tid, bool) and tid > 0:
            name_to_ids.setdefault(tname, set()).add(tid)

    name_to_ids: dict[str, set[int]] = {}
    for skill in getattr(cfg, "skills", []) or []:
        for tool in getattr(skill, "tools", []) or []:
            _index(tool)
    # Always-on builtin tools live on config.tools (not under a skill) since §2; include
    # them so a dropped id on a builtin call (common with weaker local models) is recovered.
    for tool in getattr(cfg, "tools", []) or []:
        _index(tool)

    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        ids = name_to_ids.get((tc.get("tool") or "").strip())
        # Unambiguous name → use its id (fills a missing one, corrects a wrong one;
        # a correct id resolves to itself). Ambiguous/unknown names are left as-is.
        if ids and len(ids) == 1:
            tc["tool_id"] = next(iter(ids))
    return tool_calls


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 — serialisation must never break the loop
        return str(value)


def _build_transcript_messages(
    assistant: Any, question: str, payload: Dict[str, Any], plan_prompt: str
) -> List[Dict[str, str]]:
    """Build a structured conversation for the transcript path (every provider when the
    OpenAI server-side session is off).

    The anchor is the planner prompt WITHOUT accumulators (rules + manifest + schema +
    question); each accumulated hop is replayed as a real assistant tool-call turn followed
    by a user observation turn. This append-only, byte-stable prefix caches well and makes
    multi-hop behaviour identical across OpenAI / Anthropic / local — instead of dumping the
    whole _acc_* blob into one prompt each hop.
    """
    acc_calls = payload.get("_acc_tool_calls") or []
    acc_results = payload.get("_acc_tool_results") or []
    acc_docs = payload.get("_acc_docs") or []

    # First hop (nothing accumulated yet): the plan_prompt already IS the full anchor.
    if not (acc_calls or acc_results or acc_docs):
        return [{"role": "user", "content": plan_prompt}]

    anchor_payload = dict(payload)
    anchor_payload["_history_anchor"] = True
    anchor = assistant.prompt(question=question, **anchor_payload)

    messages: List[Dict[str, str]] = [{"role": "user", "content": anchor}]
    for i in range(max(len(acc_calls), len(acc_results))):
        if i < len(acc_calls):
            messages.append({
                "role": "assistant",
                "content": _json({"action": "tool_calls", "tool_calls": [acc_calls[i]]}),
            })
        if i < len(acc_results):
            messages.append({"role": "user", "content": "Tool result:\n" + _json(acc_results[i])})
    if acc_docs:
        messages.append({"role": "user", "content": "Documents retrieved:\n" + _json(acc_docs)})
    messages.append({
        "role": "user",
        "content": "Continue. Decide the next action as a single JSON object matching the schema.",
    })
    return messages


def _extract_partial_final_answer(text: str) -> Optional[str]:
    """Pull the current value of the JSON string field "final_answer" out of a partial
    planner-JSON stream, so the answer can be shown growing while it's generated. Returns
    None until the field's opening quote is seen (and for null/non-string)."""
    key = '"final_answer"'
    i = text.find(key)
    if i < 0:
        return None
    j = text.find(":", i + len(key))
    if j < 0:
        return None
    k = j + 1
    while k < len(text) and text[k] in " \t\r\n":
        k += 1
    if k >= len(text) or text[k] != '"':
        return None  # null / not a string yet
    k += 1
    out: list[str] = []
    _esc = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
    while k < len(text):
        c = text[k]
        if c == "\\":
            if k + 1 >= len(text):
                break  # escape split across chunks; stop here, next tick continues
            out.append(_esc.get(text[k + 1], text[k + 1]))
            k += 2
            continue
        if c == '"':
            break  # closing quote → value complete
        out.append(c)
        k += 1
    return "".join(out)


def _ask_user_should_fail(question: str, *, is_workflow: bool) -> bool:
    """Decide whether an ``ask_user`` must fail the turn instead of pausing.

    - A workflow run NEVER asks the user — it is fully autonomous, so any ask_user fails
      the operation (with the question as the reason).
    - Outside a workflow, an empty/blank question is a dead-end and fails; a real question
      pauses for the user as before.
    """
    if is_workflow:
        return True
    return not (question or "").strip()


def _skills_providing_tools(assistant: Any, tool_names: list[str]) -> str:
    """Map blocked tool names → the skill(s) that provide them, for a recovery hint
    (e.g. "system__shell_exec → runtime_cli_automation"). Empty string if unknown."""
    wanted = {(t or "").strip() for t in (tool_names or []) if (t or "").strip()}
    if not wanted:
        return ""
    pairs: list[str] = []
    try:
        for skill in getattr(assistant, "skills", None) or []:
            sname = getattr(skill, "name", None)
            if not sname:
                continue
            for tool in getattr(skill, "tools", None) or []:
                tname = (getattr(tool, "name", "") or "").strip()
                if tname in wanted:
                    pairs.append(f"{tname} → {sname}")
    except Exception:  # noqa: BLE001 — hint is best-effort
        return ""
    # de-dup preserving order
    seen: set[str] = set()
    uniq = [p for p in pairs if not (p in seen or seen.add(p))]
    return "; ".join(uniq)


def _resolve_skill_choices(
    assistant: Any,
    requested_names: list,
    requested_tool_names: list,
) -> tuple[list[str], dict]:
    """Tolerant resolution of the planner's ``selected_skill_names`` against the
    skill catalog — used ONLY when extra guidance is enabled for the model. It
    forgives two common small/local-model mistakes:
      1. a near-miss in casing/whitespace, and
      2. naming a TOOL (e.g. ``fabric_data_agent_query``) instead of the SKILL
         that provides it (e.g. ``fabric_operations_management``).
    It also recovers skills implied by any tool names the planner referenced in
    ``tool_calls`` (a malformed select_skills plan usually still names the tool it
    wants). Returns ``(chosen_skill_names, debug)`` where debug maps each input to
    its outcome (for tracing). Only user/domain skills (enabled, non-system,
    non-runtime) are eligible — the same set as the selection catalog.
    """
    cfg = getattr(assistant, "config", None)
    skills = [
        s for s in (getattr(cfg, "skills", None) or [])
        if getattr(s, "is_enabled", True)
        and not getattr(s, "is_system", False)
        and not getattr(s, "is_runtime", False)
    ]
    by_name: dict[str, str] = {}        # lower skill name -> real skill name
    tool_to_skill: dict[str, str] = {}  # lower tool name  -> real skill name
    for s in skills:
        sname = (getattr(s, "name", "") or "").strip()
        if not sname:
            continue
        by_name[sname.lower()] = sname
        for tool in (getattr(s, "tools", None) or []):
            tname = (getattr(tool, "name", "") or "").strip().lower()
            if tname:
                tool_to_skill.setdefault(tname, sname)

    chosen: list[str] = []
    debug: dict[str, str] = {}

    def _add(name: str) -> None:
        if name and name not in chosen:
            chosen.append(name)

    # 1) explicit skill picks: exact → case-insensitive skill → tool-name → skill.
    for raw in (requested_names or []):
        key = str(raw).strip()
        if not key:
            continue
        low = key.lower()
        if low in by_name:
            _add(by_name[low]); debug[key] = by_name[low]
        elif low in tool_to_skill:
            _add(tool_to_skill[low]); debug[key] = f"tool→{tool_to_skill[low]}"
        else:
            debug[key] = "dropped"

    # 2) skills implied by referenced tool_calls (recovers a tool-named-as-skill plan).
    for raw in (requested_tool_names or []):
        low = str(raw).strip().lower()
        if low and low in tool_to_skill and tool_to_skill[low] not in chosen:
            _add(tool_to_skill[low])
            debug[f"tool_call:{raw}"] = f"→{tool_to_skill[low]}"

    return chosen, debug


def _flow_instruction(is_workflow: bool) -> str:
    """The per-flow agent instruction block (chat vs workflow), appended to the editable
    base instruction so the system prompt is Base + (Chat | Workflow). Read per turn so
    UI edits take effect without a restart."""
    from pathlib import Path
    name = "agent.instruction.workflow.md" if is_workflow else "agent.instruction.chat.md"
    try:
        path = Path(__file__).resolve().parents[1] / "runtime" / "system_specs" / name
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


TERMINAL_COMPLETED = "completed"
TERMINAL_WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
TERMINAL_WAITING_FOR_USER = "waiting_for_user"
TERMINAL_POLICY_DENIED = "policy_denied"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_FAILED = "failed"
TERMINAL_BUDGET_EXCEEDED = "budget_exceeded"


def _agent_loop_budgets(is_workflow: bool, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    if is_workflow:
        base = {
            "max_iterations": int(getattr(settings, "WORKFLOW_AGENT_MAX_ITERATIONS_PER_OPERATION", 12)),
            "max_tool_calls": int(getattr(settings, "WORKFLOW_AGENT_MAX_TOOL_CALLS_PER_OPERATION", 20)),
            "max_same_error_repeats": int(getattr(settings, "WORKFLOW_AGENT_MAX_SAME_ERROR_REPEATS", 2)),
            "max_wall_clock_seconds": int(getattr(settings, "WORKFLOW_AGENT_MAX_WALL_CLOCK_SECONDS", 600)),
        }
    else:
        base = {
            "max_iterations": int(getattr(settings, "CHAT_AGENT_MAX_ITERATIONS_PER_STEP", 8)),
            "max_tool_calls": int(getattr(settings, "CHAT_AGENT_MAX_TOOL_CALLS_PER_STEP", 12)),
            "max_same_error_repeats": int(getattr(settings, "CHAT_AGENT_MAX_SAME_ERROR_REPEATS", 2)),
            "max_wall_clock_seconds": int(getattr(settings, "CHAT_AGENT_MAX_WALL_CLOCK_SECONDS", 300)),
        }
    # Per-operation manual overrides (e.g. a workflow op that legitimately runs long).
    # max_wall_clock_seconds accepts 0 = no time limit; the others must stay >= 1 so the
    # loop always has a hard iteration/tool cap even when time is unbounded.
    if overrides:
        wc = overrides.get("max_wall_clock_seconds")
        if isinstance(wc, int) and not isinstance(wc, bool) and wc >= 0:
            base["max_wall_clock_seconds"] = wc
        for k in ("max_iterations", "max_tool_calls", "max_same_error_repeats"):
            v = overrides.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
                base[k] = v
    # Absolute ceiling for workflow ops: bound a "no limit" (0) or an override set
    # above the hard cap, so a wandering agent can never run forever. 0 disables it.
    if is_workflow:
        hard = int(getattr(settings, "WORKFLOW_AGENT_MAX_WALL_CLOCK_HARD_SECONDS", 1800))
        if hard > 0:
            wc = base["max_wall_clock_seconds"]
            if wc <= 0 or wc > hard:
                base["max_wall_clock_seconds"] = hard
    return base


def _tool_result_failed(result: Any) -> bool:
    return isinstance(result, dict) and str(result.get("status") or "").lower() in {"failed", "error"}


def _tool_result_recoverable(result: Any) -> bool:
    if not _tool_result_failed(result):
        return False
    return bool(result.get("recoverable", True))


def _tool_error_fingerprint(result: Dict[str, Any]) -> str:
    message = str(result.get("message") or result.get("summary") or result.get("error") or "")[:240].strip().lower()
    return "|".join([
        str(result.get("tool") or ""),
        str(result.get("status") or ""),
        str(result.get("error_type") or ""),
        message,
        str(result.get("exit_code") if result.get("exit_code") is not None else ""),
    ])


def _summarize_tool_error(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tool": result.get("tool"),
        "status": result.get("status"),
        "error_type": result.get("error_type"),
        "message": str(result.get("message") or result.get("summary") or result.get("error") or "")[:500],
        "exit_code": result.get("exit_code"),
        "recoverable": result.get("recoverable"),
    }


def _verification_findings(final_text: str, payload: Dict[str, Any]) -> List[str]:
    """Deterministische self-check vóór completion. Lege lijst => geverifieerd.

    Bewust conservatief om valse positieven (en onnodige re-loops) te vermijden:
    alleen hoog-signaal problemen worden gevlagd.
    """
    findings: List[str] = []
    text = (final_text or "").strip()
    if not text:
        findings.append("The final answer is empty.")
    else:
        try:
            if text == (_fallback_no_evidence_message() or "").strip():
                findings.append(
                    "The final answer is a no-evidence fallback message rather than a substantive answer."
                )
        except Exception:  # noqa: BLE001 — fallback-vergelijking mag nooit de check breken
            pass

    for r in payload.get("_acc_tool_results") or []:
        if not isinstance(r, dict):
            continue
        status = str(r.get("status") or "").lower()
        # Alleen expliciet onherstelbare fouten tellen; herstelbare fouten zijn
        # mogelijk al door de assistant omzeild.
        if status in {"error", "failed"} and r.get("recoverable") is False:
            findings.append(
                f"Tool '{r.get('tool') or 'unknown'}' failed unrecoverably; the answer may be incomplete."
            )
    return findings


def _agent_loop_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "agent_loop_started": bool(payload.get("_agent_loop_started")),
        "agent_loop_started_at": payload.get("_agent_loop_started_at"),
        "iteration_count": int(payload.get("_agent_loop_iterations") or 0),
        "tool_call_count": int(payload.get("_agent_loop_tool_calls") or 0),
        "error_repeats": dict(payload.get("_agent_loop_error_repeats") or {}),
        "last_tool_calls": payload.get("_last_tool_calls") or [],
        "last_tool_results": payload.get("_last_tool_results") or [],
        "last_docs": payload.get("_last_docs") or [],
        "acc_tool_calls": payload.get("_acc_tool_calls") or [],
        "acc_tool_results": payload.get("_acc_tool_results") or [],
        "acc_docs": payload.get("_acc_docs") or [],
        "remaining_eval_hops": payload.get("_remaining_eval_hops"),
        "remaining_tool_budget": payload.get("_remaining_tool_budget"),
        "text_search_used": bool(payload.get("_text_search_used", False)),
    }


def _payload_without_callbacks(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (payload or {}).items() if k != "_cancellation_check"}


class AssistantPipelineRunner:
    """Reusable assistant execution engine.

    This is intentionally independent from AssistantOrchestrator.run(), so it can be
    used by chat routing and by background workflow execution.
    """

    def __init__(
        self,
        *,
        openai_service,
        runtime_resolver,
        tool_runner,
        tool_guard,
        assistant_output_store_service,
        trace_fn: Callable[..., None],
        pending_store=None,
        max_tool_calls_per_turn: Optional[int] = None,
        require_mutation_confirmation: bool = True,
    ):
        log.infox(
            "AssistantPipelineRunner initialiseren",
            has_openai_service=openai_service is not None,
            has_runtime_resolver=runtime_resolver is not None,
            has_tool_runner=tool_runner is not None,
            has_tool_guard=tool_guard is not None,
            has_assistant_output_store_service=assistant_output_store_service is not None,
            has_trace_fn=trace_fn is not None,
            has_pending_store=pending_store is not None,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
            settings_max_tool_steps=getattr(settings, "MAX_TOOL_STEPS", None),
            require_mutation_confirmation=require_mutation_confirmation,
        )
        self.openai = openai_service
        self.runtime = runtime_resolver
        self.tool_runner = tool_runner
        self.tool_guard = tool_guard
        self.assistant_output_store = assistant_output_store_service
        self.trace_fn = trace_fn
        self.pending = pending_store
        self.max_tool_calls_per_turn = max_tool_calls_per_turn or settings.MAX_TOOL_STEPS
        self.require_mutation_confirmation = require_mutation_confirmation
        log.infox(
            "AssistantPipelineRunner geïnitialiseerd",
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
            require_mutation_confirmation=self.require_mutation_confirmation,
            has_pending_store=self.pending is not None,
        )

    def _store_large_handoff_content_if_needed(
        self,
        *,
        handoff: Dict[str, Any],
        session_id: Optional[str],
        turn_id: int,
        assistant_name: str,
        trace: List[dict],
    ) -> Dict[str, Any]:
        log.debugx(
            "Pipeline handoff content opslag check gestart",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            handoff_type=type(handoff).__name__,
            handoff_keys=list(handoff.keys()) if isinstance(handoff, dict) else None,
            trace_count=len(trace or []),
        )
        if not isinstance(handoff, dict):
            log.debugx(
                "Pipeline handoff content opslag overgeslagen: handoff is geen dict",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
            )
            return handoff

        full_answer = (handoff.get("full_answer") or "").strip()
        if not full_answer:
            log.debugx(
                "Pipeline handoff content opslag overgeslagen: full_answer ontbreekt",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
            )
            return handoff

        inline_threshold = 6000
        if len(full_answer) <= inline_threshold:
            log.debugx(
                "Pipeline handoff content blijft inline",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                full_answer_length=len(full_answer),
                inline_threshold=inline_threshold,
            )
            return handoff

        log.infox(
            "Pipeline grote handoff content opslaan gestart",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            full_answer_length=len(full_answer),
            inline_threshold=inline_threshold,
            chunk_size=6000,
        )
        ref = self.assistant_output_store.store_text(
            text=full_answer,
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            kind="assistant_output",
            chunk_size=6000,
        )

        new_handoff = dict(handoff)
        new_handoff["full_answer"] = None
        new_handoff["output_ref"] = ref

        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="handoff_output_stored",
            summary=f"Stored large handoff output in {ref.get('chunk_count', 0)} chunks",
            data={"assistant": assistant_name, "output_ref": ref},
        )

        log.infox(
            "Pipeline grote handoff content opgeslagen",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            output_id=ref.get("id") if isinstance(ref, dict) else None,
            chunk_count=ref.get("chunk_count") if isinstance(ref, dict) else None,
        )
        return new_handoff



    @staticmethod
    def _resolve_extra_guidance(model: Optional[str], payload: Dict[str, Any]) -> bool:
        """Effective 'extra guidance' flag for this turn: a per-session override
        from the Chat tile (forces on) OR the planner model's per-model toggle.
        Best-effort — never breaks the turn."""
        if bool(payload.get("_extra_guidance_session")):
            return True
        try:
            from db.database import SessionLocal
            from services.providers.registry_service import ProviderRegistryService
            db = SessionLocal()
            try:
                reg = ProviderRegistryService(db)
                mid = (model or "").strip()
                if not mid:
                    r = reg.resolve_slot("chat.planner")
                    mid = getattr(r, "model_id", "") if r else ""
                return reg.model_needs_extra_guidance(mid)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _resolve_light_mode(model: Optional[str], payload: Dict[str, Any]) -> bool:
        """Effective planner 'light mode' for this turn: a per-session override
        (payload), else the model's per-model prompt_mode (AI Models → Routing;
        unset = auto → light when the model is local). Small/local models are
        prefill-bound, so the compact prompt is what makes them responsive.
        Best-effort — never breaks the turn."""
        if "_light_mode_session" in payload:
            return bool(payload.get("_light_mode_session"))
        try:
            from db.database import SessionLocal
            from services.providers.registry_service import ProviderRegistryService
            db = SessionLocal()
            try:
                reg = ProviderRegistryService(db)
                mid = (model or "").strip()
                if not mid:
                    r = reg.resolve_slot("chat.planner")
                    mid = getattr(r, "model_id", "") if r else ""
                return reg.model_prompt_light(mid)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            return False

    async def _run_assistant_pipeline(
        self,
        *,
        assistant,
        question: str,
        model: Optional[str] = None,
        payload: Dict[str, Any],
        session_id: Optional[str],
        turn_id: int,
        trace: Optional[List[dict]] = None,
        progress_cb: ProgressCallback = None,
    ) -> Dict[str, Any]:
        log.infox(
            "Assistant pipeline run gestart",
            session_id=session_id,
            turn_id=turn_id,
            assistant_type=type(assistant).__name__,
            question_length=len(question or ""),
            model=model,
            payload_keys=list(payload.keys()) if isinstance(payload, dict) else None,
            trace_count=len(trace or []),
            has_progress_cb=progress_cb is not None,
        )
        trace = trace or []
        payload = dict(payload or {})
        assistant_name = _assistant_name(assistant)
        # Resolve the "extra guidance" flag once per turn (carried across hops via
        # payload): a per-session override from the Chat tile, OR the planner
        # model's per-model toggle (AI Models → Routing). Drives whether the
        # for-dummies guidance block is prepended in build_planner_prompt.
        if "_extra_guidance" not in payload:
            payload["_extra_guidance"] = self._resolve_extra_guidance(model, payload)
        # Light mode (compact planner prompt for small/local models) — resolved once
        # per turn and carried across hops via the payload, like _extra_guidance.
        if "_light_mode" not in payload:
            payload["_light_mode"] = self._resolve_light_mode(model, payload)
        # Koppel achtergrondtaken (task__create) aan deze run-thread. ContextVars
        # zijn per asyncio-task, dus parallelle runs blijven gescheiden.
        try:
            from services.builtin.tools.background_tasks import current_run_thread
            current_run_thread.set(session_id)
        except Exception:  # noqa: BLE001 — background tools optioneel
            pass
        is_workflow_background = bool(payload.get("_workflow_background", False))
        # Effective system instruction = editable base + the per-flow (chat/workflow) block
        # + the always-available builtin tool manifest (STATIC across the loop, so it lives
        # in the per-request instructions and is sent ONCE — not re-embedded in every hop's
        # user turn / accumulated in the OpenAI Responses session; on Anthropic it's part of
        # the cached system prefix).
        _base_instructions = getattr(assistant, "instructions", "") or ""
        _flow_block = _flow_instruction(is_workflow_background)
        effective_instructions = f"{_base_instructions}\n\n{_flow_block}".strip() if _flow_block else _base_instructions
        try:
            _always_on = assistant.prompt_builder.render_always_on_tools_block(
                assistant.config, compact=bool(payload.get("_light_mode"))
            )
            if _always_on:
                effective_instructions = f"{effective_instructions}\n\n{_always_on}".strip()
        except Exception as _e:  # noqa: BLE001 — never break planning on manifest render
            log.warningx("planner:always_on_instructions_failed", error=str(_e))
        # Plan mode (user toggled the "Plan" button → require_router_plan_approval): force a
        # plan proposal first for THIS request, overriding the default "only for risky work"
        # rule. The FE renders the propose_plan as an interactive plan card (approve / comment
        # / reject). Not applicable to autonomous workflow runs (they never wait on a user).
        if payload.get("require_router_plan_approval") and not is_workflow_background:
            effective_instructions = (
                f"{effective_instructions}\n\n"
                "## Plan mode is ON\n"
                "The user enabled Plan mode for this request. On your FIRST step, respond with "
                "`action='propose_plan'` — a short numbered plan in `final_answer` (one line per "
                "step) and a one-line summary in `reason` — and wait for approval before doing any "
                "work, even if the request looks straightforward. If you need a detail to plan well, "
                "you may first ask ONE brief clarifying question with `action='ask_user'`."
            ).strip()
        loop_budgets = _agent_loop_budgets(is_workflow_background, overrides=payload.get("_agent_budget_overrides"))
        # Goal mode (/goal): raise — never remove — the loop budgets so the agent
        # can keep working toward the goal. Hard caps stay (RUNTIME_TIMEOUT*,
        # same-error repeats unchanged so genuine loops still abort).
        if payload.get("_goal_mode") and not is_workflow_background:
            _gm = float(getattr(settings, "GOAL_MODE_BUDGET_MULTIPLIER", 3.0) or 3.0)
            for _k in ("max_iterations", "max_tool_calls", "max_wall_clock_seconds"):
                if int(loop_budgets.get(_k) or 0) > 0:
                    loop_budgets[_k] = int(loop_budgets[_k] * _gm)
        if not payload.get("_agent_loop_started"):
            payload["_agent_loop_started"] = True
            payload["_agent_loop_started_at"] = time.time()
            payload["_agent_loop_iterations"] = 0
            payload["_agent_loop_tool_calls"] = 0
            payload["_agent_loop_error_repeats"] = {}
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_loop_start",
                summary="Assistant agent loop started",
                data={"assistant": assistant_name, "workflow_background": is_workflow_background, "budgets": loop_budgets},
                progress_cb=progress_cb,
            )

        payload["_agent_loop_iterations"] = int(payload.get("_agent_loop_iterations") or 0) + 1
        loop_iteration = int(payload["_agent_loop_iterations"])
        elapsed_s = time.time() - float(payload.get("_agent_loop_started_at") or time.time())
        # max_wall_clock_seconds <= 0 means "no time limit" (iteration cap still applies).
        wall_limit = loop_budgets["max_wall_clock_seconds"]
        wall_exceeded = wall_limit > 0 and elapsed_s > wall_limit
        iter_exceeded = loop_iteration > loop_budgets["max_iterations"]
        if iter_exceeded or wall_exceeded:
            reason = "max_iterations" if iter_exceeded else "max_wall_clock_seconds"
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_loop_budget_exceeded",
                level="error",
                summary="Assistant agent loop budget exceeded",
                data={"assistant": assistant_name, "reason": reason, "iteration": loop_iteration, "elapsed_s": int(elapsed_s)},
                progress_cb=progress_cb,
            )
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_loop_terminal",
                level="error",
                summary="Assistant agent loop reached terminal state",
                data={"assistant": assistant_name, "terminal_state": TERMINAL_BUDGET_EXCEEDED, "reason": reason},
                progress_cb=progress_cb,
            )
            return build_result(
                mode="error",
                answer=f"Agent loop budget exceeded: {reason}.",
                trace=trace,
                thread_id=session_id,
                terminal_state=TERMINAL_BUDGET_EXCEEDED,
                budget_reason=reason,
            )

        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="agent_loop_iteration",
            summary="Assistant agent loop iteration",
            data={"assistant": assistant_name, "iteration": loop_iteration, "elapsed_s": int(elapsed_s)},
            progress_cb=progress_cb,
        )

        # Notificeer voltooide achtergrondtaken (task__create) zodat de assistant
        # ze in volgende iteraties kan oppakken — Claude-Code-stijl achtergrondwerk.
        try:
            from services.builtin.tools.background_tasks import drain_completed_background_tasks
            completed_bg = await drain_completed_background_tasks(session_id)
            for bg in completed_bg:
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="background_task_completed",
                    level="error" if str(bg.get("status")) == "error" else "info",
                    summary=f"Background task {bg.get('task_id')} {bg.get('status')}",
                    data={"assistant": assistant_name, **bg},
                    progress_cb=progress_cb,
                )
            if completed_bg:
                notifications = list(payload.get("_background_task_notifications") or [])
                notifications.extend(completed_bg)
                payload["_background_task_notifications"] = notifications
        except Exception as exc:  # noqa: BLE001 — notificatie mag de loop niet breken
            log.warningx("Drain achtergrondtaken mislukt", session_id=session_id, exception=exc)

        cancellation_check = payload.get("_cancellation_check")
        if callable(cancellation_check):
            try:
                cancellation_check()
            except Exception as exc:
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="agent_loop_cancelled",
                    level="warn",
                    summary="Assistant agent loop cancelled before planner call",
                    data={"assistant": assistant_name, "reason": str(exc)[:500]},
                    progress_cb=progress_cb,
                )
                return build_result(
                    mode="error",
                    answer=str(exc),
                    trace=trace,
                    thread_id=session_id,
                    terminal_state=TERMINAL_CANCELLED,
                )

        selected_skill_names = payload.get("_selected_skill_names") or []

        if not isinstance(selected_skill_names, list):
            return build_result(
                mode="error",
                answer="Invalid _selected_skill_names. Expected array of skill names.",
                trace=trace,
                thread_id=session_id,
            )

        selected_skill_names = [
            str(name).strip()
            for name in selected_skill_names
            if str(name).strip()
        ]
        selected_skill_names = resolve_effective_selected_skills(
            base_selected_skill_names=selected_skill_names,
            assistant_skills=assistant.config.skills or [],
            question=question,
            payload=payload or {},
        )

        # Drop skill names that aren't in the agent's catalog instead of hard-failing the
        # turn/operation. Builtin tools are always available, and a workflow may reference a
        # skill that was since retired (e.g. a builtin-wrapper skill like the old
        # text_document_management — its text__* tools are now always-on builtins). Keep the
        # valid skills + builtin tools and warn; only truly-unknown names are dropped.
        allowed_skills = self.tool_guard.allowed_skill_names_for(assistant.config)
        unknown_skills = [s for s in selected_skill_names if s not in allowed_skills]
        if unknown_skills:
            selected_skill_names = [s for s in selected_skill_names if s in allowed_skills]
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="skill_dropped",
                level="warn",
                summary=f"Ignored unknown skill(s): {unknown_skills} (using builtin tools + valid skills)",
                data={
                    "assistant": assistant_name,
                    "dropped": unknown_skills,
                    "kept": selected_skill_names,
                },
                progress_cb=progress_cb,
            )
        remaining_eval_hops = int(payload.get("_remaining_eval_hops", settings.EVALUATION_HOPS))
        # The budget the model is TOLD it has must match what the loop actually
        # ENFORCES (loop_budgets.max_tool_calls) — not the small global
        # settings.MAX_TOOL_STEPS (default 3). Otherwise the model thinks it has ~3
        # tool calls, "runs out" after a few, and gives up claiming "tool budget
        # exhausted" while the real budget (e.g. 20 for a workflow) is barely used.
        remaining_tool_budget = int(payload.get("_remaining_tool_budget", loop_budgets["max_tool_calls"]))
        has_used_evaluate = payload.get("_used_evaluate", False)
        has_failure_handled = payload.get("_failure_handled", False)

        log.debugx(
            "Assistant pipeline runtime counters bepaald",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            remaining_eval_hops=remaining_eval_hops,
            remaining_tool_budget=remaining_tool_budget,
            has_used_evaluate=has_used_evaluate,
            has_failure_handled=has_failure_handled,
            settings_evaluation_hops=getattr(settings, "EVALUATION_HOPS", None),
        )

        planner_payload = dict(payload or {})
        planner_payload["_cycle"] = int(turn_id)
        planner_payload["_conversation_id"] = session_id
        planner_payload["_selected_skill_names"] = selected_skill_names

        # §6 — flow + session strategy (computed before the prompt is built so the prompt
        # builder can adapt). Goal: keep the full reasoning context in the model's
        # server-side session and send only the NEW observation each hop, instead of
        # re-dumping all accumulated tool results/docs every continuation hop (O(n^2)).
        #
        # BUT this only works on a provider with a real server-side session: the OpenAI
        # Responses path chains previous_response_id automatically per stable session_id
        # when keep_context=True. Alternate providers (Anthropic, local/openai-compatible)
        # run the loop STATELESSLY via provider.chat() — no session — so for them we must
        # keep dumping the full _acc_* accumulators in the prompt, exactly as before.
        is_workflow_background = bool(payload.get("_workflow_background", False))
        is_planner_continuation = bool(payload.get("_used_evaluate", False))

        if is_workflow_background:
            # A workflow run is isolated per run+operation (resolved_session_id is unique),
            # so chaining stays within THIS run — no interactive-chat memory bleeds in.
            planner_session_id = session_id
            planner_role = f"workflow_planner:{assistant_name}"
        else:
            # Chat: first pass and every continuation share one session id so the model
            # retains the whole turn server-side; we then send only the new observation.
            planner_session_id = f"{session_id}:assistant:{assistant_name}" if session_id else None
            planner_role = f"assistant:{assistant_name}"

        # Does the resolved provider for this role/model actually have a server-side session?
        # (OpenAI Responses → yes; alternate providers → no.) Default True for plain OpenAI
        # services that don't expose the probe.
        supports_session = True
        _probe = getattr(self.openai, "supports_server_side_session", None)
        if callable(_probe):
            try:
                supports_session = bool(_probe(model, planner_role))
            except Exception:  # noqa: BLE001 — never break the loop on a capability probe
                supports_session = True

        planner_keep_context = supports_session
        planner_payload["_stateful_continuation"] = is_planner_continuation and supports_session

        log.debugx(
            "Planner payload opgebouwd",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            supports_session=supports_session,
            planner_payload_keys=list(planner_payload.keys()),
        )

        plan_prompt = assistant.prompt(question=question, **planner_payload)
        log.infox(
            "Planner prompt gebouwd",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            prompt_length=len(plan_prompt or ""),
            model=model,
        )

        # Input shape:
        # - server-side session (OpenAI, toggle on): send the delta prompt; the model holds
        #   the earlier hops in its Responses session.
        # - transcript path (default, every provider): send a structured conversation —
        #   the anchor (rules+manifest+schema+question) followed by each prior hop as a real
        #   assistant tool-call turn + user observation turn. This is a stable, append-only,
        #   cache-friendly prefix instead of a re-serialised _acc_* blob, and it makes the
        #   loop behave identically on OpenAI / Anthropic / local.
        if supports_session:
            plan_input: Any = plan_prompt
        else:
            plan_input = _build_transcript_messages(assistant, question, planner_payload, plan_prompt)

        planner_metadata = {
            "kind": "assistant_planner",
            "assistant": assistant_name,
            "turn_id": str(turn_id),
            "workflow_background": str(is_workflow_background).lower(),
            "planner_continuation": str(is_planner_continuation).lower(),
        }

        # Streaming: the merged agent's final answer lives in the planner JSON's
        # final_answer field, so to show it building up live we stream the planner call and
        # progressively extract final_answer. Only on the OpenAI base path (json_schema is
        # dropped there → free-text JSON we can stream + parse) and only on the transcript
        # route (not the server-side-session route, to avoid touching the response chain).
        # Any failure → fall back to the normal non-streaming call (zero regression).
        plan_resp = None
        # Option A — Claude Code as a FULL AGENT. When the chat planner slot
        # resolves to claude_code, don't run the ND3X planner loop (Claude Code is
        # an autonomous agent and stalls in the plan-JSON role). Instead let it
        # drive its own agent loop with ND3X's tools/skills/MCP via the gateway,
        # then wrap its natural-language answer as a final plan so this loop ends
        # here. Not for workflow-background turns (those use the claude_code
        # workflow engine).
        _cc_type = None
        try:
            _cc_probe = getattr(self.openai, "chat_provider_type", None)
            if callable(_cc_probe) and not is_workflow_background:
                _cc_type = _cc_probe(model, planner_role)
        except Exception:  # noqa: BLE001
            _cc_type = None
        if _cc_type == "claude_code":
            from db.database import SessionLocal
            from services.assistants.claude_code_chat_agent import ClaudeCodeChatAgent
            try:
                with SessionLocal() as _agent_db:
                    _agent = ClaudeCodeChatAgent(_agent_db)
                    # Typed stream: the agent's interim text ('thinking') and its
                    # tool calls go to the STEPS view via the same event types the
                    # normal loop uses (agent_narration / tool_call), so no FE
                    # change is needed. Only the 'answer' event becomes the chat
                    # reply.
                    _answer = ""
                    _narration = list(payload.get("_narration") or [])
                    async for _ev in _agent.run_stream_events(
                        user_input=plan_input, model=model,
                        skill_names=selected_skill_names,
                    ):
                        _kind = _ev.get("kind")
                        if _kind == "answer":
                            _answer = _ev.get("text") or ""
                            if progress_cb is not None:
                                try:
                                    progress_cb({
                                        "type": "answer_partial", "turn_id": turn_id,
                                        "assistant": assistant_name, "partial_answer": _answer,
                                    })
                                except Exception:  # noqa: BLE001
                                    pass
                        elif _kind == "thinking":
                            _say = (_ev.get("text") or "").strip()
                            if _say:
                                self.trace_fn(
                                    trace, thread_id=session_id, turn_id=turn_id,
                                    type="agent_narration", summary=_say,
                                    data={"assistant": assistant_name, "say": _say},
                                    progress_cb=progress_cb,
                                )
                                _narration.append({"kind": "say", "text": _say, "ts": time.time()})
                        elif _kind == "tool":
                            _tool = _ev.get("name") or "tool"
                            self.trace_fn(
                                trace, thread_id=session_id, turn_id=turn_id,
                                type="tool_call", summary=f"Calling {_tool}",
                                data={"assistant": assistant_name, "tool": _tool},
                                progress_cb=progress_cb,
                            )
                            _narration.append({"kind": "tool", "text": f"Using {_tool}", "ts": time.time()})
                    payload["_narration"] = _narration
                # A FULLY schema-valid final plan (planner.schema.json requires
                # all of these) so plan validation passes — a partial plan makes
                # the validator retry the "planner" and loop into
                # plan_validation_failed.
                plan_resp = SimpleNamespace(text=json.dumps({
                    "action": "final",
                    "reason": "Answered by the Claude Code agent.",
                    "say": "",
                    "tool_calls": [],
                    "response_mode": "synthesize_answer",
                    "search_keywords": [],
                    "final_answer": _answer,
                    "downstream_handoff": None,
                }))
            except Exception as _cc_exc:  # noqa: BLE001 — surface as a planner error
                self.trace_fn(
                    trace, thread_id=session_id, turn_id=turn_id,
                    type="planner_call_error",
                    summary="Claude Code chat-agent run failed",
                    data={"assistant": assistant_name, "model": model,
                          "error": type(_cc_exc).__name__, "message": str(_cc_exc)[:300]},
                    progress_cb=progress_cb,
                )
                raise

        _resolves_openai = getattr(self.openai, "resolves_to_openai", None)
        can_stream_planner = (
            plan_resp is None  # not already answered by the Claude Code agent above
            and progress_cb is not None
            and not planner_keep_context
            and callable(_resolves_openai)
            and bool(_resolves_openai(model, planner_role))
        )
        # Audit the model call itself so a hang/timeout/empty-output is visible in the
        # trace. Previously only a parse failure emitted an event; a timed-out/hung call
        # propagated (or never returned) with no audit record, leaving a 3-event stub.
        # planner_call_start fires before; planner_call_end after (duration + output size);
        # planner_call_error on exception. start with no end/error == the call hung.
        # User-facing status: keep it friendly ("Thinking…") rather than the technical
        # "Planner model call starting (model)". The model + role stay in `data` for the
        # diagnostic trace.
        self.trace_fn(
            trace, thread_id=session_id, turn_id=turn_id,
            type="planner_call_start",
            summary="Thinking…",
            data={
                "assistant": assistant_name, "model": model, "role": planner_role,
                "streaming": bool(can_stream_planner),
                "prompt_chars": len(plan_prompt or ""),
                "instruction_chars": len(effective_instructions or ""),
                "iteration": loop_iteration,
            },
            progress_cb=progress_cb,
        )
        _planner_t0 = time.monotonic()
        if can_stream_planner:
            try:
                acc: list[str] = []
                last_emit = 0.0
                last_partial = ""
                emit_count = 0
                async for delta in self.openai.ask_orchestration_stream(
                    plan_input,
                    role=planner_role,
                    instructions=effective_instructions,
                    model=model,
                    max_output_tokens=6000,
                    metadata=planner_metadata,
                ):
                    acc.append(delta)
                    now = time.monotonic()
                    if now - last_emit >= 0.3:
                        last_emit = now
                        partial = _extract_partial_final_answer("".join(acc))
                        if partial and partial != last_partial:
                            last_partial = partial
                            emit_count += 1
                            try:
                                progress_cb({
                                    "type": "answer_partial",
                                    "turn_id": turn_id,
                                    "assistant": assistant_name,
                                    "partial_answer": partial,
                                })
                            except Exception:  # noqa: BLE001
                                pass
                text = "".join(acc).strip()
                log.infox(
                    "Planner gestreamd",
                    session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
                    delta_chars=len(text), answer_partial_emits=emit_count,
                    final_answer_len=len(_extract_partial_final_answer(text) or ""),
                )
                if text:
                    plan_resp = SimpleNamespace(text=text)
            except Exception as exc:  # noqa: BLE001 — fall back to non-streaming
                log.warningx(
                    "Planner streaming mislukt; val terug op non-streaming",
                    session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
                    error=str(exc),
                )
                plan_resp = None

        if plan_resp is None:
            try:
                plan_resp = await self.openai.ask_orchestration_async(
                    plan_input,
                    role=planner_role,
                    instructions=effective_instructions,
                    keep_context=planner_keep_context,
                    store=planner_keep_context,
                    session_id=planner_session_id,
                    model=model,
                    max_output_tokens=6000,
                    json_schema=getattr(getattr(assistant, "config", None), "schema", None),
                    metadata=planner_metadata,
                )
            except Exception as _pexc:  # noqa: BLE001 — record then re-raise unchanged
                self.trace_fn(
                    trace, thread_id=session_id, turn_id=turn_id,
                    type="planner_call_error",
                    summary="Planner model call failed (timeout / connection / provider error)",
                    data={
                        "assistant": assistant_name, "model": model,
                        "duration_s": round(time.monotonic() - _planner_t0, 2),
                        "error": type(_pexc).__name__, "message": str(_pexc)[:300],
                    },
                    progress_cb=progress_cb,
                )
                raise
        _planner_text = getattr(plan_resp, "text", "") or ""
        _planner_dur = round(time.monotonic() - _planner_t0, 2)
        # Slow-step flag (TODO 1.4): a planner call over the threshold is marked
        # warn in the audit + logged, and counted in the per-model metrics rollup.
        from services.model_metrics_service import slow_step_threshold_s
        _is_slow = _planner_dur >= slow_step_threshold_s()
        if _is_slow:
            log.warningx(
                "Trage planner-stap",
                session_id=session_id, turn_id=turn_id, model=model,
                duration_s=_planner_dur, threshold_s=slow_step_threshold_s(),
            )
        self.trace_fn(
            trace, thread_id=session_id, turn_id=turn_id,
            type="planner_call_end",
            level="warn" if _is_slow else "info",
            summary=f"Planner model call returned {len(_planner_text)} chars in "
                    f"{round(time.monotonic() - _planner_t0, 1)}s"
                    + (" (SLOW)" if _is_slow else ""),
            data={
                "assistant": assistant_name, "model": model,
                "duration_s": _planner_dur,
                "slow": _is_slow,
                "output_chars": len(_planner_text),
                "empty_output": not bool(_planner_text.strip()),
            },
            progress_cb=progress_cb,
        )
        log.infox(
            "Planner OpenAI response ontvangen",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            response_text_length=len(_planner_text),
        )
        try:
            log.debugx(
                "Planner JSON extractie gestart",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
            )
            raw_plan = assistant.extract_first_json_object(plan_resp.text)
            plan = _coerce_plan_to_dict(raw_plan)
            log.debugx(
                "Planner JSON extractie afgerond",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                raw_plan_type=type(raw_plan).__name__,
                plan_keys=list(plan.keys()) if isinstance(plan, dict) else None,
                has_plan_error=bool(plan.get("_plan_error")) if isinstance(plan, dict) else None,
            )
            if plan.get("_plan_error"):
                log.errorx(
                    "Planner gaf ongeldige shape terug",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    plan_error=plan.get("_plan_error"),
                    has_failure_handled=has_failure_handled,
                )
                # Don't hard-fail on a one-off bad shape (e.g. the model emitted a JSON list
                # of objects): retry once — the next attempt almost always returns a valid
                # plan. Only error out if it's still broken after the retry.
                if not has_failure_handled:
                    payload["_failure_handled"] = True
                    self.trace_fn(
                        trace, thread_id=session_id, turn_id=turn_id, type="turn_end",
                        summary="Planner returned invalid shape, trying again.",
                        data={"assistant": assistant_name, "plan_error": plan.get("_plan_error")},
                        progress_cb=progress_cb,
                    )
                    return await self.run(
                        assistant=assistant,
                        question=question,
                        model=model,
                        payload=payload,
                        session_id=session_id,
                        turn_id=turn_id + 1,
                        trace=trace,
                        progress_cb=progress_cb,
                    )
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="error",
                    level="error",
                    summary="Planner returned invalid shape",
                    data={
                        "assistant": assistant_name,
                        "model": model,
                        "plan_error": plan.get("_plan_error"),
                        "plan_preview": _preview(plan.get("_plan_raw")),
                        # Full raw output so the audit shows exactly what the model returned.
                        "raw_output": plan.get("_plan_raw"),
                    },
                    progress_cb=progress_cb,
                )
                return build_result(
                    mode="error",
                    answer=plan.get("_plan_error") or "Planner returned an invalid response shape.",
                    trace=trace,
                    thread_id=session_id,
                )
        except ValueError:
            # The agent answered in plain prose instead of planner JSON — common with
            # smaller models on conversational turns now that every turn runs through the
            # one agent loop. A clean non-JSON reply IS a final answer, so take it as such
            # instead of failing to parse. Only retry/error for empty or broken-JSON output.
            prose = (getattr(plan_resp, "text", "") or "").strip()
            if prose and not _looks_like_planner_json(prose):
                log.infox(
                    "Planner gaf vrije tekst i.p.v. JSON; behandeld als final answer",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    response_text_length=len(prose),
                )
                # Salvaged prose is by construction not schema-shaped — it must
                # skip the conformity gate below (it IS the final answer).
                plan = {"action": "final", "final_answer": prose, "_salvaged_prose": True}
            else:
                log.warningx(
                    "Planner plan kon niet worden geparsed",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    has_failure_handled=has_failure_handled,
                    response_text_length=len(getattr(plan_resp, "text", "") or ""),
                )
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="turn_end",
                    summary="Plan could not be parsed, trying again.",
                    data={"assistant": assistant_name, "plan": plan_resp.text},
                    progress_cb=progress_cb,
                )
                if not has_failure_handled:
                    payload["_failure_handled"] = True
                    log.infox(
                        "Planner parse failure retry gestart",
                        session_id=session_id,
                        turn_id=turn_id,
                        next_turn_id=turn_id + 1,
                        assistant_name=assistant_name,
                    )
                    return await self.run(
                        assistant=assistant,
                        question=question,
                        model=model,
                        payload=payload,
                        session_id=session_id,
                        turn_id=turn_id + 1,
                        trace=trace,
                    )
                raw_output = getattr(plan_resp, "text", "") or ""
                log.errorx(
                    "Planner parse failure blijft bestaan na retry",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    raw_output_length=len(raw_output),
                    raw_output_preview=raw_output[:2000],
                )
                # Persist the full raw model output to the audit so it can be
                # inspected (this is exactly what was missing before).
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="error",
                    summary="Planner output could not be parsed after a retry.",
                    data={
                        "assistant": assistant_name,
                        "model": model,
                        "error": "planner_unparseable",
                        "raw_output": raw_output,
                        "raw_output_chars": len(raw_output),
                    },
                    progress_cb=progress_cb,
                )
                # Light mode: rather than failing the turn, salvage the model's raw
                # text as the answer (small local models often reply in prose).
                if getattr(settings, "LOCAL_MODEL_LIGHT_MODE", True) and raw_output.strip():
                    log.infox(
                        "Light mode: ruwe planner-output als antwoord gebruikt",
                        session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
                    )
                    return build_result(
                        mode="answer",
                        answer=raw_output.strip(),
                        trace=trace,
                        thread_id=session_id,
                    )
                return build_result(
                    mode="error",
                    answer=(
                        "I couldn't understand the model's reply this turn — it didn't come "
                        "back in the expected format, even after retrying. The model's raw "
                        "output is saved in this turn's audit (look for “Planner output could "
                        "not be parsed”). This often happens with smaller local models; try a "
                        "larger/cloud model on the Agent slot, or enable Light mode for local "
                        "models in AI Models."
                    ),
                    trace=trace,
                    thread_id=session_id,
                )

        # Conformity gate (TODO 1.1): the plan parsed as JSON, but a semantically
        # dead plan (empty select_skills, tool_calls without calls, schema
        # violations) would silently burn a full agent hop — ~100s on a local
        # model. Reject it with targeted feedback: the retry hop sees the exact
        # problems and corrects its reply. Budget: 2 corrective retries per turn,
        # then a clear error (audit carries the invalid plan).
        from services.assistants.plan_validator import validate_plan
        if plan.pop("_salvaged_prose", False):
            _validation_problems = []
        else:
            _validation_problems = validate_plan(plan, getattr(assistant.config, "schema", None))
        if _validation_problems:
            _val_retries = int(payload.get("_plan_validation_retries") or 0)
            self.trace_fn(
                trace, thread_id=session_id, turn_id=turn_id,
                type="plan_validation_failed",
                level="warn",
                summary=f"Plan rejected by validation ({len(_validation_problems)} problem(s)), "
                        f"attempt {_val_retries + 1}",
                data={
                    "assistant": assistant_name,
                    "model": model,
                    "problems": _validation_problems,
                    "plan": plan,
                    "retries_used": _val_retries,
                },
                progress_cb=progress_cb,
            )
            if _val_retries < 2:
                payload["_plan_validation_retries"] = _val_retries + 1
                payload["_plan_validation_feedback"] = _validation_problems
                return await self.run(
                    assistant=assistant,
                    question=question,
                    model=model,
                    payload=payload,
                    session_id=session_id,
                    turn_id=turn_id + 1,
                    trace=trace,
                    progress_cb=progress_cb,
                )
            return build_result(
                mode="error",
                answer=(
                    "The model kept returning a plan that fails validation, even after "
                    "corrective retries. The invalid plans and the exact problems are in "
                    "this turn's audit (look for “plan_validation_failed”). "
                    "Problems: " + "; ".join(_validation_problems[:3])
                ),
                trace=trace,
                thread_id=session_id,
            )
        # Valid plan: clear any pending correction feedback so later hops don't
        # keep repeating it (the retries budget stays used-up for this turn).
        payload.pop("_plan_validation_feedback", None)

        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="plan",
            summary=f"{assistant_name} produced action={(plan.get('action') or '').strip()}",
            data={"assistant": assistant_name, "plan": plan},
            progress_cb=progress_cb,
        )

        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="assistant_plan_received",
            summary="Assistant planner output received",
            data={
                "assistant": assistant_name,
                "action": (plan.get("action") or "").strip(),
                "response_mode": (plan.get("response_mode") or "synthesize_answer").strip(),
            },
            progress_cb=progress_cb,
        )

        action = (plan.get("action") or "").strip()
        log.infox(
            "Planner plan ontvangen",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            action=action,
            response_mode=(plan.get("response_mode") or "synthesize_answer").strip(),
            plan_keys=list(plan.keys()),
        )

        # User-facing running commentary (chat only). The model fills `say` with a
        # short, plain-language note on what it's doing/found/recovering; we surface
        # it live as an `agent_narration` event so the chat shows Claude-Code-style
        # progress. Workflow runs are autonomous → suppress narration there.
        say = (plan.get("say") or "").strip()
        if say and not bool(payload.get("_workflow_background")):
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_narration",
                summary=say,
                data={"assistant": assistant_name, "say": say, "for_action": action},
                progress_cb=progress_cb,
            )
            narration = list(payload.get("_narration") or [])
            narration.append({"kind": "say", "text": say, "ts": time.time()})
            payload["_narration"] = narration

        # Merged agent loop: the agent picks its own skill(s) as a first step instead of a
        # separate selector call. Load the chosen domain skills' tools and re-enter the loop.
        if action == "select_skills":
            skill_debug: Optional[dict] = None
            if payload.get("_extra_guidance"):
                # Less-capable model: tolerant resolution — map a near-miss or a
                # TOOL name to the SKILL that provides it, and recover skills implied
                # by referenced tool_calls. Gated to the guidance toggle so strong
                # models keep the strict exact-match behaviour.
                requested_tool_names = [
                    (tc.get("tool") or "").strip()
                    for tc in (plan.get("tool_calls") or [])
                    if isinstance(tc, dict)
                ]
                chosen, skill_debug = _resolve_skill_choices(
                    assistant,
                    plan.get("selected_skill_names") or [],
                    requested_tool_names,
                )
            else:
                cfg = getattr(assistant, "config", None)
                catalog = {
                    getattr(s, "name", None)
                    for s in (getattr(cfg, "skills", None) or [])
                    if getattr(s, "is_enabled", True)
                    and not getattr(s, "is_system", False)
                    and not getattr(s, "is_runtime", False)
                }
                chosen = [
                    str(x).strip()
                    for x in (plan.get("selected_skill_names") or [])
                    if str(x).strip() in catalog
                ]
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_skill_selection",
                summary=f"Agent selected skill(s): {chosen or '[]'}",
                data={"assistant": assistant_name, "selected_skill_names": chosen,
                      **({"resolution": skill_debug} if skill_debug is not None else {})},
                progress_cb=progress_cb,
            )
            next_payload = dict(payload or {})
            next_payload["_selected_skill_names"] = chosen
            next_payload.pop("_needs_skill_selection", None)
            # Selecting a skill is loop overhead, not real work — refund this hop so it
            # doesn't eat the iteration budget (the next entry re-increments it back).
            next_payload["_agent_loop_iterations"] = max(
                0, int(payload.get("_agent_loop_iterations") or 1) - 1
            )
            return await self.run(
                assistant=assistant,
                question=question,
                model=model,
                payload=next_payload,
                session_id=session_id,
                turn_id=turn_id,
                trace=trace,
                progress_cb=progress_cb,
            )

        if action in {"final", "ask_user", "propose_plan"}:
            log.infox(
                "Assistant pipeline direct final/ask_user pad gestart",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                action=action,
                has_used_evaluate=has_used_evaluate,
            )
            final_text = plan.get("final_answer") or ""
            if action in {"ask_user", "propose_plan"} and not (final_text or "").strip():
                # Models sometimes put the clarification question / plan in `reason`
                # instead of final_answer; surface it so it isn't lost as empty.
                final_text = plan.get("reason") or ""
            extracted = _extract_final_answer_if_json(final_text)
            if extracted:
                log.debugx(
                    "Final answer uit JSON geëxtraheerd in pipeline",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    extracted_length=len(extracted),
                )
                final_text = extracted
            elif _looks_like_planner_json(final_text):
                log.warningx(
                    "Final answer lijkt planner JSON, fallback wordt gebruikt",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    final_text_length=len(final_text or ""),
                )
                final_text = _fallback_no_evidence_message()

            # ── Verification / self-check hop (Phase 4) ──────────────────────
            # Vóór we een "final" antwoord als voltooid markeren, doen we een
            # self-check. Bij problemen heropenen we de loop met feedback zodat
            # de assistant het antwoord verbetert. Begrensd door
            # AGENT_MAX_VERIFICATION_HOPS én de bestaande loop-budgetten.
            if (
                action == "final"
                # Goal mode always self-checks its final answer, even when the
                # global verification toggle is off — "demonstrably achieved"
                # is the whole contract.
                and (bool(getattr(settings, "AGENT_VERIFICATION_ENABLED", True))
                     or bool(payload.get("_goal_mode")))
                and not is_workflow_background
                and bool(payload.get("_acc_tool_calls"))
            ):
                verification_hops = int(payload.get("_verification_hops") or 0)
                max_verification_hops = int(getattr(settings, "AGENT_MAX_VERIFICATION_HOPS", 1))
                findings = _verification_findings(final_text, payload)
                if not findings:
                    self.trace_fn(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="verification_passed",
                        summary="Self-check passed",
                        data={"assistant": assistant_name, "hops_used": verification_hops},
                        progress_cb=progress_cb,
                    )
                elif verification_hops >= max_verification_hops:
                    self.trace_fn(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="verification_exhausted",
                        level="warn",
                        summary="Self-check budget exhausted; returning best answer",
                        data={"assistant": assistant_name, "findings": findings, "hops_used": verification_hops},
                        progress_cb=progress_cb,
                    )
                else:
                    self.trace_fn(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="verification_failed",
                        level="warn",
                        summary=f"Self-check found {len(findings)} issue(s); re-attempting",
                        data={"assistant": assistant_name, "findings": findings, "hop": verification_hops + 1},
                        progress_cb=progress_cb,
                    )
                    next_payload = dict(payload)
                    next_payload["_verification_hops"] = verification_hops + 1
                    next_payload["_verification_feedback"] = {
                        "findings": findings,
                        "previous_answer": final_text,
                    }
                    verification_question = (
                        f"{question}\n\n## Self-check feedback (verification hop {verification_hops + 1})\n"
                        "Your previous answer did not pass the self-check. Address each issue below and "
                        "produce a complete, verified answer:\n"
                        + "\n".join(f"- {f}" for f in findings)
                    )
                    return await self.run(
                        assistant=assistant,
                        question=verification_question,
                        model=model,
                        payload=next_payload,
                        session_id=session_id,
                        turn_id=turn_id,
                        trace=trace,
                        progress_cb=progress_cb,
                    )

            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="assistant_turn_end",
                summary=f"Assistant completed ({action})",
                data={"assistant": assistant_name, "mode": action, "answer_preview": (final_text or "")[:400]},
                progress_cb=progress_cb,
            )
            downstream_handoff = None
            if action not in {"ask_user", "propose_plan"}:
                downstream_handoff = _extract_downstream_handoff(plan, final_text or "", [])
                downstream_handoff = self._store_large_handoff_content_if_needed(
                    handoff=downstream_handoff,
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    trace=trace,
                )

            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_loop_terminal",
                summary="Assistant agent loop reached terminal state",
                data={
                    "assistant": assistant_name,
                    "terminal_state": TERMINAL_WAITING_FOR_USER if action in {"ask_user", "propose_plan"} else TERMINAL_COMPLETED,
                    "action": action,
                },
                progress_cb=progress_cb,
            )

            log.infox(
                "Assistant pipeline direct final/ask_user pad afgerond",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                action=action,
                answer_length=len((final_text or "").strip()),
                has_downstream_handoff=downstream_handoff is not None,
            )
            pending_action = None
            result_mode = action
            terminal_state = TERMINAL_WAITING_FOR_USER if action in {"ask_user", "propose_plan"} else TERMINAL_COMPLETED

            if action in {"ask_user", "propose_plan"}:
                ask_question = (final_text or "").strip()
                # A workflow NEVER asks the user — it is fully autonomous. Any ask_user
                # (and any empty-question ask anywhere) fails the operation with the
                # question as the reason, so the run never silently waits.
                if _ask_user_should_fail(ask_question, is_workflow=is_workflow_background):
                    reason = (
                        "Agent requested user input but gave no question."
                        if not ask_question
                        else f"Workflow runs autonomously and cannot ask the user; the agent "
                             f"needs input to continue. Question: {ask_question}"
                    )
                    self.trace_fn(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="error",
                        level="error",
                        summary="ask_user failed (autonomous workflow or empty question)",
                        data={"assistant": assistant_name, "question": ask_question, "is_workflow": is_workflow_background},
                        progress_cb=progress_cb,
                    )
                    return build_result(
                        mode="error",
                        answer=reason,
                        trace=trace,
                        thread_id=session_id,
                        terminal_state=TERMINAL_FAILED,
                    )

            return build_result(
                mode=result_mode,
                answer=(final_text or "").strip(),
                trace=trace,
                thread_id=session_id,
                downstream_handoff=downstream_handoff,
                terminal_state=terminal_state,
                pending_action=pending_action,
            )

        tool_calls = self.tool_runner.normalize_tool_calls(plan)
        # Safety net (always on): resolve each tool_id from the tool name when it's
        # unambiguous — fills a dropped id AND corrects a hallucinated one (which the
        # guard would otherwise block). Safe: a correct id resolves to itself.
        _ids_before = [tc.get("tool_id") for tc in tool_calls if isinstance(tc, dict)]
        tool_calls = _backfill_tool_ids(assistant, tool_calls)
        _fixed = [
            {"tool": (tc.get("tool") or "").strip(), "from": before, "to": tc.get("tool_id")}
            for before, tc in zip(_ids_before, tool_calls)
            if isinstance(tc, dict) and before != tc.get("tool_id")
        ]
        if _fixed:
            self.trace_fn(
                trace, thread_id=session_id, turn_id=turn_id,
                type="tool_id_resolved",
                summary=f"Resolved tool_id by name: {_fixed}",
                data={"assistant": assistant_name, "fixes": _fixed},
                progress_cb=progress_cb,
            )
        response_mode = (plan.get("response_mode") or "synthesize_answer").strip()

        log.infox(
            "Tool calls genormaliseerd vanuit planner plan",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            response_mode=response_mode,
            tool_call_count=len(tool_calls or []),
            tool_names=[(tc.get("tool") or "").strip() for tc in tool_calls if isinstance(tc, dict)],
        )

        if bool(payload.get("_text_search_used", False)) and _plan_has_tool(tool_calls, "text_search"):
            log.warningx(
                "Pipeline blokkeert tweede text_search in dezelfde cycle",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool_call_count=len(tool_calls or []),
            )
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="error",
                level="error",
                summary="Blocked: text_search used more than once in cycle",
                data={"assistant": assistant_name, "tool_calls": tool_calls},
                progress_cb=progress_cb,
            )
            return build_result(
                mode="error",
                answer="Planner attempted a second text_search in the same cycle, which is not allowed.",
                trace=trace,
                thread_id=session_id,
                tool_calls=tool_calls,
            )

        try:
            log.debugx(
                "Tool guard assert_tools_allowed gestart",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool_call_count=len(tool_calls or []),
            )
            self.tool_guard.assert_tools_allowed(
                assistant.config,
                tool_calls,
                selected_skill_names=selected_skill_names,
            )
            log.debugx(
                "Tool guard assert_tools_allowed akkoord",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
            )
        except ValueError as e:
            log.warningx(
                "Tool geblokkeerd door assistant registry",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                error=str(e),
                tool_call_count=len(tool_calls or []),
            )
            # Recoverable: a tool the agent wanted isn't in its currently-selected
            # skills (e.g. it picked a file-inspection skill but needs system__shell_exec).
            # Instead of failing the turn, re-enter the loop and let it select the right
            # skill — naming which skill provides each blocked tool. Bounded so a model
            # that keeps mis-selecting can't loop forever.
            block_retries = int(payload.get("_tool_block_retries") or 0)
            wanted = [(tc.get("tool") or "").strip() for tc in (tool_calls or []) if isinstance(tc, dict)]
            providers_hint = _skills_providing_tools(assistant, wanted)
            if block_retries < int(getattr(settings, "AGENT_MAX_TOOL_BLOCK_RETRIES", 2)) and not is_workflow_background:
                self.trace_fn(
                    trace, thread_id=session_id, turn_id=turn_id,
                    type="tool_block_recovering", level="warn",
                    summary="Blocked tool — re-selecting a skill that provides it",
                    data={"assistant": assistant_name, "wanted_tools": wanted,
                          "providers": providers_hint, "selected_skill_names": selected_skill_names},
                    progress_cb=progress_cb,
                )
                next_payload = dict(payload)
                next_payload["_tool_block_retries"] = block_retries + 1
                next_payload["_needs_skill_selection"] = True
                next_payload["_selected_skill_names"] = []  # re-pick from the catalog
                hint = (
                    f"select the skill that provides it — {providers_hint}"
                    if providers_hint else
                    "select the skill that provides it from the catalog"
                )
                recovery_question = (
                    f"{question}\n\n## Tool not available with your current skills\n{e}\n"
                    f"Use action='select_skills' to {hint}, then continue with the task."
                )
                return await self.run(
                    assistant=assistant,
                    question=recovery_question,
                    model=model,
                    payload=next_payload,
                    session_id=session_id,
                    turn_id=turn_id,
                    trace=trace,
                    progress_cb=progress_cb,
                )
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="error",
                level="error",
                summary="Tool blocked by assistant registry",
                data={
                    "assistant": assistant_name,
                    "selected_skill_names": selected_skill_names,
                    "plan": plan,
                },
                progress_cb=progress_cb,
            )
            return build_result(
                mode="error",
                answer=str(e),
                trace=trace,
                thread_id=session_id,
                tool_calls=tool_calls,
                terminal_state=TERMINAL_FAILED,
            )

        confirmed_guarded_tool_hashes: set[str] = set()
        guarded_tool_calls = [
            tc for tc in (tool_calls or [])
            if isinstance(tc, dict) and is_guarded_tool((tc.get("tool") or "").strip())
        ]
        if guarded_tool_calls:
            is_workflow_background = bool((payload or {}).get("_workflow_background"))
            if is_workflow_background:
                execution_policy = (payload or {}).get("_workflow_execution_policy") or {}
                skill_files_root = _selected_skill_files_root(assistant, selected_skill_names)
                for guarded_tool_call in guarded_tool_calls:
                    tool = (guarded_tool_call.get("tool") or "").strip()
                    try:
                        validation = validate_guarded_tool_call(guarded_tool_call)
                    except ValueError as e:
                        self.trace_fn(
                            trace,
                            thread_id=session_id,
                            turn_id=turn_id,
                            type="error",
                            level="error",
                            summary="Guarded tool validation failed",
                            data={"assistant": assistant_name, "tool": tool, "error": str(e)},
                            progress_cb=progress_cb,
                        )
                        return build_result(mode="error", answer=str(e), trace=trace, thread_id=session_id, tool_calls=tool_calls, terminal_state=TERMINAL_FAILED)

                    args = validation.tool_call.get("args") or {}
                    decision = evaluate_workflow_guarded_tool_policy(
                        tool=tool,
                        tool_args=args,
                        execution_policy=execution_policy,
                        skill_files_root=skill_files_root,
                    )
                    self.trace_fn(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="guarded_tool_policy_evaluated",
                        level="info" if decision.allowed else "error",
                        summary="Guarded tool workflow policy evaluated",
                        data={
                            "assistant": assistant_name,
                            **decision.trace_data(
                                command=args.get("command") or "",
                                working_dir=args.get("working_dir") or args.get("cwd"),
                            ),
                        },
                        progress_cb=progress_cb,
                    )
                    if not decision.allowed:
                        if decision.on_denied == "pause":
                            pending_action = {
                                "type": "workflow_tool_approval",
                                "assistant_id": getattr(getattr(assistant, "config", None), "id", None),
                                "assistant_name": getattr(getattr(assistant, "config", None), "name", None) or assistant_name,
                                "tool": tool,
                                "tool_id": validation.tool_call.get("tool_id"),
                                "risk_level": "high",
                                "message": "Shell command denied by workflow policy and requires approval.",
                                "confirmation_prompt": "Approve this shell command for this workflow operation?",
                                "display": {
                                    "command": args.get("command"),
                                    "working_dir": args.get("working_dir") or args.get("cwd"),
                                    "timeout": args.get("timeout"),
                                },
                                "tool_call_hash": validation.tool_call_hash,
                                "tool_call": validation.tool_call,
                                "policy_decision": decision.trace_data(
                                    command=args.get("command") or "",
                                    working_dir=args.get("working_dir") or args.get("cwd"),
                                ),
                                "resume_payload": {
                                    "question": question,
                                    "model": model,
                                    "payload": _payload_without_callbacks(payload),
                                    "agent_loop_state": _agent_loop_state(payload),
                                    "iteration_count": int(payload.get("_agent_loop_iterations") or 0),
                                    "tool_call_count": int(payload.get("_agent_loop_tool_calls") or 0),
                                },
                            }
                            self.trace_fn(
                                trace,
                                thread_id=session_id,
                                turn_id=turn_id,
                                type="agent_loop_terminal",
                                level="warn",
                                summary="Assistant agent loop is waiting for workflow approval",
                                data={"assistant": assistant_name, "terminal_state": TERMINAL_WAITING_FOR_CONFIRMATION, "reason": decision.reason},
                                progress_cb=progress_cb,
                            )
                            return build_result(
                                mode="workflow_waiting",
                                answer="Workflow operation is waiting for guarded tool approval.",
                                trace=trace,
                                thread_id=session_id,
                                tool_calls=tool_calls,
                                terminal_state=TERMINAL_WAITING_FOR_CONFIRMATION,
                                pending_action=pending_action,
                            )

                        answer = f"policy_denied: {decision.reason}"
                        self.trace_fn(
                            trace,
                            thread_id=session_id,
                            turn_id=turn_id,
                            type="agent_loop_terminal",
                            level="error",
                            summary="Assistant agent loop reached terminal state",
                            data={"assistant": assistant_name, "terminal_state": TERMINAL_POLICY_DENIED, "reason": decision.reason},
                            progress_cb=progress_cb,
                        )
                        return build_result(
                            mode="error",
                            answer=answer,
                            trace=trace,
                            thread_id=session_id,
                            tool_calls=tool_calls,
                            terminal_state=TERMINAL_POLICY_DENIED,
                        )
                    confirmed_guarded_tool_hashes.add(tool_call_hash(guarded_tool_call))
            else:
                guarded_tool_call = guarded_tool_calls[0]
                try:
                    pending_action = build_tool_confirmation_pending_action(guarded_tool_call)
                except ValueError as e:
                    self.trace_fn(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="error",
                        level="error",
                        summary="Guarded tool validation failed",
                        data={
                            "assistant": assistant_name,
                            "tool": (guarded_tool_call.get("tool") or "").strip(),
                            "error": str(e),
                        },
                        progress_cb=progress_cb,
                    )
                    return build_result(
                        mode="error",
                        answer=str(e),
                        trace=trace,
                        thread_id=session_id,
                        tool_calls=tool_calls,
                    )

                pending_action["continuation"] = {
                    "assistant_id": getattr(getattr(assistant, "config", None), "id", None),
                    "assistant_name": getattr(getattr(assistant, "config", None), "name", None) or assistant_name,
                    "question": question,
                    "payload": {k: v for k, v in (payload or {}).items() if k != "_cancellation_check"},
                    "model": model,
                }

                if self.pending is None:
                    return build_result(
                        mode="error",
                        answer="Guarded tools require interactive confirmation, which is not available in this execution context.",
                        trace=trace,
                        thread_id=session_id,
                        tool_calls=tool_calls,
                    )

                self.pending.set(session_id, pending_action)
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="guarded_tool_confirmation_required",
                    level="warn",
                    summary="Guarded tool requires confirmation",
                    data={"assistant": assistant_name, **guard_trace_data(pending_action)},
                    progress_cb=progress_cb,
                )
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="agent_loop_terminal",
                    level="warn",
                    summary="Assistant agent loop reached terminal state",
                    data={"assistant": assistant_name, "terminal_state": TERMINAL_WAITING_FOR_CONFIRMATION},
                    progress_cb=progress_cb,
                )
                return build_result(
                    mode="confirm_action",
                    answer=pending_action.get("prompt") or pending_action.get("confirmation_prompt") or "Please confirm this tool call.",
                    trace=trace,
                    thread_id=session_id,
                    tool_calls=tool_calls,
                    pending_action=pending_action,
                    terminal_state=TERMINAL_WAITING_FOR_CONFIRMATION,
                )

        if any(self.tool_guard.is_mutation_tool(_tool_call_name(tc)) for tc in tool_calls):
            log.infox(
                "Mutation tool calls gedetecteerd",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool_names=[_tool_call_name(tc) for tc in tool_calls],
                require_mutation_confirmation=self.require_mutation_confirmation,
                has_pending_store=self.pending is not None,
            )
            try:
                prompt = build_mutation_confirmation_prompt(tool_calls)
                log.debugx(
                    "Mutation confirmation prompt gebouwd",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    prompt_length=len(prompt or ""),
                )
            except ValueError as e:
                log.warningx(
                    "Mutation confirmation prompt bouwen mislukt",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    error=str(e),
                )
                return build_result(
                    mode="error",
                    answer=str(e),
                    trace=trace,
                    thread_id=session_id,
                    tool_calls=tool_calls,
                )

            pending_action = {
                "type": "mutation_confirmation",
                "tool_calls": tool_calls,
                "prompt": prompt,
            }
            if not self.require_mutation_confirmation or self.pending is None:
                log.errorx(
                    "Mutation tools vereisen confirmatie maar context ondersteunt dit niet",
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_name=assistant_name,
                    require_mutation_confirmation=self.require_mutation_confirmation,
                    has_pending_store=self.pending is not None,
                )
                return build_result(
                    mode="error",
                    answer="Mutation tools require interactive confirmation, which is not available in this execution context.",
                    trace=trace,
                    thread_id=session_id,
                    tool_calls=tool_calls,
                    terminal_state=TERMINAL_FAILED,
                )

            self.pending.set(session_id, pending_action)
            log.infox(
                "Mutation confirmation pending opgeslagen",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool_call_count=len(tool_calls),
            )
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="confirm_prompt",
                level="warn",
                summary="Mutation requires confirmation",
                data={"assistant": assistant_name, "tool_calls": tool_calls},
                progress_cb=progress_cb,
            )
            return build_result(
                mode="confirm_action",
                answer=prompt,
                trace=trace,
                thread_id=session_id,
                pending_action=pending_action,
                terminal_state=TERMINAL_WAITING_FOR_CONFIRMATION,
            )

        cancellation_check = payload.get("_cancellation_check")
        if callable(cancellation_check):
            try:
                cancellation_check()
            except Exception as exc:
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="agent_loop_cancelled",
                    level="warn",
                    summary="Assistant agent loop cancelled before tool execution",
                    data={"assistant": assistant_name, "reason": str(exc)[:500]},
                    progress_cb=progress_cb,
                )
                return build_result(
                    mode="error",
                    answer=str(exc),
                    trace=trace,
                    thread_id=session_id,
                    tool_calls=tool_calls,
                    terminal_state=TERMINAL_CANCELLED,
                )

        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="tool_execution_started",
            summary="Assistant agent loop executing tools",
            data={
                "assistant": assistant_name,
                "tool_count": len(tool_calls or []),
                "tools": [(tc.get("tool") or "").strip() for tc in tool_calls if isinstance(tc, dict)],
            },
            progress_cb=progress_cb,
        )

        try:
            log.infox(
                "Pipeline tool calls uitvoeren gestart",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool_call_count=len(tool_calls or []),
                tool_names=[(tc.get("tool") or "").strip() for tc in tool_calls if isinstance(tc, dict)],
            )
            tool_results = await self.tool_runner.execute_tool_calls(
                tool_calls=tool_calls,
                session_id=session_id,
                turn_id=turn_id,
                trace=trace,
                assistant_name=assistant_name,
                trace_fn=self.trace_fn,
                preview_fn=_preview,
                progress_cb=progress_cb,
                confirmed_tool_call_hashes=confirmed_guarded_tool_hashes,
            )
            log.infox(
                "Pipeline tool calls uitvoeren afgerond",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                tool_call_count=len(tool_calls or []),
                tool_result_count=len(tool_results or []),
            )
        except ValueError as e:
            log.warningx(
                "Pipeline tool calls uitvoeren mislukt met ValueError",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                error=str(e),
            )
            return build_result(
                mode="error",
                answer=str(e),
                trace=trace,
                thread_id=session_id,
                tool_calls=tool_calls,
                terminal_state=TERMINAL_FAILED,
            )

        docs = build_docs_for_tool_calls(tool_calls, tool_results)
        log.infox(
            "Docs gebouwd vanuit tool calls",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            doc_count=len(docs or []),
            doc_paths=[d.get("path") for d in docs[:5] if isinstance(d, dict)],
        )

        failed_results = [tr for tr in (tool_results or []) if _tool_result_failed(tr)]
        recoverable_failures = [tr for tr in failed_results if _tool_result_recoverable(tr)]
        unrecoverable_failures = [tr for tr in failed_results if not _tool_result_recoverable(tr)]
        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="tool_execution_completed",
            summary="Assistant agent loop tool execution completed",
            data={
                "assistant": assistant_name,
                "tool_count": len(tool_calls or []),
                "result_count": len(tool_results or []),
                "failed_count": len(failed_results),
            },
            progress_cb=progress_cb,
        )
        for failed in recoverable_failures:
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="tool_execution_failed_recoverable",
                level="warn",
                summary="Recoverable tool failure observed",
                data={"assistant": assistant_name, "error": _summarize_tool_error(failed)},
                progress_cb=progress_cb,
            )
        for failed in unrecoverable_failures:
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="tool_execution_failed_unrecoverable",
                level="error",
                summary="Unrecoverable tool failure observed",
                data={"assistant": assistant_name, "error": _summarize_tool_error(failed)},
                progress_cb=progress_cb,
            )

        if unrecoverable_failures:
            error_summary = _summarize_tool_error(unrecoverable_failures[0])
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_loop_terminal",
                level="error",
                summary="Assistant agent loop reached terminal state",
                data={"assistant": assistant_name, "terminal_state": TERMINAL_FAILED, "error": error_summary},
                progress_cb=progress_cb,
            )
            return build_result(
                mode="error",
                answer=error_summary.get("message") or "Tool execution failed.",
                trace=trace,
                thread_id=session_id,
                tool_calls=tool_calls,
                tool_results=tool_results,
                docs=docs,
                terminal_state=TERMINAL_FAILED,
                last_error=error_summary,
            )

        repeat_counts = dict(payload.get("_agent_loop_error_repeats") or {})
        for failed in recoverable_failures:
            fingerprint = _tool_error_fingerprint(failed)
            repeat_counts[fingerprint] = int(repeat_counts.get(fingerprint) or 0) + 1
            if repeat_counts[fingerprint] > loop_budgets["max_same_error_repeats"]:
                error_summary = _summarize_tool_error(failed)
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="agent_loop_same_error_repeated",
                    level="error",
                    summary="Assistant agent loop stopped after repeated equivalent tool error",
                    data={"assistant": assistant_name, "repeat_count": repeat_counts[fingerprint], "error": error_summary},
                    progress_cb=progress_cb,
                )
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="agent_loop_budget_exceeded",
                    level="error",
                    summary="Assistant agent loop budget exceeded",
                    data={"assistant": assistant_name, "reason": "same_error_repeated", "error": error_summary},
                    progress_cb=progress_cb,
                )
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="agent_loop_terminal",
                    level="error",
                    summary="Assistant agent loop reached terminal state",
                    data={"assistant": assistant_name, "terminal_state": TERMINAL_BUDGET_EXCEEDED, "reason": "same_error_repeated"},
                    progress_cb=progress_cb,
                )
                return build_result(
                    mode="error",
                    answer="Agent loop stopped because the same recoverable tool error repeated.",
                    trace=trace,
                    thread_id=session_id,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    docs=docs,
                    terminal_state=TERMINAL_BUDGET_EXCEEDED,
                    budget_reason="same_error_repeated",
                    last_error=error_summary,
                )

        total_tool_calls = int(payload.get("_agent_loop_tool_calls") or 0) + len(tool_calls or [])
        if total_tool_calls > loop_budgets["max_tool_calls"]:
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_loop_budget_exceeded",
                level="error",
                summary="Assistant agent loop tool-call budget exceeded",
                data={"assistant": assistant_name, "reason": "max_tool_calls", "tool_calls": total_tool_calls},
                progress_cb=progress_cb,
            )
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_loop_terminal",
                level="error",
                summary="Assistant agent loop reached terminal state",
                data={"assistant": assistant_name, "terminal_state": TERMINAL_BUDGET_EXCEEDED, "reason": "max_tool_calls"},
                progress_cb=progress_cb,
            )
            return build_result(
                mode="error",
                answer="Agent loop budget exceeded: max_tool_calls.",
                trace=trace,
                thread_id=session_id,
                tool_calls=tool_calls,
                tool_results=tool_results,
                docs=docs,
                terminal_state=TERMINAL_BUDGET_EXCEEDED,
                budget_reason="max_tool_calls",
            )

        if tool_calls:
            next_payload = dict(payload or {})
            next_payload["_remaining_eval_hops"] = max(0, remaining_eval_hops - 1)
            next_payload["_remaining_tool_budget"] = max(0, remaining_tool_budget - len(tool_calls))
            next_payload["_used_evaluate"] = True
            next_payload["_agent_loop_tool_calls"] = total_tool_calls
            next_payload["_agent_loop_error_repeats"] = repeat_counts
            next_payload["_text_search_used"] = payload.get("_text_search_used", False) or any(
                tc["tool"] == "text_search" for tc in tool_calls
            )
            next_payload["_last_tool_calls"] = tool_calls
            next_payload["_last_tool_results"] = tool_results
            next_payload["_last_docs"] = docs
            next_payload["_acc_tool_calls"] = (
                list(next_payload.get("_acc_tool_calls") or [])
                + [_compact_tool_call(tc) for tc in tool_calls]
            )[-20:]
            next_payload["_acc_tool_results"] = (
                list(next_payload.get("_acc_tool_results") or [])
                + [_compact_tool_result(tr, max_chars=1200) for tr in tool_results]
            )[-20:]
            next_payload["_acc_docs"] = (
                list(next_payload.get("_acc_docs") or [])
                + [_compact_doc(d, max_chars=2500) for d in docs]
            )[-10:]
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="agent_loop_continue_after_tool_result",
                summary="Assistant agent loop continuing after tool observations",
                data={
                    "assistant": assistant_name,
                    "next_iteration": loop_iteration + 1,
                    "tool_calls_used": total_tool_calls,
                    "failed_count": len(failed_results),
                },
                progress_cb=progress_cb,
            )
            cancellation_check = payload.get("_cancellation_check")
            if callable(cancellation_check):
                try:
                    cancellation_check()
                except Exception as exc:
                    self.trace_fn(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="agent_loop_cancelled",
                        level="warn",
                        summary="Assistant agent loop cancelled after tool execution",
                        data={"assistant": assistant_name, "reason": str(exc)[:500]},
                        progress_cb=progress_cb,
                    )
                    return build_result(
                        mode="error",
                        answer=str(exc),
                        trace=trace,
                        thread_id=session_id,
                        tool_calls=tool_calls,
                        tool_results=tool_results,
                        docs=docs,
                        terminal_state=TERMINAL_CANCELLED,
                    )
            return await self.run(
                assistant=assistant,
                question=question,
                model=model,
                payload=next_payload,
                session_id=session_id,
                turn_id=turn_id,
                trace=trace,
                progress_cb=progress_cb,
            )

        if response_mode == "emit_handoff":
            log.infox(
                "Pipeline emit_handoff response_mode verwerken",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                doc_count=len(docs),
            )
            handoff = _extract_downstream_handoff(plan, "", docs)
            handoff = self._store_large_handoff_content_if_needed(
                handoff=handoff,
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                trace=trace,
            )

            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="assistant_turn_end",
                summary="Assistant completed (emit_handoff)",
                data={"assistant": assistant_name, "mode": "emit_handoff"},
                progress_cb=progress_cb,
            )

            log.infox(
                "Pipeline emit_handoff afgerond",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                handoff_keys=list(handoff.keys()) if isinstance(handoff, dict) else None,
            )
            return build_result(
                mode="emit_handoff",
                answer="",
                trace=trace,
                thread_id=session_id,
                tool_calls=tool_calls,
                tool_results=tool_results,
                docs=docs,
                downstream_handoff=handoff,
            )

        if response_mode == "evaluate_answer" and remaining_eval_hops > 0:
            log.infox(
                "Pipeline evaluate_answer vervolg-hop gestart",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                remaining_eval_hops=remaining_eval_hops,
                remaining_tool_budget=remaining_tool_budget,
                tool_call_count=len(tool_calls),
            )
            next_payload = dict(payload or {})
            next_payload["_remaining_eval_hops"] = remaining_eval_hops - 1
            next_payload["_remaining_tool_budget"] = remaining_tool_budget - len(tool_calls)
            next_payload["_used_evaluate"] = True
            next_payload["_text_search_used"] = payload.get("_text_search_used", False) or any(
                tc["tool"] == "text_search" for tc in tool_calls
            )
            # Keep the immediate last results full enough for the next planner pass.
            # This is the same assistant evaluating its own tool results.
            next_payload["_last_tool_calls"] = tool_calls
            next_payload["_last_tool_results"] = tool_results
            next_payload["_last_docs"] = docs

            # Only compact accumulated history from older hops.
            next_payload["_acc_tool_calls"] = (
                    list(next_payload.get("_acc_tool_calls") or [])
                    + [_compact_tool_call(tc) for tc in tool_calls]
            )[-20:]

            next_payload["_acc_tool_results"] = (
                    list(next_payload.get("_acc_tool_results") or [])
                    + [_compact_tool_result(tr, max_chars=1200) for tr in tool_results]
            )[-20:]

            next_payload["_acc_docs"] = (
                    list(next_payload.get("_acc_docs") or [])
                    + [_compact_doc(d, max_chars=2500) for d in docs]
            )[-10:]

            log.debugx(
                "Pipeline evaluate_answer next_payload opgebouwd",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                next_remaining_eval_hops=next_payload.get("_remaining_eval_hops"),
                next_remaining_tool_budget=next_payload.get("_remaining_tool_budget"),
                acc_tool_call_count=len(next_payload.get("_acc_tool_calls") or []),
                acc_tool_result_count=len(next_payload.get("_acc_tool_results") or []),
                acc_doc_count=len(next_payload.get("_acc_docs") or []),
                text_search_used=next_payload.get("_text_search_used"),
            )

            return await self.run(
                assistant=assistant,
                question=question,
                model=model,
                payload=next_payload,
                session_id=session_id,
                turn_id=turn_id,
                trace=trace,
            )

        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="docs_built",
            summary=f"Built {len(docs)} docs",
            data={
                "assistant": assistant_name,
                "docs_count": len(docs),
                "docs_preview": [{"path": d.get("path"), "meta": d.get("meta"), "doc_id": d.get("doc_id")} for d in docs[:5]],
            },
            progress_cb=progress_cb,
        )

        if response_mode == "return_file":
            log.infox(
                "Pipeline return_file response_mode verwerken",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                doc_count=len(docs),
            )
            answer = format_return_file_answer(tool_calls=tool_calls, tool_results=tool_results)
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="assistant_turn_end",
                summary="Assistant completed (return_file)",
                data={"assistant": assistant_name, "mode": "return_file", "answer_preview": answer[:300]},
                progress_cb=progress_cb,
            )
            result_handoff = _extract_downstream_handoff(plan, answer, docs)
            log.infox(
                "Pipeline return_file afgerond",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                answer_length=len(answer or ""),
                handoff_keys=list(result_handoff.keys()) if isinstance(result_handoff, dict) else None,
            )
            return build_result(
                mode="return_file",
                answer=answer,
                trace=trace,
                thread_id=session_id,
                tool_calls=tool_calls,
                tool_results=tool_results,
                docs=docs,
                downstream_handoff=result_handoff,
            )

        all_calls = payload.get("_acc_tool_calls") or [_compact_tool_call(tc) for tc in tool_calls]
        all_results = payload.get("_acc_tool_results") or [_compact_tool_result(tr, max_chars=800) for tr in tool_results]
        all_docs = payload.get("_acc_docs") or [_compact_doc(d, max_chars=2000) for d in docs]

        log.infox(
            "Pipeline final writer voorbereiden",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            all_call_count=len(all_calls or []),
            all_result_count=len(all_results or []),
            all_doc_count=len(all_docs or []),
            used_acc_calls=bool(payload.get("_acc_tool_calls")),
            used_acc_results=bool(payload.get("_acc_tool_results")),
            used_acc_docs=bool(payload.get("_acc_docs")),
        )

        writer = self.runtime.get_final_answer_runtime_assistant()
        write_prompt = writer.prompt(
            question,
            tool_name=";".join([tc.get("tool", "") for tc in all_calls if isinstance(tc, dict)]),
            tool_args=[tc.get("args") for tc in all_calls],
            tool_result=all_results,
            docs=all_docs,
        )
        log.infox(
            "Pipeline final writer prompt gebouwd",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            writer_name=getattr(writer, "name", type(writer).__name__),
            prompt_length=len(write_prompt or ""),
            model=model,
        )
        writer_role = f"writer:{assistant_name}:{turn_id}"
        writer_metadata = {
            "kind": "assistant_writer",
            "assistant": assistant_name,
            "turn_id": str(turn_id),
        }

        # Stream the free-text answer so the user sees it build up live: accumulate deltas
        # and push answer_partial straight to the run status (not the audit trace) every
        # ~350ms. Any failure / unsupported provider → fall back to the single non-streaming
        # call below, so a streaming bug can never break a turn (worst case = today).
        raw_answer = ""
        streamed_ok = False
        try:
            acc: list[str] = []
            last_emit = 0.0
            async for delta in self.openai.ask_orchestration_stream(
                write_prompt,
                role=writer_role,
                instructions=writer.instructions,
                model=model,
                max_output_tokens=8000,
                metadata=writer_metadata,
            ):
                acc.append(delta)
                if progress_cb is not None:
                    now = time.monotonic()
                    if now - last_emit >= 0.35:
                        last_emit = now
                        try:
                            progress_cb({
                                "type": "answer_partial",
                                "turn_id": turn_id,
                                "assistant": assistant_name,
                                "partial_answer": "".join(acc),
                            })
                        except Exception:  # noqa: BLE001 — partial UI update must never break the turn
                            pass
            raw_answer = "".join(acc).strip()
            streamed_ok = bool(raw_answer)
        except Exception as exc:  # noqa: BLE001 — fall back to the non-streaming writer
            log.warningx(
                "Streaming writer mislukt; val terug op non-streaming",
                session_id=session_id, turn_id=turn_id, assistant_name=assistant_name,
                error=str(exc),
            )

        if not streamed_ok:
            write_resp = await self.openai.ask_orchestration_async(
                write_prompt,
                role=writer_role,
                instructions=writer.instructions,
                keep_context=False,
                store=False,
                session_id=session_id,
                model=model,
                max_output_tokens=8000,
                metadata=writer_metadata,
            )
            raw_answer = (write_resp.text or "").strip()

        log.infox(
            "Pipeline final writer response ontvangen",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            writer_name=getattr(writer, "name", type(writer).__name__),
            streamed=streamed_ok,
            response_text_length=len(raw_answer),
        )
        extracted = _extract_final_answer_if_json(raw_answer)
        if extracted:
            log.debugx(
                "Pipeline final writer answer uit JSON geëxtraheerd",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                extracted_length=len(extracted),
            )
            answer_text = extracted
        elif _looks_like_planner_json(raw_answer):
            log.warningx(
                "Pipeline final writer answer lijkt planner JSON, fallback wordt gebruikt",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                raw_answer_length=len(raw_answer),
            )
            answer_text = _fallback_no_evidence_message()
        else:
            answer_text = raw_answer or "(no answer)"
            log.debugx(
                "Pipeline final writer answer bepaald",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                answer_length=len(answer_text),
                used_no_answer=not bool(raw_answer),
            )

        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="writer",
            summary="Writer produced final answer",
            data={"assistant": assistant_name, "answer_preview": (answer_text or "")[:600]},
            progress_cb=progress_cb,
        )
        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="assistant_turn_end",
            summary="Assistant completed (synthesize_answer)",
            data={"assistant": assistant_name, "mode": "synthesize_answer"},
            progress_cb=progress_cb,
        )

        downstream_handoff = _extract_downstream_handoff(plan, answer_text, docs)
        log.infox(
            "Assistant pipeline run afgerond",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            mode="synthesize_answer",
            answer_length=len(answer_text or ""),
            tool_call_count=len(all_calls or []),
            tool_result_count=len(all_results or []),
            doc_count=len(all_docs or []),
            handoff_keys=list(downstream_handoff.keys()) if isinstance(downstream_handoff, dict) else None,
        )
        return build_result(
            mode="synthesize_answer",
            answer=answer_text,
            trace=trace,
            thread_id=session_id,
            tool_calls=all_calls,
            tool_results=all_results,
            docs=all_docs,
            downstream_handoff=downstream_handoff,
        )

    async def run(self, **kwargs):
        """
        Fallback wrapper to run the assistant from new implementations.
        """
        log.debugx(
            "AssistantPipelineRunner.run wrapper gestart",
            kwarg_keys=list(kwargs.keys()),
            assistant_name=getattr(kwargs.get("assistant"), "name", type(kwargs.get("assistant")).__name__) if kwargs.get("assistant") is not None else None,
            session_id=kwargs.get("session_id"),
            turn_id=kwargs.get("turn_id"),
        )
        result = await self._run_assistant_pipeline(**kwargs)
        log.debugx(
            "AssistantPipelineRunner.run wrapper afgerond",
            result_mode=result.get("mode") if isinstance(result, dict) else None,
            answer_length=len((result.get("answer") or "") if isinstance(result, dict) else ""),
        )
        return result