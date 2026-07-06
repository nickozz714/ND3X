from __future__ import annotations
from typing import Any, Dict, Optional, List, Callable

from component.config import settings
from component.logging import get_logger
from services.assistants.assistant_service import AssistantService
from services.assistants.output_validator import AssistantOutputValidator
from services.assistants.tool_guard import AssistantToolGuard
from services.audit_service import AuditService
from services.system_cognition.factory import create_system_cognition_service
from services.assistants.orchestration.pending import (
    PendingStore,
    is_confirmation_text,
    is_cancellation_text,
    build_confirmed_mutation_answer,
)
from services.assistants.orchestration.runtime import RuntimeResolver
from services.assistants.orchestration.tool_execution import (
    ToolExecutionRunner,
)
from services.assistants.orchestration.tracing import OrchestratorTracer
from services.assistants.orchestration.routing import RouterWorkflow, format_router_plan_for_approval
from services.assistants.orchestration.guarded_tools import (
    guard_trace_data,
    verify_pending_tool_confirmation,
)
from services.assistants.orchestration.pipeline_runner import TERMINAL_COMPLETED

from services.assistants.orchestration.documents import (
    build_doc_from_text_update_result,
)
from services.assistants.orchestration.formatting import (
    _preview,
    _extract_final_answer_if_json,
    _looks_like_planner_json,
    _fallback_no_evidence_message,
    build_result,
)
from services.assistants.orchestration.pipeline_runner_factory import AssistantPipelineRunnerFactory
from services.system_cognition.memory_injection_service import MemoryInjectionService
from services.system_cognition.memory_retrieval_policy import MemoryRetrievalPolicy

log = get_logger(__name__)

ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]


class AssistantOrchestrator:
    def __init__(
        self,
        *,
        openai_service,
        assistant_service,
        tool_execution_service,
        assistant_output_store_service,
        application_setting_service,
        mcp_client_factory=None,
        workflow_factory=None,
        workflow_service=None,
        pending_store: PendingStore = None,
    ):
        log.infox(
            "AssistantOrchestrator initialiseren",
            has_openai_service=openai_service is not None,
            has_assistant_service=assistant_service is not None,
            has_tool_execution_service=tool_execution_service is not None,
            has_assistant_output_store_service=assistant_output_store_service is not None,
            has_mcp_client_factory=mcp_client_factory is not None,
            has_workflow_factory=workflow_factory is not None,
            has_workflow_service=workflow_service is not None,
            max_tool_steps=getattr(settings, "MAX_TOOL_STEPS", None),
        )

        self.openai = openai_service
        self.assistant_service: AssistantService = assistant_service
        self.mcp_client_factory = mcp_client_factory

        self.system_cognition_allowed = bool(application_setting_service.get_from_code("system_cognition_allowed", is_bool=True))
        # Cognition is an OPTIONAL capability: with no model assigned to the
        # chat.cognition slot it is disabled and skipped entirely (no fallback).
        caps = getattr(self.openai, "capabilities", None)
        if caps is not None and not caps.get("cognition", False):
            if self.system_cognition_allowed:
                log.infox("System cognition uitgeschakeld: geen model toegewezen aan chat.cognition")
            self.system_cognition_allowed = False
        print(f"system_cognition_allowed: {self.system_cognition_allowed}")

        self.runtime = RuntimeResolver(self.assistant_service)
        log.debugx("RuntimeResolver aangemaakt voor AssistantOrchestrator")

        self.output_validator = AssistantOutputValidator()
        log.debugx("AssistantOutputValidator aangemaakt voor AssistantOrchestrator")

        self.tool_guard = AssistantToolGuard()
        log.debugx("AssistantToolGuard aangemaakt voor AssistantOrchestrator")

        self.assistant_output_store = assistant_output_store_service
        self.workflow_factory = workflow_factory

        self.max_tool_calls_per_turn = settings.MAX_TOOL_STEPS
        self.tool_runner = ToolExecutionRunner(
            tool_execution_service=tool_execution_service,
            ingest_wait_timeout_s=600.0,
            ingest_poll_interval_s=0.75,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
        )
        log.debugx(
            "ToolExecutionRunner aangemaakt voor AssistantOrchestrator",
            ingest_wait_timeout_s=600.0,
            ingest_poll_interval_s=0.75,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
        )

        self.pending = pending_store
        log.debugx("PendingStore aangemaakt voor AssistantOrchestrator")

        self.audit = AuditService()
        log.debugx("AuditService aangemaakt voor AssistantOrchestrator")

        self.tracer = OrchestratorTracer(self.audit)
        log.debugx("OrchestratorTracer aangemaakt voor AssistantOrchestrator")

        self.system_cognition, self.system_cognition_dispatcher = create_system_cognition_service(
            openai_service=self.openai,
        )
        self.memory_injection = MemoryInjectionService()
        log.infox(
            "System cognition componenten gekoppeld aan AssistantOrchestrator",
            has_system_cognition=self.system_cognition is not None,
            has_dispatcher=self.system_cognition_dispatcher is not None,
        )

        self.workflow_service = workflow_service
        self.pipeline_runner = AssistantPipelineRunnerFactory(
            openai_service=self.openai,
            assistant_service=self.assistant_service,
            tool_execution_service=tool_execution_service,
            assistant_output_store_service=self.assistant_output_store
        ).create(
            require_mutation_confirmation=True,
            pending_store=self.pending,
        )
        log.infox(
            "AssistantPipelineRunner aangemaakt",
            require_mutation_confirmation=True,
            has_pending_store=self.pending is not None,
        )

        self.router = RouterWorkflow(
            runtime_resolver=self.runtime,
            openai=self.openai,
            output_validator=self.output_validator,
            workflow_service=self.workflow_service,
            run_assistant_pipeline=self.pipeline_runner.run,
            trace_fn=self.tracer.trace,
        )
        log.infox(
            "AssistantOrchestrator geïnitialiseerd",
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
            has_router=self.router is not None,
            has_pipeline_runner=self.pipeline_runner is not None,
        )

        self.memory_retrieval_policy = MemoryRetrievalPolicy(
            memory_repo=self.system_cognition.memory_repo,
            openai_service=self.openai,
        )

    def _cognition_thread_id(self, session_id: Optional[str]) -> Optional[str]:
        if not session_id:
            return None

        value = str(session_id)

        if value.startswith("cognition_"):
            return value

        return f"cognition_{value}"

    async def _inject_planner_memories(
            self,
            *,
            payload: Dict[str, Any],
            question: str,
            session_id: Optional[str],
            turn_id: int,
            trace: List[dict],
            progress_cb: ProgressCallback = None,
    ) -> Dict[str, Any]:
        if not self.system_cognition_allowed:
            return payload

        if not bool(getattr(settings, "PLANNER_MEMORY_INJECTION_ENABLED", True)):
            payload["_planner_memory_context_injected"] = True
            payload["_planner_memory_context"] = {
                "cognition_thread_id": self._cognition_thread_id(session_id) if session_id else None,
                "project_id": payload.get("project_id") or payload.get("_project_id"),
                "memories": [],
                "beliefs": [],
                "instructions": {
                    "purpose": "Planner memory injection disabled by setting.",
                },
            }
            return payload

        if not session_id:
            return payload

        if payload.get("_workflow_background"):
            return payload

        if payload.get("_planner_memory_context_injected"):
            return payload

        cognition_thread_id = self._cognition_thread_id(session_id)
        project_id = payload.get("project_id") or payload.get("_project_id")
        active_state = payload.get("_active_conversation_state") or {}

        try:
            decision = await self.system_cognition.decide_planner_memory_retrieval(
                question=question,
                active_conversation_state=active_state,
                thread_id=session_id,
                project_id=project_id,
                turn_id=turn_id,
                trace=trace,
                progress_cb=progress_cb,
                model=None,  # role memory_decision: → chat.memory_decision slot
            )

            requested_scopes = decision.get("scopes") or []
            requested_types = decision.get("types") or []

            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="planner_memory_retrieval_decision",
                summary=(
                        "Planner memory retrieval: "
                        + ("enabled" if decision.get("should_retrieve") else "skipped")
                ),
                data={
                    "should_retrieve": bool(decision.get("should_retrieve")),
                    "reason": decision.get("reason"),
                    "query": decision.get("query"),
                    "requested_scopes": requested_scopes,
                    "requested_types": requested_types,
                    "cognition_thread_id": cognition_thread_id,
                    "project_id": project_id,
                },
                progress_cb=progress_cb,
            )

            if not decision.get("should_retrieve"):
                payload["_planner_memory_context"] = {
                    "cognition_thread_id": cognition_thread_id,
                    "project_id": project_id,
                    "memories": [],
                    "beliefs": [],
                    "instructions": {
                        "purpose": "Planner memory retrieval skipped by nano decision.",
                    },
                }
                payload["_planner_memory_context_injected"] = True
                return payload

            raw_context = await self.memory_retrieval_policy.retrieve_planner_candidates(
                query=decision.get("query") or question,
                real_thread_id=session_id,
                cognition_thread_id=cognition_thread_id,
                project_id=project_id,
                requested_scopes=requested_scopes,
                requested_types=requested_types,
            )

            debug = raw_context.get("_retrieval_debug") or {}

            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="planner_memory_candidates_filtered",
                summary=(
                    f"Planner memory candidates filtered: "
                    f"{debug.get('kept_count', 0)} kept / "
                    f"{debug.get('candidate_count', 0)} candidates"
                ),
                data=debug,
                progress_cb=progress_cb,
            )

            planner_context = await self.memory_injection.build_planner_context(
                real_thread_id=session_id,
                raw_context=raw_context,
                max_memories=3,
                max_beliefs=0,
            )

            payload["_planner_memory_context"] = {
                "cognition_thread_id": cognition_thread_id,
                "project_id": project_id,
                "memories": planner_context.get("memories") or [],
                "beliefs": [],
                "instructions": planner_context.get("instructions") or {},
            }
            payload["_planner_memory_context_injected"] = True

            injected_ids = planner_context.get("_injected_ids") or []

            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="planner_memory_injected",
                summary=(
                    f"Injected planner memory context: "
                    f"{len(payload['_planner_memory_context']['memories'])} memories, 0 beliefs"
                ),
                data={
                    "cognition_thread_id": cognition_thread_id,
                    "project_id": project_id,
                    "memory_count": len(payload["_planner_memory_context"]["memories"]),
                    "belief_count": 0,
                    "injected_ids": injected_ids,
                    "memories": payload["_planner_memory_context"]["memories"],
                    "beliefs": [],
                },
                progress_cb=progress_cb,
            )

            return payload

        except Exception as exc:
            log.warningx(
                "Planner memory retrieval/injection mislukt; doorgaan zonder planner memories",
                session_id=session_id,
                cognition_thread_id=cognition_thread_id,
                project_id=project_id,
                turn_id=turn_id,
                error=repr(exc),
            )

            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="planner_memory_injection_failed",
                level="warn",
                summary="Planner memory injection failed; continuing without planner memories",
                data={
                    "error": repr(exc),
                    "cognition_thread_id": cognition_thread_id,
                    "project_id": project_id,
                },
                progress_cb=progress_cb,
            )

            payload["_planner_memory_context_injected"] = True
            payload["_planner_memory_context"] = {
                "cognition_thread_id": cognition_thread_id,
                "project_id": project_id,
                "memories": [],
                "beliefs": [],
                "instructions": {},
            }

            return payload

    async def _inject_router_memories(
            self,
            *,
            payload: Dict[str, Any],
            question: str,
            session_id: Optional[str],
            turn_id: int,
            trace: List[dict],
            progress_cb: ProgressCallback = None,
    ) -> Dict[str, Any]:
        if not self.system_cognition_allowed:
            return payload

        if not session_id:
            return payload

        if payload.get("_workflow_background"):
            return payload

        if payload.get("_router_memory_context_injected"):
            return payload

        router_thread_id = "cognition_router"
        active_state = payload.get("_active_conversation_state") or {}
        project_id = payload.get("project_id") or payload.get("_project_id")

        try:
            decision = await self.system_cognition.decide_router_memory_retrieval(
                question=question,
                active_conversation_state=active_state,
                thread_id=session_id,
                project_id=project_id,
                turn_id=turn_id,
                trace=trace,
                progress_cb=progress_cb,
                model=None,  # role memory_decision: → chat.memory_decision slot
            )

            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="router_memory_retrieval_decision",
                summary=(
                        "Router memory retrieval: "
                        + ("enabled" if decision.get("should_retrieve") else "skipped")
                ),
                data={
                    "should_retrieve": bool(decision.get("should_retrieve")),
                    "reason": decision.get("reason"),
                    "query": decision.get("query"),
                },
                progress_cb=progress_cb,
            )

            if not decision.get("should_retrieve"):
                payload["_router_memory_context"] = {
                    "router_thread_id": router_thread_id,
                    "memories": [],
                    "beliefs": [],
                    "instructions": {
                        "purpose": "Router memory retrieval skipped by nano decision.",
                    },
                }
                payload["_router_memory_context_injected"] = True
                return payload

            raw_context = await self.memory_retrieval_policy.retrieve_router_candidates(
                query=decision.get("query") or question,
            )

            debug = raw_context.get("_retrieval_debug") or {}

            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="router_memory_candidates_filtered",
                summary=(
                    f"Router memory candidates filtered: "
                    f"{debug.get('kept_count', 0)} kept / "
                    f"{debug.get('candidate_count', 0)} candidates"
                ),
                data=debug,
                progress_cb=progress_cb,
            )

            router_context = await self.memory_injection.build_router_context(
                real_thread_id=session_id,
                raw_context=raw_context,
                max_memories=3,
                max_beliefs=0,
            )

            payload["_router_memory_context"] = {
                "router_thread_id": router_thread_id,
                "memories": router_context.get("memories") or [],
                "beliefs": [],
                "instructions": router_context.get("instructions") or {},
            }
            payload["_router_memory_context_injected"] = True

            injected_ids = router_context.get("_injected_ids") or []

            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="router_memory_injected",
                summary=(
                    f"Injected router memory context: "
                    f"{len(payload['_router_memory_context']['memories'])} memories, 0 beliefs"
                ),
                data={
                    "router_thread_id": router_thread_id,
                    "memory_count": len(payload["_router_memory_context"]["memories"]),
                    "belief_count": 0,
                    "injected_ids": injected_ids,
                    "memories": payload["_router_memory_context"]["memories"],
                    "beliefs": [],
                },
                progress_cb=progress_cb,
            )

            return payload

        except Exception as exc:
            log.warningx(
                "Router memory retrieval/injection mislukt; doorgaan zonder router memories",
                session_id=session_id,
                router_thread_id=router_thread_id,
                turn_id=turn_id,
                error=repr(exc),
            )

            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="router_memory_injection_failed",
                level="warn",
                summary="Router memory injection failed; continuing without router memories",
                data={
                    "error": repr(exc),
                    "router_thread_id": router_thread_id,
                },
                progress_cb=progress_cb,
            )

            payload["_router_memory_context_injected"] = True
            payload["_router_memory_context"] = {
                "router_thread_id": router_thread_id,
                "memories": [],
                "beliefs": [],
                "instructions": {},
            }

            return payload

    async def _postprocess_result(self, result, *, question, session_id, model):
        log.infox(
            "Orchestrator resultaat postprocessen gestart",
            mode=result.get("mode") if isinstance(result, dict) else None,
            session_id=session_id,
            question_length=len(question or ""),
            model=model,
        )

        if result.get("mode") == "workflow_finalize":
            log.infox(
                "Workflow finalize resultaat gedetecteerd",
                session_id=session_id,
                workflow_handoff_count=len(result.get("workflow_handoffs") or []),
            )
            final_answer = await self._finalize_workflow_answer(
                question=question,
                workflow_handoffs=result.get("workflow_handoffs") or [],
                session_id=session_id,
                model=model,
            )
            result["mode"] = "synthesize_answer"
            result["answer"] = final_answer

            log.infox(
                "Workflow finalize resultaat omgezet naar synthesize_answer",
                session_id=session_id,
                answer_length=len(final_answer or ""),
            )

        log.infox(
            "Orchestrator resultaat postprocessen afgerond",
            mode=result.get("mode") if isinstance(result, dict) else None,
            session_id=session_id,
            answer_length=len((result.get("answer") or "") if isinstance(result, dict) else ""),
        )
        return result

    def _enqueue_system_cognition(
            self,
            *,
            question: str,
            answer: str,
            session_id: Optional[str],
            turn_id: int,
            project_id: Optional[str] = None,
    ) -> None:
        cognition_thread_id = self._cognition_thread_id(session_id) or "cognition_global"

        log.infox(
            "System cognition enqueue gestart",
            session_id=session_id,
            cognition_thread_id=cognition_thread_id,
            turn_id=turn_id,
            question_length=len(question or ""),
            answer_length=len(answer or ""),
            has_dispatcher=self.system_cognition_dispatcher is not None,
        )

        try:
            self.system_cognition_dispatcher.enqueue(
                question=question,
                answer=answer or "",
                thread_id=cognition_thread_id,
                project_id=project_id,
                turn_id=turn_id,
                trace=[],
                progress_cb=None,
            )

            log.infox(
                "System cognition enqueue gelukt",
                session_id=session_id,
                cognition_thread_id=cognition_thread_id,
                turn_id=turn_id,
            )

        except Exception as e:
            log.errorx(
                "System cognition enqueue mislukt",
                session_id=session_id,
                cognition_thread_id=cognition_thread_id,
                turn_id=turn_id,
                error=repr(e),
            )

    def _resolve_handoff_full_answer(self, handoff: Dict[str, Any]) -> Optional[str]:
        log.debugx(
            "Handoff full_answer resolven gestart",
            handoff_type=type(handoff).__name__,
            handoff_keys=list(handoff.keys()) if isinstance(handoff, dict) else None,
        )

        if not isinstance(handoff, dict):
            log.debugx("Handoff full_answer resolven overgeslagen: handoff is geen dict")
            return None

        full_answer = handoff.get("full_answer")
        if isinstance(full_answer, str) and full_answer.strip():
            log.debugx(
                "Handoff full_answer inline gevonden",
                full_answer_length=len(full_answer),
            )
            return full_answer

        output_ref = handoff.get("output_ref") or {}
        output_id = output_ref.get("id")
        if not output_id:
            log.debugx(
                "Handoff full_answer niet gevonden: output_ref ontbreekt",
                has_output_ref=bool(output_ref),
            )
            return None

        log.infox(
            "Handoff full_answer ophalen uit output store",
            output_id=output_id,
            output_ref_keys=list(output_ref.keys()) if isinstance(output_ref, dict) else None,
        )
        result = self.assistant_output_store.retrieve_text(output_id=output_id)
        log.infox(
            "Handoff full_answer opgehaald uit output store",
            output_id=output_id,
            result_length=len(result or ""),
        )
        return result

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
            "Large handoff content check gestart",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            handoff_type=type(handoff).__name__,
            handoff_keys=list(handoff.keys()) if isinstance(handoff, dict) else None,
        )

        if not isinstance(handoff, dict):
            log.debugx("Large handoff content check overgeslagen: handoff is geen dict")
            return handoff

        full_answer = (handoff.get("full_answer") or "").strip()
        if not full_answer:
            log.debugx("Large handoff content check overgeslagen: full_answer ontbreekt")
            return handoff

        # keep small outputs inline
        inline_threshold = 6000
        if len(full_answer) <= inline_threshold:
            log.debugx(
                "Handoff full_answer blijft inline",
                session_id=session_id,
                turn_id=turn_id,
                assistant_name=assistant_name,
                full_answer_length=len(full_answer),
                inline_threshold=inline_threshold,
            )
            return handoff

        log.infox(
            "Grote handoff output opslaan in output store",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            full_answer_length=len(full_answer),
            inline_threshold=inline_threshold,
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

        self.tracer.trace(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="handoff_output_stored",
            summary=f"Stored large handoff output in {ref.get('chunk_count', 0)} chunks",
            data={
                "assistant": assistant_name,
                "output_ref": ref,
            },
        )

        log.infox(
            "Grote handoff output opgeslagen",
            session_id=session_id,
            turn_id=turn_id,
            assistant_name=assistant_name,
            output_id=ref.get("id") if isinstance(ref, dict) else None,
            chunk_count=ref.get("chunk_count") if isinstance(ref, dict) else None,
        )
        return new_handoff

    async def _finalize_workflow_answer(
        self,
        *,
        question: str,
        workflow_handoffs: List[Dict[str, Any]],
        session_id: Optional[str],
        model: Optional[str] = None,
    ) -> str:
        log.infox(
            "Workflow antwoord finaliseren gestart",
            session_id=session_id,
            question_length=len(question or ""),
            workflow_handoff_count=len(workflow_handoffs or []),
            model=model,
        )

        if not workflow_handoffs:
            log.warningx(
                "Workflow antwoord finaliseren overgeslagen: geen handoffs",
                session_id=session_id,
            )
            return "(no answer)"

        parts: List[str] = []
        for i, handoff in enumerate(workflow_handoffs, start=1):
            log.debugx(
                "Workflow handoff verwerken voor final answer",
                session_id=session_id,
                step=i,
                handoff_type=type(handoff).__name__,
                handoff_keys=list(handoff.keys()) if isinstance(handoff, dict) else None,
            )

            if not isinstance(handoff, dict):
                log.debugx(
                    "Workflow handoff overgeslagen: geen dict",
                    session_id=session_id,
                    step=i,
                    handoff_type=type(handoff).__name__,
                )
                continue

            summary = (handoff.get("summary") or "").strip()
            facts = handoff.get("facts") or {}
            open_questions = handoff.get("open_questions") or []
            artifacts = handoff.get("artifacts") or []
            full_answer = self._resolve_handoff_full_answer(handoff)
            output_ref = handoff.get("output_ref")

            log.debugx(
                "Workflow handoff velden gelezen",
                session_id=session_id,
                step=i,
                summary_length=len(summary),
                fact_keys=list(facts.keys()) if isinstance(facts, dict) else None,
                open_question_count=len(open_questions) if isinstance(open_questions, list) else None,
                artifact_count=len(artifacts) if isinstance(artifacts, list) else None,
                full_answer_length=len(full_answer or ""),
                has_output_ref=bool(output_ref),
            )

            parts.append(f"Step {i}")
            if summary:
                parts.append(f"Summary: {summary}")
            if full_answer:
                parts.append(f"Full answer: {full_answer}")
            if facts:
                parts.append(f"Facts: {facts}")
            if open_questions:
                parts.append(f"Open questions: {open_questions}")
            if artifacts:
                parts.append(f"Artifacts: {artifacts[:5]}")
            if output_ref:
                parts.append(f"Output ref: {output_ref}")
            parts.append("")

        log.infox(
            "Final answer writer ophalen voor workflow finalisatie",
            session_id=session_id,
            part_count=len(parts),
            parts_length=len("\n".join(parts)),
        )
        writer = self.runtime.get_final_answer_runtime_assistant()
        write_prompt = writer.prompt(
            question,
            tool_name="workflow_handoffs",
            tool_args=[],
            tool_result=[],
            docs=[{
                "kind": "workflow_handoff",
                "meta": "workflow_handoffs",
                "path": "workflow_handoffs",
                "doc_id": None,
                "selected": None,
                "content_preview": "\n".join(parts),
                "source_tool": "workflow_finalize",
            }],
        )

        log.infox(
            "Workflow final answer OpenAI call gestart",
            session_id=session_id,
            writer_name=getattr(writer, "name", type(writer).__name__),
            prompt_length=len(write_prompt or ""),
            model=model,
        )
        write_resp = await self.openai.ask_orchestration_async(
            write_prompt,
            role="workflow_finalizer",
            instructions=writer.instructions,
            keep_context=False,
            store=False,
            session_id=session_id,
            model=model,
            max_output_tokens=8000,
            metadata={
                "kind": "workflow_finalizer",
            },
        )

        raw_answer = (write_resp.text or "").strip()
        log.infox(
            "Workflow final answer OpenAI call afgerond",
            session_id=session_id,
            raw_answer_length=len(raw_answer),
        )

        extracted = _extract_final_answer_if_json(raw_answer)
        if extracted:
            log.infox(
                "Workflow final answer geëxtraheerd uit JSON",
                session_id=session_id,
                answer_length=len(extracted),
            )
            return extracted
        if _looks_like_planner_json(raw_answer):
            log.warningx(
                "Workflow final answer lijkt planner JSON, fallback wordt gebruikt",
                session_id=session_id,
                raw_answer_length=len(raw_answer),
            )
            return _fallback_no_evidence_message()

        result = raw_answer or "(no answer)"
        log.infox(
            "Workflow antwoord finaliseren afgerond",
            session_id=session_id,
            answer_length=len(result),
            used_default_no_answer=not bool(raw_answer),
        )
        return result

    async def run(
        self,
        *,
        question: str,
        payload: Dict[str, Any] | None = None,
        thread_id: str | None = None,
        model: str | None = None,
        progress_cb: ProgressCallback = None,
    ) -> Dict[str, Any]:
        log.infox(
            "AssistantOrchestrator run gestart",
            thread_id=thread_id,
            model=model,
            question_length=len(question or ""),
            payload_keys=list((payload or {}).keys()),
            has_progress_cb=progress_cb is not None,
        )

        payload = payload or {}
        trace: list[dict] = []
        session_id = thread_id
        turn_id = self.tracer.next_turn_id(session_id)

        log.debugx(
            "Nieuwe orchestrator turn aangemaakt",
            session_id=session_id,
            turn_id=turn_id,
            payload_keys=list(payload.keys()),
        )

        self.tracer.trace(
            trace,
            thread_id=session_id,
            turn_id=turn_id,
            type="turn_start",
            summary="Turn started",
            data={"question_preview": (question or "")[:500]},
            progress_cb=progress_cb,
        )

        pending = self.pending.get(session_id)
        if pending:
            log.infox(
                "Pending actie gevonden voor sessie",
                session_id=session_id,
                turn_id=turn_id,
                pending_type=pending.get("type"),
                pending_keys=list(pending.keys())[:10],
                user_input_preview=(question or "")[:120],
            )

            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="pending_check",
                level="warn",
                summary="Pending action exists",
                data={"pending_keys": list(pending.keys())[:10], "user_input": (question or "")[:120]},
            )

            if is_cancellation_text(question):
                pending_type = pending.get("type", "mutation_confirmation")
                original_question = pending.get("original_question") or ""

                log.infox(
                    "Gebruiker annuleert pending actie",
                    session_id=session_id,
                    turn_id=turn_id,
                    pending_type=pending_type,
                    original_question_length=len(original_question),
                )
                self.pending.clear(session_id)

                self.tracer.trace(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="confirm_outcome",
                    level="warn",
                    summary="User cancelled pending action",
                    data={"pending_type": pending_type, "user_input": (question or "")[:300]},
                )

                if pending_type == "tool_confirmation":
                    self.tracer.trace(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="guarded_tool_confirmation_required",
                        level="warn",
                        summary="Guarded tool confirmation cancelled",
                        data=guard_trace_data(pending, confirmed=False),
                    )
                    return build_result(
                        mode="final",
                        answer="Cancelled. Shell command was not executed.",
                        trace=trace,
                        thread_id=session_id,
                    )

                if pending_type == "router_plan_approval":
                    s = (question or "").strip()
                    lowered = s.lower()
                    has_feedback = lowered.startswith("no ") or lowered.startswith("no,") or lowered.startswith("cancel ")

                    log.debugx(
                        "Router plan annulering verwerkt",
                        session_id=session_id,
                        turn_id=turn_id,
                        has_feedback=has_feedback,
                        user_input=s[:300],
                    )

                    if has_feedback:
                        feedback = s
                        if lowered.startswith("no,"):
                            feedback = s[3:].strip()
                        elif lowered.startswith("no "):
                            feedback = s[2:].strip()
                        elif lowered.startswith("cancel "):
                            feedback = s[7:].strip()

                        replan_question = (
                            f"{original_question}\n\n"
                            f"The previous router plan was rejected by the user.\n"
                            f"User feedback for replanning: {feedback}"
                        )

                        log.infox(
                            "Router plan opnieuw plannen met feedback",
                            session_id=session_id,
                            turn_id=turn_id,
                            feedback_length=len(feedback),
                            replan_question_length=len(replan_question),
                        )
                        return await self.run(
                            question=replan_question,
                            payload=payload,
                            thread_id=session_id,
                            model=model,
                        )

                    log.infox(
                        "Router plan geannuleerd zonder herplanning",
                        session_id=session_id,
                        turn_id=turn_id,
                    )
                    return build_result(
                        mode="final",
                        answer="Cancelled. Router plan was not executed.",
                        trace=trace,
                        thread_id=session_id,
                    )

                log.infox(
                    "Pending mutation geannuleerd",
                    session_id=session_id,
                    turn_id=turn_id,
                    pending_type=pending_type,
                )
                return build_result(
                    mode="final",
                    answer="Cancelled. No changes were made.",
                    trace=trace,
                    thread_id=session_id,
                )

            if is_confirmation_text(question):
                pending_type = pending.get("type", "mutation_confirmation")

                log.infox(
                    "Gebruiker bevestigt pending actie",
                    session_id=session_id,
                    turn_id=turn_id,
                    pending_type=pending_type,
                )

                if pending_type == "tool_confirmation":
                    try:
                        validation = verify_pending_tool_confirmation(pending)
                    except ValueError as e:
                        self.pending.clear(session_id)
                        self.tracer.trace(
                            trace,
                            thread_id=session_id,
                            turn_id=turn_id,
                            type="guarded_tool_confirmation_required",
                            level="error",
                            summary="Guarded tool confirmation rejected",
                            data={**guard_trace_data(pending, confirmed=False), "error": str(e)},
                        )
                        return build_result(
                            mode="error",
                            answer=str(e),
                            trace=trace,
                            thread_id=session_id,
                        )

                    self.tracer.trace(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="guarded_tool_confirmation_required",
                        level="info",
                        summary="Guarded tool confirmation approved",
                        data=guard_trace_data(pending, confirmed=True),
                    )
                    tool_call = validation.tool_call
                    tool_results = await self.tool_runner.execute_tool_calls(
                        tool_calls=[tool_call],
                        session_id=session_id,
                        turn_id=turn_id,
                        trace=trace,
                        assistant_name="pending_tool_confirmation",
                        trace_fn=self.tracer.trace,
                        preview_fn=_preview,
                        progress_cb=progress_cb,
                        confirmed_tool_call_hashes={validation.tool_call_hash},
                    )
                    self.pending.clear(session_id)
                    status = None
                    if tool_results and isinstance(tool_results[0], dict):
                        status = tool_results[0].get("status")
                    self.tracer.trace(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="guarded_tool_execution_status",
                        level="info",
                        summary="Guarded tool executed after confirmation",
                        data={**guard_trace_data(pending, confirmed=True), "execution_status": status},
                    )

                    continuation = pending.get("continuation") or {}
                    assistant_id = continuation.get("assistant_id")
                    if assistant_id:
                        try:
                            assistant = self.runtime.get_runtime_assistant_by_id_or_name(assistant_id, continuation.get("assistant_name"))
                            cont_payload = dict(continuation.get("payload") or {})
                            cont_payload["_used_evaluate"] = True
                            cont_payload["_last_tool_calls"] = [tool_call]
                            cont_payload["_last_tool_results"] = tool_results
                            cont_payload["_last_docs"] = []
                            cont_payload["_acc_tool_calls"] = list(cont_payload.get("_acc_tool_calls") or []) + [tool_call]
                            cont_payload["_acc_tool_results"] = list(cont_payload.get("_acc_tool_results") or []) + tool_results
                            result = await self.pipeline_runner.run(
                                assistant=assistant,
                                question=continuation.get("question") or "Continue after confirmed tool execution.",
                                model=continuation.get("model"),
                                payload=cont_payload,
                                session_id=session_id,
                                turn_id=turn_id,
                                trace=trace,
                                progress_cb=progress_cb,
                            )
                            return await self._postprocess_result(
                                result,
                                question=continuation.get("question") or "",
                                session_id=session_id,
                                model=continuation.get("model"),
                            )
                        except Exception as exc:
                            log.warningx(
                                "Guarded tool continuation failed; returning execution status",
                                session_id=session_id,
                                turn_id=turn_id,
                                error=str(exc),
                            )

                    return build_result(
                        mode="synthesize_answer",
                        answer="✅ Confirmed and executed the shell command.",
                        trace=trace,
                        thread_id=session_id,
                        tool_calls=[tool_call],
                        tool_results=tool_results,
                        terminal_state=TERMINAL_COMPLETED,
                    )

                if pending_type == "workflow_trigger":
                    workflow_id = pending.get("workflow_id")
                    input_payload = pending.get("input_payload") or {}

                    self.pending.clear(session_id)

                    log.infox(
                        "Workflow trigger bevestigd",
                        session_id=session_id,
                        turn_id=turn_id,
                        workflow_id=workflow_id,
                        input_payload_keys=list(input_payload.keys()) if isinstance(input_payload, dict) else None,
                        has_workflow_factory=self.workflow_factory is not None,
                    )

                    if not self.workflow_factory:
                        log.errorx(
                            "Workflow trigger mislukt: workflow_factory ontbreekt",
                            session_id=session_id,
                            turn_id=turn_id,
                            workflow_id=workflow_id,
                        )
                        return build_result(
                            mode="error",
                            answer="Workflow trigger requested, but workflow_factory is not configured.",
                            trace=trace,
                            thread_id=session_id,
                        )

                    workflow_run = self.workflow_factory.trigger_manual(
                        workflow_id=workflow_id,
                        input_payload=input_payload,
                    )

                    log.infox(
                        "Workflow gestart vanuit pending bevestiging",
                        session_id=session_id,
                        turn_id=turn_id,
                        workflow_id=workflow_id,
                        workflow_run_id=getattr(workflow_run, "id", None),
                    )
                    return build_result(
                        mode="workflow_queued",
                        answer=f"Workflow started. Run id: {workflow_run.id}",
                        trace=trace,
                        thread_id=session_id,
                        workflow_run_id=workflow_run.id,
                    )
                if pending_type == "router_plan_approval":
                    approved_route = pending.get("route") or {}
                    original_question = pending.get("original_question") or question
                    self.pending.clear(session_id)

                    log.infox(
                        "Router plan bevestigd",
                        session_id=session_id,
                        turn_id=turn_id,
                        route_mode=approved_route.get("mode") if isinstance(approved_route, dict) else None,
                        original_question_length=len(original_question or ""),
                    )

                    self.tracer.trace(
                        trace,
                        thread_id=session_id,
                        turn_id=turn_id,
                        type="confirm_outcome",
                        level="info",
                        summary="User approved router plan",
                        data={"route": approved_route},
                    )

                    result = await self.router.execute_router_plan(
                        route=approved_route,
                        question=original_question,
                        payload={**payload, "_router_plan_approved": True},
                        session_id=session_id,
                        model=model,
                        trace=trace,
                        turn_id=turn_id,
                    )

                    log.infox(
                        "Goedgekeurd router plan uitgevoerd",
                        session_id=session_id,
                        turn_id=turn_id,
                        result_mode=result.get("mode") if isinstance(result, dict) else None,
                    )

                    final_result = await self._postprocess_result(
                        result,
                        question=original_question,
                        session_id=session_id,
                        model=model,
                    )
                    if self.system_cognition_allowed:
                        self._enqueue_system_cognition(
                            question=original_question,
                            answer=final_result.get("answer") or "",
                            session_id=session_id,
                            turn_id=turn_id,
                            project_id=(payload or {}).get("project_id") or (payload or {}).get("_project_id"),
                        )
                    return final_result

                tool_calls = pending.get("tool_calls") or []

                log.infox(
                    "Bevestigde mutation tool calls uitvoeren",
                    session_id=session_id,
                    turn_id=turn_id,
                    tool_call_count=len(tool_calls),
                    tool_names=[(tc.get("tool") or "").strip() for tc in tool_calls if isinstance(tc, dict)],
                )
                tool_results = await self.tool_runner.execute_tool_calls(
                    tool_calls=tool_calls,
                    session_id=session_id,
                    turn_id=turn_id,
                    trace=trace,
                    assistant_name="pending_confirmation",
                    trace_fn=self.tracer.trace,
                    preview_fn=_preview,
                    progress_cb=progress_cb,
                )

                log.infox(
                    "Bevestigde mutation tool calls afgerond",
                    session_id=session_id,
                    turn_id=turn_id,
                    tool_call_count=len(tool_calls),
                    tool_result_count=len(tool_results or []),
                )

                docs_from_mutation: List[Dict[str, Any]] = []
                for tc, result in zip(tool_calls, tool_results):
                    if (tc.get("tool") or "").strip() == "text_update":
                        d = build_doc_from_text_update_result(result)
                        if d:
                            docs_from_mutation.append(d)

                log.debugx(
                    "Documenten uit bevestigde mutation opgebouwd",
                    session_id=session_id,
                    turn_id=turn_id,
                    doc_count=len(docs_from_mutation),
                )

                self.pending.clear(session_id)
                answer = build_confirmed_mutation_answer(tool_calls, tool_results, docs_from_mutation)

                self.tracer.trace(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="turn_end",
                    summary="Turn completed (confirmed mutation)",
                    data={"mode": "synthesize_answer"},
                )

                final_result = build_result(
                    mode="synthesize_answer",
                    answer=answer,
                    trace=trace,
                    thread_id=session_id,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    docs=docs_from_mutation,
                )

                log.infox(
                    "Bevestigde mutation resultaat gebouwd",
                    session_id=session_id,
                    turn_id=turn_id,
                    answer_length=len(answer or ""),
                    doc_count=len(docs_from_mutation),
                )
                if self.system_cognition_allowed:
                    self._enqueue_system_cognition(
                        question=question,
                        answer=final_result.get("answer") or "",
                        session_id=session_id,
                        turn_id=turn_id,
                        project_id=(payload or {}).get("project_id") or (payload or {}).get("_project_id"),
                    )

                return final_result

            log.infox(
                "Pending actie wacht nog op expliciete bevestiging",
                session_id=session_id,
                turn_id=turn_id,
                pending_type=pending.get("type"),
            )
            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="confirm_prompt",
                level="warn",
                summary="Awaiting confirmation",
                data={"prompt_preview": (pending.get("prompt") or "")[:400]},
            )
            return build_result(
                mode="confirm_action",
                answer=pending.get("prompt") or "Please confirm: reply 'yes' to proceed or 'no' to cancel.",
                trace=trace,
                thread_id=session_id,
                pending_action=pending,
            )

        payload = await self._inject_router_memories(
            payload=payload,
            question=question,
            session_id=session_id,
            turn_id=turn_id,
            trace=trace,
            progress_cb=progress_cb,
        )

        # payload = await self._inject_planner_memories(
        #     payload=payload,
        #     question=question,
        #     session_id=session_id,
        #     turn_id=turn_id,
        #     trace=trace,
        #     progress_cb=progress_cb,
        # )

        log.debugx(
            "Memory contexts toegevoegd aan payload",
            session_id=session_id,
            turn_id=turn_id,
            payload_keys=list(payload.keys()),
            has_router_memory_context=bool(payload.get("_router_memory_context")),
            has_planner_memory_context=bool(payload.get("_planner_memory_context")),
            router_memory_count=len((payload.get("_router_memory_context") or {}).get("memories") or []),
            router_belief_count=len((payload.get("_router_memory_context") or {}).get("beliefs") or []),
            planner_memory_count=len((payload.get("_planner_memory_context") or {}).get("memories") or []),
            planner_belief_count=len((payload.get("_planner_memory_context") or {}).get("beliefs") or []),
        )

        if getattr(settings, "SINGLE_AGENT_MODE", False):
            log.infox(
                "Single-agent modus actief: skill-selectie i.p.v. router",
                session_id=session_id,
                turn_id=turn_id,
            )
            result = await self.router.run_single_agent(
                question=question,
                payload=payload,
                session_id=session_id,
                model=model,
                trace=trace,
                turn_id=turn_id,
                progress_cb=progress_cb,
            )
            final_result = await self._postprocess_result(
                result,
                question=question,
                session_id=session_id,
                model=model,
            )
            # Gate cognition on trivial turns: a direct answer or clarifying question did
            # no real work and should not spawn memory/belief extraction.
            trivial_turn = (final_result.get("mode") or "").strip() in {"answer", "ask_user"}
            if self.system_cognition_allowed and not trivial_turn:
                self._enqueue_system_cognition(
                    question=question,
                    answer=final_result.get("answer") or "",
                    session_id=session_id,
                    turn_id=turn_id,
                    project_id=(payload or {}).get("project_id") or (payload or {}).get("_project_id"),
                )
            return final_result

        router_payload = self.router.build_router_payload(payload=payload, thread_id=session_id)

        log.infox(
            "Router payload gebouwd",
            session_id=session_id,
            turn_id=turn_id,
            router_payload_keys=list(router_payload.keys()) if isinstance(router_payload, dict) else None,
        )

        try:
            log.infox(
                "Router route_request gestart",
                session_id=session_id,
                turn_id=turn_id,
                model=model,
                question_length=len(question or ""),
            )
            route = await self.router.route_request(
                question=question,
                payload=router_payload,
                session_id=session_id,
                model=model,
                trace=trace,
                turn_id=turn_id,
                progress_cb=progress_cb,
            )
            log.infox(
                "Router route_request afgerond",
                session_id=session_id,
                turn_id=turn_id,
                route_mode=route.get("mode") if isinstance(route, dict) else None,
                route_keys=list(route.keys()) if isinstance(route, dict) else None,
            )
        except ValueError as e:
            log.errorx(
                "Router output parse failed",
                session_id=session_id,
                turn_id=turn_id,
                error=str(e),
            )
            self.tracer.trace(
                trace,
                thread_id=session_id,
                turn_id=turn_id,
                type="error",
                level="error",
                summary="Router output parse failed",
                data={"error": str(e)},
            )
            return build_result(
                mode="error",
                answer=f"Router produced an answer that could not be parsed: {e}",
                trace=trace,
                thread_id=session_id,
            )

        if bool(payload.get("require_router_plan_approval", False)) and not bool(payload.get("_router_plan_approved", False)):
            mode = (route.get("mode") or "").strip()
            log.infox(
                "Router plan approval vereist",
                session_id=session_id,
                turn_id=turn_id,
                route_mode=mode,
                already_approved=bool(payload.get("_router_plan_approved", False)),
            )
            if mode != "ask_user":
                prompt = format_router_plan_for_approval(route)
                pending_action = {
                    "type": "router_plan_approval",
                    "route": route,
                    "original_question": question,
                    "prompt": prompt,
                }
                self.pending.set(session_id, pending_action)
                log.infox(
                    "Router plan approval pending opgeslagen",
                    session_id=session_id,
                    turn_id=turn_id,
                    route_mode=mode,
                    prompt_length=len(prompt or ""),
                )
                self.tracer.trace(
                    trace,
                    thread_id=session_id,
                    turn_id=turn_id,
                    type="confirm_prompt",
                    level="info",
                    summary="Router plan requires approval",
                    data={"route": route},
                )
                return build_result(
                    mode="confirm_action",
                    answer=prompt,
                    trace=trace,
                    thread_id=session_id,
                    pending_action=pending_action,
                    router_plan=route,
                )

        log.infox(
            "Router plan uitvoeren gestart",
            session_id=session_id,
            turn_id=turn_id,
            route_mode=route.get("mode") if isinstance(route, dict) else None,
        )
        result = await self.router.execute_router_plan(
            route=route,
            question=question,
            payload=payload,
            session_id=session_id,
            model=model,
            trace=trace,
            turn_id=turn_id,
            progress_cb=progress_cb,
        )
        log.infox(
            "Router plan uitvoeren afgerond",
            session_id=session_id,
            turn_id=turn_id,
            result_mode=result.get("mode") if isinstance(result, dict) else None,
            answer_length=len((result.get("answer") or "") if isinstance(result, dict) else ""),
        )

        if result.get("mode") == "workflow_offer":
            workflow_id = result.get("workflow_id")
            log.infox(
                "Workflow offer ontvangen vanuit router resultaat",
                session_id=session_id,
                turn_id=turn_id,
                workflow_id=workflow_id,
            )
            return build_result(
                mode="confirm_action",
                answer=(
                        result.get("answer")
                        or f"This looks like a long-running workflow. Start workflow {workflow_id}?"
                ),
                trace=trace,
                thread_id=session_id,
                pending_action={
                    "type": "workflow_trigger",
                    "workflow_id": workflow_id,
                    "input_payload": result.get("input_payload") or {},
                    "original_question": question,
                },
                router_plan=result.get("router_plan"),
            )

        if result.get("mode") == "workflow_trigger":
            workflow_id = result.get("workflow_id")
            input_payload = result.get("input_payload") or {}

            log.infox(
                "Workflow trigger ontvangen vanuit router resultaat",
                session_id=session_id,
                turn_id=turn_id,
                workflow_id=workflow_id,
                input_payload_keys=list(input_payload.keys()) if isinstance(input_payload, dict) else None,
                has_workflow_factory=self.workflow_factory is not None,
            )

            workflow_run = self.workflow_factory.trigger_manual(
                workflow_id=workflow_id,
                input_payload=input_payload,
            )

            log.infox(
                "Workflow gestart vanuit router resultaat",
                session_id=session_id,
                turn_id=turn_id,
                workflow_id=workflow_id,
                workflow_run_id=getattr(workflow_run, "id", None),
            )
            return build_result(
                mode="workflow_queued",
                answer=f"Workflow started. Run id: {workflow_run.id}",
                trace=trace,
                thread_id=session_id,
                workflow_run_id=workflow_run.id,
            )

        final_result = await self._postprocess_result(
            result,
            question=question,
            session_id=session_id,
            model=model,
        )

        log.infox(
            "Final result na postprocess beschikbaar",
            session_id=session_id,
            turn_id=turn_id,
            mode=final_result.get("mode") if isinstance(final_result, dict) else None,
            answer_length=len((final_result.get("answer") or "") if isinstance(final_result, dict) else ""),
        )
        if self.system_cognition_allowed:
            self._enqueue_system_cognition(
                question=question,
                answer=final_result.get("answer") or "",
                session_id=session_id,
                turn_id=turn_id,
                project_id=(payload or {}).get("project_id") or (payload or {}).get("_project_id"),
            )

        log.infox(
            "AssistantOrchestrator run afgerond",
            session_id=session_id,
            turn_id=turn_id,
            mode=final_result.get("mode") if isinstance(final_result, dict) else None,
            trace_count=len(trace),
        )
        return final_result

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
        """Backward-compatible delegate for code that still calls the old private method."""
        log.infox(
            "Backward-compatible _run_assistant_pipeline delegate gestart",
            assistant_name=getattr(assistant, "name", type(assistant).__name__),
            session_id=session_id,
            turn_id=turn_id,
            question_length=len(question or ""),
            model=model,
            payload_keys=list(payload.keys()) if isinstance(payload, dict) else None,
            trace_count=len(trace or []),
            has_progress_cb=progress_cb is not None,
        )
        result = await self.pipeline_runner.run(
            assistant=assistant,
            question=question,
            model=model,
            payload=payload,
            session_id=session_id,
            turn_id=turn_id,
            trace=trace,
            progress_cb=progress_cb,
        )
        log.infox(
            "Backward-compatible _run_assistant_pipeline delegate afgerond",
            assistant_name=getattr(assistant, "name", type(assistant).__name__),
            session_id=session_id,
            turn_id=turn_id,
            result_mode=result.get("mode") if isinstance(result, dict) else None,
            answer_length=len((result.get("answer") or "") if isinstance(result, dict) else ""),
        )
        return result