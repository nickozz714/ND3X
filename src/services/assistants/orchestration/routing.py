from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from component.logging import get_logger
from services.assistants.orchestration.formatting import (
    _compact_router_history,
    _compact_step_history_entry,
    build_result,
)

log = get_logger(__name__)

ProgressCallback = Optional[Any]


def _step_id(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe_step_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[int] = set()
    deduped: List[Dict[str, Any]] = []

    for item in results or []:
        if not isinstance(item, dict):
            continue

        sid = _step_id(item.get("step"))

        if sid is None:
            deduped.append(item)
            continue

        if sid in seen:
            continue

        seen.add(sid)
        deduped.append(item)

    return deduped


def _handoff_status(result: Dict[str, Any]) -> Optional[str]:
    handoff = result.get("downstream_handoff")
    if not isinstance(handoff, dict):
        return None

    status = handoff.get("status")
    return str(status).strip().lower() if status is not None else None


def _handoff_has_open_questions(result: Dict[str, Any]) -> bool:
    handoff = result.get("downstream_handoff")
    if not isinstance(handoff, dict):
        return False

    open_questions = handoff.get("open_questions") or []
    return bool(open_questions)


def _result_requires_router_replan(result: Dict[str, Any]) -> bool:
    mode = (result.get("mode") or "").strip().lower()

    if mode in {"ask_user", "confirm_action", "error"}:
        return True

    handoff_status = _handoff_status(result)

    if handoff_status in {"partial", "failed", "error", "blocked"}:
        return True

    if _handoff_has_open_questions(result):
        return True

    return False

class RouterWorkflow:
    def __init__(
        self,
        *,
        runtime_resolver,
        openai,
        output_validator,
        workflow_service,
        run_assistant_pipeline: Callable[..., Any],
        trace_fn: Callable[..., None],
    ):
        log.infox(
            "RouterWorkflow initialiseren",
            has_runtime_resolver=runtime_resolver is not None,
            has_openai=openai is not None,
            has_output_validator=output_validator is not None,
            has_workflow_service=workflow_service is not None,
            has_run_assistant_pipeline=run_assistant_pipeline is not None,
            has_trace_fn=trace_fn is not None,
        )
        self.runtime = runtime_resolver
        self.openai = openai
        self.output_validator = output_validator
        self.run_assistant_pipeline = run_assistant_pipeline
        self.trace_fn = trace_fn
        self.workflow_service = workflow_service
        log.infox("RouterWorkflow geïnitialiseerd")

    def build_router_payload(self, *, payload: Dict[str, Any], thread_id: Optional[str]) -> Dict[str, Any]:
        log.infox(
            "Router payload bouwen gestart",
            thread_id=thread_id,
            input_payload_keys=list((payload or {}).keys()),
        )
        p = dict(payload or {})
        p.setdefault("current_assistant", p.get("_current_assistant"))
        p.setdefault("force_assistant", p.get("_force_assistant"))
        p.setdefault("allow_multi_assistant", True)
        p.setdefault("_conversation_id", thread_id)
        log.infox(
            "Router payload bouwen afgerond",
            thread_id=thread_id,
            payload_keys=list(p.keys()),
            current_assistant=p.get("current_assistant"),
            force_assistant=p.get("force_assistant"),
            allow_multi_assistant=p.get("allow_multi_assistant"),
            conversation_id=p.get("_conversation_id"),
        )
        return p

    async def route_request(
        self,
        *,
        question: str,
        payload: Dict[str, Any],
        session_id: Optional[str],
        model: Optional[str],
        trace: List[dict],
        turn_id: int,
        progress_cb: ProgressCallback = None,
    ) -> Dict[str, Any]:
        log.infox(
            "Router request starten",
            session_id=session_id,
            turn_id=turn_id,
            question_length=len(question or ""),
            payload_keys=list((payload or {}).keys()),
            model=model,
            has_progress_cb=progress_cb is not None,
        )
        router = self.runtime.get_router_runtime_assistant()
        log.debugx(
            "Router runtime assistant opgehaald",
            session_id=session_id,
            turn_id=turn_id,
            router_name=getattr(router, "name", type(router).__name__),
            router_type=type(router).__name__,
        )

        router_payload = dict(payload or {})
        router_payload["_available_assistants"] = self.runtime.runtime_loader.list_router_selectable()
        router_payload["_available_workflows"] = self.workflow_service.get_all(
            skip=0,
            limit=100,
            include_disabled=False,
        )

        log.infox(
            "Router beschikbare context geladen",
            session_id=session_id,
            turn_id=turn_id,
            available_assistant_count=len(router_payload.get("_available_assistants") or []),
            available_workflow_count=len(router_payload.get("_available_workflows") or []),
            router_payload_keys=list(router_payload.keys()),
        )

        router_prompt = router.prompt(question=question, **router_payload)
        log.infox(
            "Router prompt gebouwd",
            session_id=session_id,
            turn_id=turn_id,
            router_name=getattr(router, "name", type(router).__name__),
            prompt_length=len(router_prompt or ""),
            model=model,
        )

        router_resp = await self.openai.ask_orchestration_async(
            router_prompt,
            role=f"router:{turn_id}",
            instructions=router.instructions,
            keep_context=False,
            store=False,
            session_id=session_id,
            model=model,
            max_output_tokens=3000,
            json_schema=getattr(router.config, "schema", None),
            metadata={
                "kind": "router",
                "turn_id": str(turn_id),
            },
        )
        log.infox(
            "Router OpenAI response ontvangen",
            session_id=session_id,
            turn_id=turn_id,
            router_name=getattr(router, "name", type(router).__name__),
            response_text_length=len(getattr(router_resp, "text", "") or ""),
        )

        route = router.extract_first_json_object(router_resp.text)
        log.infox(
            "Router response JSON geëxtraheerd",
            session_id=session_id,
            turn_id=turn_id,
            route_type=type(route).__name__,
            route_keys=list(route.keys()) if isinstance(route, dict) else None,
            route_mode=route.get("mode") if isinstance(route, dict) else None,
        )

        try:
            self.output_validator.validate(router.config.schema, route)
            log.debugx(
                "Router output validatie geslaagd",
                session_id=session_id,
                turn_id=turn_id,
                route_mode=route.get("mode") if isinstance(route, dict) else None,
            )
        except Exception:
            log.warningx(
                "Router output validatie mislukt maar wordt genegeerd",
                session_id=session_id,
                turn_id=turn_id,
                route_mode=route.get("mode") if isinstance(route, dict) else None,
            )

        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="router_plan",
            summary=f"Router produced mode={(route.get('mode') or '').strip()}",
            data={"route": route},
            progress_cb=progress_cb,
        )
        log.infox(
            "Router request afgerond",
            session_id=session_id,
            turn_id=turn_id,
            route_mode=(route.get("mode") or "").strip(),
            step_count=len(route.get("steps") or []) if isinstance(route, dict) else None,
            trace_count=len(trace or []),
        )
        return route

    # ------------------------------------------------------------------
    # Single-agent path: skill selection is folded INTO the agent loop (no separate
    # selector call). See run_single_agent + the pipeline's action='select_skills' handler.
    # ------------------------------------------------------------------
    async def run_single_agent(
        self,
        *,
        question: str,
        payload: Dict[str, Any],
        session_id: Optional[str],
        model: Optional[str],
        trace: List[dict],
        turn_id: int,
        progress_cb: ProgressCallback = None,
    ) -> Dict[str, Any]:
        """Single-agent turn: the ONE agent loop decides everything itself — answer
        directly, ask, select skill(s) (which loads their tools), or use builtin tools.
        No separate selector call: skill choice is folded into the agent's first step
        (driven by _needs_skill_selection → the skill catalog in the planner prompt)."""
        agent = self.runtime.get_single_agent_runtime_assistant()
        agent_payload = dict(payload or {})
        agent_payload["_needs_skill_selection"] = True
        agent_payload.setdefault("_selected_skill_names", [])

        log.infox(
            "Single-agent pipeline gestart (merged selectie in de loop)",
            session_id=session_id,
            turn_id=turn_id,
        )
        result = await self.run_assistant_pipeline(
            assistant=agent,
            question=question,
            model=model,
            payload=agent_payload,
            session_id=session_id,
            turn_id=turn_id,
            trace=trace,
            progress_cb=progress_cb,
        )
        return result

    async def execute_router_plan(
        self,
        *,
        route: Dict[str, Any],
        question: str,
        payload: Dict[str, Any],
        session_id: Optional[str],
        model: Optional[str],
        trace: List[dict],
        turn_id: int,
        progress_cb: ProgressCallback = None,
    ) -> Dict[str, Any]:
        mode = (route.get("mode") or "").strip()
        steps = route.get("steps") or []

        log.infox(
            "Router plan uitvoeren gestart",
            session_id=session_id,
            turn_id=turn_id,
            mode=mode,
            step_count=len(steps) if isinstance(steps, list) else None,
            question_length=len(question or ""),
            payload_keys=list((payload or {}).keys()),
            model=model,
            has_progress_cb=progress_cb is not None,
        )

        ask_text = (route.get("ask_user") or "").strip()

        # A workflow mode without a concrete workflow_id is invalid (the spec
        # requires an integer workflow_id). Smaller/local models sometimes pick
        # workflow_offer for what is really a clarification and still populate
        # ask_user — coerce to ask_user so the question reaches the user instead of
        # being dropped behind the workflow branch. (audit 38cd2004)
        if mode in {"workflow_offer", "workflow_trigger"} and not route.get("workflow_id") and ask_text:
            log.infox(
                "Workflow mode zonder workflow_id met ask_user → ask_user",
                session_id=session_id,
                turn_id=turn_id,
                original_mode=mode,
            )
            mode = "ask_user"

        # Direct-answer mode: the router answered a basic question itself, with no
        # skills/assistants/tools needed. Short-circuit straight to the user.
        if mode in {"direct_answer", "answer"}:
            answer = (route.get("answer") or route.get("ask_user") or route.get("reason") or "").strip()
            log.infox(
                "Router plan beantwoordt direct",
                session_id=session_id,
                turn_id=turn_id,
                answer_length=len(answer),
            )
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="turn_end",
                summary="Turn completed (router direct answer)",
                data={"mode": "direct_answer", "answer_preview": answer[:400]},
                progress_cb=progress_cb,
            )
            return build_result(
                mode="answer",
                answer=answer,
                trace=trace,
                thread_id=session_id,
                router_plan=route,
                executed_steps=[],
            )

        if mode in {"workflow_offer", "workflow_trigger"}:
            log.infox(
                "Router plan is workflow mode",
                session_id=session_id,
                turn_id=turn_id,
                mode=mode,
                workflow_id=route.get("workflow_id"),
                input_payload_keys=list((route.get("input_payload") or {}).keys()) if isinstance(route.get("input_payload") or {}, dict) else None,
            )
            return build_result(
                mode=mode,
                answer=(route.get("reason") or route.get("answer") or "").strip(),
                trace=trace,
                thread_id=session_id,
                router_plan=route,
                workflow_id=route.get("workflow_id"),
                input_payload=route.get("input_payload") or {},
            )

        if mode == "ask_user":
            answer = (route.get("ask_user") or "").strip()
            log.infox(
                "Router plan vraagt input van gebruiker",
                session_id=session_id,
                turn_id=turn_id,
                answer_length=len(answer),
            )
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="turn_end",
                summary="Turn completed (router ask_user)",
                data={"mode": "ask_user", "answer_preview": answer[:400]},
                progress_cb=progress_cb,
            )

            result = build_result(
                mode="ask_user",
                answer=answer,
                trace=trace,
                thread_id=session_id,
                router_plan=route,
                executed_steps=[],
            )
            log.infox(
                "Router ask_user resultaat gebouwd",
                session_id=session_id,
                turn_id=turn_id,
                answer_length=len(answer),
            )
            return result

        if not isinstance(steps, list) or not steps:
            log.errorx(
                "Router plan bevat geen uitvoerbare stappen",
                session_id=session_id,
                turn_id=turn_id,
                mode=mode,
                steps_type=type(steps).__name__,
            )
            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="error",
                level="error",
                summary="Router returned no executable steps",
                data={"route": route},
                progress_cb=progress_cb,
            )
            return build_result(
                mode="error",
                answer="Router returned no executable steps.",
                trace=trace,
                thread_id=session_id,
                downstream_handoff=None,
                router_plan=route,
                executed_steps=[],
            )

        previous_step_results = payload.get("previous_step_results") or []
        if not isinstance(previous_step_results, list):
            previous_step_results = []

        previous_step_results = _dedupe_step_results(previous_step_results)

        completed_step_ids = {
            _step_id(item.get("step"))
            for item in previous_step_results
            if isinstance(item, dict) and item.get("status") == "success"
        }
        completed_step_ids.discard(None)

        executed_steps: List[Dict[str, Any]] = []

        for step in steps:
            step_no = _step_id(step.get("step"))

            if step_no in completed_step_ids and not bool(step.get("force_rerun", False)):
                log.infox(
                    "Router workflow stap overgeslagen omdat deze al succesvol is uitgevoerd",
                    session_id=session_id,
                    turn_id=turn_id,
                    step=step.get("step"),
                    assistant_name=step.get("assistant_name"),
                    completed_step_ids=sorted(completed_step_ids),
                )
                continue
            assistant_id = step.get("assistant_id")
            assistant_name = step.get("assistant_name")
            question_for_assistant = (step.get("question_for_assistant") or "").strip()
            selected_skill_names = step.get("skill_names") or []

            if not isinstance(selected_skill_names, list):
                return build_result(
                    mode="error",
                    answer=f"Router step {step.get('step')} returned invalid skill_names. Expected array of strings.",
                    trace=trace,
                    thread_id=session_id,
                    router_plan=route,
                    executed_steps=executed_steps,
                )

            selected_skill_names = [
                str(name).strip()
                for name in selected_skill_names
                if str(name).strip()
            ]

            if not selected_skill_names:
                return build_result(
                    mode="error",
                    answer=(
                        f"Router step {step.get('step')} did not include skill_names. "
                        "Every executable assistant step requires at least one skill."
                    ),
                    trace=trace,
                    thread_id=session_id,
                    router_plan=route,
                    executed_steps=executed_steps,
                )
            router_after_step = bool(step.get("router_after_step", False))
            requires_previous_output = bool(step.get("requires_previous_output", False))
            previous_output_from_steps = step.get("previous_output_from_steps") or []

            log.infox(
                "Router workflow stap voorbereiden",
                session_id=session_id,
                turn_id=turn_id,
                step=step.get("step"),
                assistant_id=assistant_id,
                assistant_name=assistant_name,
                question_for_assistant_length=len(question_for_assistant),
                router_after_step=router_after_step,
                requires_previous_output=requires_previous_output,
                previous_output_from_steps=previous_output_from_steps,
                executed_step_count=len(executed_steps),
            )

            try:
                assistant = self.runtime.get_runtime_assistant_by_id_or_name(assistant_id, assistant_name)
                allowed_skill_names = {
                    skill.name
                    for skill in (assistant.config.skills or [])
                    if skill.is_enabled and not skill.is_system
                }

                missing_skills = [
                    name for name in selected_skill_names
                    if name not in allowed_skill_names
                ]

                if missing_skills:
                    return build_result(
                        mode="error",
                        answer=(
                            f"Router selected skill(s) not allowed for assistant "
                            f"'{assistant.name}': {missing_skills}. "
                            f"Allowed skills: {sorted(allowed_skill_names)}"
                        ),
                        trace=trace,
                        thread_id=session_id,
                        router_plan=route,
                        executed_steps=executed_steps,
                    )
                log.infox(
                    "Runtime assistant voor router stap gevonden",
                    session_id=session_id,
                    turn_id=turn_id,
                    step=step.get("step"),
                    assistant_id=assistant_id,
                    requested_assistant_name=assistant_name,
                    resolved_assistant_name=getattr(assistant, "name", None),
                    assistant_type=type(assistant).__name__,
                )
            except ValueError as e:
                log.errorx(
                    "Router refereert naar onbekende assistant",
                    session_id=session_id,
                    turn_id=turn_id,
                    step=step.get("step"),
                    assistant_id=assistant_id,
                    assistant_name=assistant_name,
                    error=str(e),
                )
                self.trace_fn(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="error",
                    level="error",
                    summary="Router referenced unknown assistant",
                    data={"assistant_name": assistant_name, "error": str(e)},
                    progress_cb=progress_cb,
                )
                return build_result(
                    mode="error",
                    answer=str(e),
                    trace=trace,
                    thread_id=session_id,
                    router_plan=route,
                    executed_steps=executed_steps,
                )

            step_payload = dict(payload or {})
            step_payload["_current_assistant"] = getattr(assistant, "name", assistant_name)
            step_payload["_router_plan"] = route
            step_payload["_workflow_step"] = step.get("step")
            step_payload["_workflow_goal"] = step.get("goal")
            step_payload["_selected_skill_names"] = selected_skill_names

            if requires_previous_output:
                available_step_results = _dedupe_step_results(previous_step_results + executed_steps)

                wanted_steps = {
                    _step_id(value)
                    for value in previous_output_from_steps
                }
                wanted_steps.discard(None)

                prior_results = [
                    s for s in available_step_results
                    if _step_id(s.get("step")) in wanted_steps
                ]
                step_payload["_previous_step_results"] = [_compact_step_history_entry(s) for s in prior_results]
                log.debugx(
                    "Vorige step resultaten toegevoegd aan step payload",
                    session_id=session_id,
                    turn_id=turn_id,
                    step=step.get("step"),
                    requested_previous_steps=previous_output_from_steps,
                    prior_result_count=len(prior_results),
                    compact_prior_result_count=len(step_payload.get("_previous_step_results") or []),
                )

            log.debugx(
                "Step payload gebouwd",
                session_id=session_id,
                turn_id=turn_id,
                step=step.get("step"),
                assistant=getattr(assistant, "name", assistant_name),
                step_payload_keys=list(step_payload.keys()),
            )

            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="assistant_step_start",
                summary=f"Executing step {step.get('step')} with {assistant.name}",
                data={"step": step},
                progress_cb=progress_cb,
            )

            log.infox(
                "Assistant pipeline voor router stap gestart",
                session_id=session_id,
                turn_id=turn_id,
                step=step.get("step"),
                assistant=getattr(assistant, "name", assistant_name),
                question_length=len(question_for_assistant or question or ""),
            )
            result = await self.run_assistant_pipeline(
                assistant=assistant,
                question=question_for_assistant or question,
                model=model,
                payload=step_payload,
                session_id=session_id,
                turn_id=turn_id,
                trace=trace,
            )
            log.infox(
                "Assistant pipeline voor router stap afgerond",
                session_id=session_id,
                turn_id=turn_id,
                step=step.get("step"),
                assistant=getattr(assistant, "name", assistant_name),
                result_mode=result.get("mode") if isinstance(result, dict) else None,
                answer_length=len((result.get("answer") or "") if isinstance(result, dict) else ""),
                has_downstream_handoff=bool(result.get("downstream_handoff")) if isinstance(result, dict) else None,
            )

            step_result_entry = {
                "step": step.get("step"),
                "assistant": getattr(assistant, "name", assistant_name),
                "status": "success" if result.get("mode") != "error" else "error",
                "skills": selected_skill_names,
                "downstream_handoff": result.get("downstream_handoff"),
            }
            executed_steps.append(step_result_entry)

            log.debugx(
                "Router step result entry opgeslagen",
                session_id=session_id,
                turn_id=turn_id,
                step=step.get("step"),
                assistant=getattr(assistant, "name", assistant_name),
                status=step_result_entry.get("status"),
                executed_step_count=len(executed_steps),
            )

            self.trace_fn(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="assistant_step_end",
                summary=f"Finished step {step.get('step')} with {assistant.name}",
                data={
                    "step": step.get("step"),
                    "assistant": assistant_name,
                    "result_mode": result.get("mode"),
                    "answer_preview": (result.get("answer") or "")[:300],
                },
                progress_cb=progress_cb,
            )

            if result.get("mode") in ("ask_user", "confirm_action", "error"):
                log.infox(
                    "Router workflow stopt vroeg door resultaat mode",
                    session_id=session_id,
                    turn_id=turn_id,
                    step=step.get("step"),
                    result_mode=result.get("mode"),
                    executed_step_count=len(executed_steps),
                )
                result["router_plan"] = route
                result["executed_steps"] = executed_steps
                return result

            should_replan = router_after_step and _result_requires_router_replan(result)

            if router_after_step and not should_replan:
                log.infox(
                    "Router re-entry overgeslagen omdat stap succesvol en verwacht is afgerond",
                    session_id=session_id,
                    turn_id=turn_id,
                    step=step.get("step"),
                    assistant=assistant_name,
                    result_mode=result.get("mode"),
                    handoff_status=_handoff_status(result),
                    has_open_questions=_handoff_has_open_questions(result),
                )

            if should_replan:
                log.infox(
                    "Router opnieuw aanroepen na afwijkend stapresultaat",
                    session_id=session_id,
                    turn_id=turn_id,
                    step=step.get("step"),
                    assistant=assistant_name,
                    result_mode=result.get("mode"),
                    handoff_status=_handoff_status(result),
                    executed_step_count=len(executed_steps),
                )

                next_router_payload = dict(payload or {})
                next_router_payload["current_assistant"] = assistant_name
                next_router_payload["previous_router_plan"] = {
                    "mode": route.get("mode"),
                    "steps": route.get("steps"),
                    "ask_user": route.get("ask_user"),
                }

                combined_step_results = _dedupe_step_results(
                    previous_step_results + _compact_router_history(executed_steps)
                )

                completed_steps = [
                    _step_id(item.get("step"))
                    for item in combined_step_results
                    if isinstance(item, dict) and item.get("status") == "success"
                ]
                completed_steps = [step for step in completed_steps if step is not None]
                completed_step_set = set(completed_steps)

                next_router_payload["previous_step_results"] = combined_step_results
                next_router_payload["completed_steps"] = completed_steps
                next_router_payload["remaining_steps"] = [
                    item
                    for item in steps
                    if _step_id(item.get("step")) not in completed_step_set
                ]
                next_router_payload["router_replan_reason"] = {
                    "step": step.get("step"),
                    "assistant_name": assistant_name,
                    "result_mode": result.get("mode"),
                    "handoff_status": _handoff_status(result),
                    "has_open_questions": _handoff_has_open_questions(result),
                }

                log.debugx(
                    "Next router payload gebouwd na stap",
                    session_id=session_id,
                    turn_id=turn_id,
                    step=step.get("step"),
                    next_router_payload_keys=list(next_router_payload.keys()),
                    previous_step_result_count=len(next_router_payload.get("previous_step_results") or []),
                )
                router_replan_count = int((payload or {}).get("_router_replan_count") or 0)

                if router_replan_count >= 5:
                    return build_result(
                        mode="error",
                        answer="Router replanning exceeded maximum allowed iterations.",
                        trace=trace,
                        thread_id=session_id,
                        router_plan=route,
                        executed_steps=executed_steps,
                    )

                next_router_payload["_router_replan_count"] = router_replan_count + 1

                next_route = await self.route_request(
                    question=question,
                    payload=next_router_payload,
                    session_id=session_id,
                    model=model,
                    trace=trace,
                    turn_id=turn_id,
                )

                log.infox(
                    "Nieuwe router route ontvangen na stap",
                    session_id=session_id,
                    turn_id=turn_id,
                    step=step.get("step"),
                    next_route_mode=next_route.get("mode") if isinstance(next_route, dict) else None,
                )

                chained_result = await self.execute_router_plan(
                    route=next_route,
                    question=question,
                    payload=next_router_payload,
                    session_id=session_id,
                    model=model,
                    trace=trace,
                    turn_id=turn_id,
                )

                chained_result["executed_steps"] = _dedupe_step_results(
                    executed_steps + (chained_result.get("executed_steps") or [])
                )
                log.infox(
                    "Chained router resultaat opgebouwd",
                    session_id=session_id,
                    turn_id=turn_id,
                    result_mode=chained_result.get("mode") if isinstance(chained_result, dict) else None,
                    total_executed_step_count=len(chained_result.get("executed_steps") or []),
                )
                return chained_result

        self.trace_fn(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="turn_end",
            summary="Turn completed (router workflow)",
            data={
                "mode": "workflow_finalize",
                "executed_steps": len(executed_steps),
                "answer_preview": "",
            },
            progress_cb=progress_cb,
        )

        workflow_handoffs = [
            s.get("downstream_handoff")
            for s in executed_steps
            if isinstance(s.get("downstream_handoff"), dict)
        ]

        log.infox(
            "Router workflow afgerond, workflow_finalize resultaat bouwen",
            session_id=session_id,
            turn_id=turn_id,
            executed_step_count=len(executed_steps),
            workflow_handoff_count=len(workflow_handoffs),
        )

        return build_result(
            mode="workflow_finalize",
            answer="",
            trace=trace,
            thread_id=session_id,
            router_plan=route,
            executed_steps=executed_steps,
            workflow_handoffs=workflow_handoffs,
        )


def format_router_plan_for_approval(route: Dict[str, Any]) -> str:
    log.infox(
        "Router plan approval prompt formatteren gestart",
        route_type=type(route).__name__,
        route_keys=list(route.keys()) if isinstance(route, dict) else None,
        mode=(route.get("mode") or "").strip() if isinstance(route, dict) else None,
    )

    mode = (route.get("mode") or "").strip()
    steps = route.get("steps") or []

    lines = ["Router plan:"]
    if mode:
        lines.append(f"- mode: {mode}")

    if isinstance(steps, list) and steps:
        lines.append("- steps:")
        for i, step in enumerate(steps, start=1):
            assistant = step.get("assistant_name") or step.get("assistant_id") or "unknown"
            goal = step.get("goal") or ""
            q = step.get("question_for_assistant") or ""
            lines.append(f"  {i}. assistant={assistant}")
            if goal:
                lines.append(f"     goal: {goal}")
            if q:
                lines.append(f"     question: {q}")

            log.debugx(
                "Router approval prompt stap toegevoegd",
                index=i,
                assistant=assistant,
                goal_length=len(goal or ""),
                question_length=len(q or ""),
            )

    ask_user = (route.get("ask_user") or "").strip()
    if ask_user:
        lines.append(f"- ask_user: {ask_user}")

    lines.append("")
    lines.append("Reply **yes** to continue, **no** to cancel, or **no, <changes>** to request a different plan.")
    result = "\n".join(lines)

    log.infox(
        "Router plan approval prompt formatteren afgerond",
        mode=mode,
        step_count=len(steps) if isinstance(steps, list) else None,
        has_ask_user=bool(ask_user),
        line_count=len(lines),
        prompt_length=len(result),
    )
    return result