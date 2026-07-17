from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from assistants.system_curiosity.memory_retrieval_decision_assistant import PlannerMemoryRetrievalDecisionAssistant, \
    RouterMemoryRetrievalDecisionAssistant
from component.logging import get_logger
from assistants.system_curiosity.memory_system_assistant import MemorySystemAssistant
from assistants.system_curiosity.curiosity_system_assistant import CuriositySystemAssistant
from assistants.system_curiosity.research_system_assistant import ResearchSystemAssistant
from assistants.system_curiosity.belief_system_assistant import BeliefSystemAssistant

from services.system_cognition.models import MemoryRecord, BeliefRecord, CuriosityJob
from repository.system_cognition.memory_repository import MemoryRepository
from repository.system_cognition.belief_repository import BeliefRepository
from repository.system_cognition.curiosity_job_repository import CuriosityJobRepository
from services.system_cognition.system_context_builder import SystemContextBuilder
from services.system_cognition.system_pipeline_runner import SystemPipelineRunner
from assistants.system_curiosity.turn_interpretation_system_assistant import TurnInterpretationSystemAssistant
from assistants.system_curiosity.cognition_router_system_assistant import CognitionRouterSystemAssistant
from assistants.system_curiosity.research_observation_system_assistant import ResearchObservationSystemAssistant
from services.system_cognition.system_embedding_service import SystemEmbeddingService

from services.system_cognition.cognition_compaction import (
    compact_existing_context,
    compact_interpretation_for_memory,
    compact_interpretation_for_curiosity,
    compact_interpretation_for_belief,
    compact_research_docs_for_belief,
)

log = get_logger(__name__)


class SystemCognitionService:
    """
    Internal orchestrator-owned cognition subsystem.

    Implements:
    - memory search/write
    - curiosity job enqueue/execution
    - belief synthesis/write
    - EXA-backed autonomous research via system assistant tool calls
    """

    def __init__(
        self,
        *,
        openai_service,
        memory_repo: MemoryRepository,
        belief_repo: BeliefRepository,
        curiosity_repo: CuriosityJobRepository,
        system_runner: SystemPipelineRunner,
        audit_service=None,
        default_model: Optional[str] = None,  # None → resolved from the chat.cognition slot
        max_jobs_per_turn: int = 2,
    ):
        log.infox(
            "SystemCognitionService initialiseren",
            has_openai_service=openai_service is not None,
            has_memory_repo=memory_repo is not None,
            has_belief_repo=belief_repo is not None,
            has_curiosity_repo=curiosity_repo is not None,
            has_system_runner=system_runner is not None,
            has_audit_service=audit_service is not None,
            default_model=default_model,
            max_jobs_per_turn=max_jobs_per_turn,
        )
        self.openai = openai_service
        self.memory_repo = memory_repo
        self.belief_repo = belief_repo
        self.curiosity_repo = curiosity_repo
        self.system_runner = system_runner
        self.audit = audit_service
        self.default_model = default_model
        self.max_jobs_per_turn = max_jobs_per_turn

        self.context_builder = SystemContextBuilder(
            memory_repo=self.memory_repo,
            belief_repo=self.belief_repo,
        )
        log.debugx("SystemContextBuilder aangemaakt voor SystemCognitionService")

        self.memory_assistant = MemorySystemAssistant()
        log.debugx("MemorySystemAssistant aangemaakt")

        self.curiosity_assistant = CuriositySystemAssistant()
        log.debugx("CuriositySystemAssistant aangemaakt")

        self.research_assistant = ResearchSystemAssistant()
        log.debugx("ResearchSystemAssistant aangemaakt")

        self.belief_assistant = BeliefSystemAssistant()
        log.debugx("BeliefSystemAssistant aangemaakt")

        self._background_tasks: set[asyncio.Task] = set()
        log.infox(
            "SystemCognitionService geïnitialiseerd",
            default_model=self.default_model,
            max_jobs_per_turn=self.max_jobs_per_turn,
            background_task_count=len(self._background_tasks),
        )

        self.turn_interpretation_assistant = TurnInterpretationSystemAssistant()
        log.debugx("TurnInterpretationSystemAssistant aangemaakt")

        self.cognition_router_assistant = CognitionRouterSystemAssistant()
        log.debugx("CognitionRouterSystemAssistant aangemaakt")

        self.research_observation_assistant = ResearchObservationSystemAssistant()
        log.debugx("ResearchObservationSystemAssistant aangemaakt")

        self.planner_memory_retrieval_decision_assistant = PlannerMemoryRetrievalDecisionAssistant()
        log.debugx("PlannerMemoryRetrievalDecisionAssistant aangemaakt")

        self.router_memory_retrieval_decision_assistant = RouterMemoryRetrievalDecisionAssistant()
        log.debugx("RouterMemoryRetrievalDecisionAssistant aangemaakt")

        self.embedding_service = SystemEmbeddingService(openai_service=self.openai)
        log.debugx("SystemEmbeddingService aangemaakt")

    def _attach_memory_embedding(self, record: MemoryRecord) -> MemoryRecord:
        try:
            text = self.embedding_service.memory_text(record.to_dict())
            embedded = self.embedding_service.embed_text(text)

            record.embedding = embedded["embedding"]
            record.embedding_model = embedded["embedding_model"]
            record.embedding_hash = embedded["embedding_hash"]
            record.embedding_updated_at = embedded["embedding_updated_at"]

        except Exception as exc:
            log.warningx(
                "Memory embedding maken mislukt; memory wordt zonder embedding opgeslagen",
                memory_id=record.id,
                memory_type=record.type,
                error=repr(exc),
            )

        return record

    def _attach_belief_embedding(self, record: BeliefRecord) -> BeliefRecord:
        try:
            text = self.embedding_service.belief_text(record.to_dict())
            embedded = self.embedding_service.embed_text(text)

            record.embedding = embedded["embedding"]
            record.embedding_model = embedded["embedding_model"]
            record.embedding_hash = embedded["embedding_hash"]
            record.embedding_updated_at = embedded["embedding_updated_at"]

        except Exception as exc:
            log.warningx(
                "Belief embedding maken mislukt; belief wordt zonder embedding opgeslagen",
                belief_id=record.id,
                topic=record.topic,
                error=repr(exc),
            )

        return record

    @staticmethod
    def _memory_decision_off() -> bool:
        """The memory-retrieval decision runs ONLY when the chat.memory_decision
        slot has a model assigned. Unassigned = the step is off — it must not
        silently borrow the planner model (user decision, 2026-07-04)."""
        try:
            from db.database import SessionLocal
            from services.providers.registry_service import ProviderRegistryService
            with SessionLocal() as db:
                return ProviderRegistryService(db).resolve_slot("chat.memory_decision") is None
        except Exception:  # noqa: BLE001 — on lookup failure, keep the step off
            return True

    @staticmethod
    def _memory_decision_disabled_result() -> Dict[str, Any]:
        return {
            "ok": True,
            "should_retrieve": False,
            "reason": "Memory decision slot (chat.memory_decision) is unassigned — step disabled.",
            "query": None,
            "scopes": [],
            "types": [],
        }

    async def decide_planner_memory_retrieval(
        self,
        *,
        question: str,
        active_conversation_state: Optional[Dict[str, Any]],
        thread_id: Optional[str],
        project_id: Optional[str],
        turn_id: int,
        trace: List[dict],
        progress_cb=None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        if model is None and self._memory_decision_off():
            return self._memory_decision_disabled_result()
        result = await self.system_runner.run(
            assistant=self.planner_memory_retrieval_decision_assistant,
            prompt_kwargs={
                "question": question,
                "active_conversation_state": active_conversation_state or {},
                "thread_id": thread_id,
                "project_id": project_id,
            },
            session_id=thread_id,
            turn_id=turn_id,
            trace=trace,
            model=model,  # role memory_decision: → chat.memory_decision slot
            role="memory_decision:planner",
            progress_cb=progress_cb,
        )

        if not result.get("ok"):
            return {
                "ok": False,
                "should_retrieve": False,
                "reason": "Planner memory retrieval decision failed; skipping memory retrieval.",
                "query": None,
                "scopes": [],
                "types": [],
                "raw": result,
            }

        plan = result.get("result") or {}

        should_retrieve = bool(plan.get("should_retrieve", False))
        query = (plan.get("query") or "").strip() or None

        scopes = plan.get("scopes") or []
        if not isinstance(scopes, list):
            scopes = []

        types = plan.get("types") or []
        if not isinstance(types, list):
            types = []

        scopes = [str(s).strip().lower() for s in scopes if str(s).strip()]
        types = [str(t).strip() for t in types if str(t).strip()]

        if should_retrieve and not query:
            should_retrieve = False

        return {
            "ok": True,
            "should_retrieve": should_retrieve,
            "reason": plan.get("reason") or "",
            "query": query,
            "scopes": scopes,
            "types": types,
            "raw": result,
        }

    async def decide_router_memory_retrieval(
        self,
        *,
        question: str,
        active_conversation_state: Optional[Dict[str, Any]],
        thread_id: Optional[str],
        project_id: Optional[str],
        turn_id: int,
        trace: List[dict],
        progress_cb=None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        if model is None and self._memory_decision_off():
            return self._memory_decision_disabled_result()
        result = await self.system_runner.run(
            assistant=self.router_memory_retrieval_decision_assistant,
            prompt_kwargs={
                "question": question,
                "active_conversation_state": active_conversation_state or {},
                "thread_id": thread_id,
                "project_id": project_id,
            },
            session_id=thread_id,
            turn_id=turn_id,
            trace=trace,
            model=model,  # role memory_decision: → chat.memory_decision slot
            role="memory_decision:router",
            progress_cb=progress_cb,
        )

        if not result.get("ok"):
            return {
                "ok": False,
                "should_retrieve": False,
                "reason": "Router memory retrieval decision failed; skipping router memory retrieval.",
                "query": None,
                "raw": result,
            }

        plan = result.get("result") or {}

        should_retrieve = bool(plan.get("should_retrieve", False))
        query = (plan.get("query") or "").strip() or None

        if should_retrieve and not query:
            should_retrieve = False

        return {
            "ok": True,
            "should_retrieve": should_retrieve,
            "reason": plan.get("reason") or "",
            "query": query,
            "raw": result,
        }

    def _decide_memory_scope(
            self,
            *,
            item: Dict[str, Any],
            turn_interpretation: Dict[str, Any],
            thread_id: Optional[str],
            project_id: Optional[str],
    ) -> str:
        explicit = (item.get("scope") or "").strip().lower()

        if explicit in {"thread", "project", "global"}:
            if explicit == "project" and not project_id:
                return "thread"
            return explicit

        importance = float(item.get("importance", 0.5) or 0.5)
        memory_type = (item.get("type") or "").strip().lower()

        project_types = {
            "architecture_decision",
            "implementation_detail",
            "project_context",
            "constraint",
            "technical_decision",
        }

        global_types = {
            "user_preference",
            "durable_preference",
        }

        if memory_type in project_types and project_id and importance >= 0.65:
            return "project"

        if memory_type in global_types and importance >= 0.85:
            return "global"

        return "thread"
    def _resolve_scoped_target(
            self,
            *,
            requested_scope: Optional[str],
            fallback_scope: Optional[str],
            thread_id: Optional[str],
            project_id: Optional[str],
    ) -> Dict[str, Optional[str]]:
        scope = (requested_scope or fallback_scope or "").strip().lower()

        if scope not in {"thread", "project", "global"}:
            scope = "project" if project_id else "thread"

        if scope == "project" and not project_id:
            scope = "thread"

        if scope == "global":
            return {
                "scope": "global",
                "thread_id": None,
                "project_id": None,
            }

        if scope == "project":
            return {
                "scope": "project",
                "thread_id": None,
                "project_id": project_id,
            }

        return {
            "scope": "thread",
            "thread_id": thread_id,
            "project_id": None,
        }

    async def process_queued_curiosity_jobs(
            self,
            *,
            limit: int = 1,
            thread_id: Optional[str] = "cognition_curiosity_scheduler",
            turn_id: int = 0,
    ) -> Dict[str, Any]:
        processed: List[Dict[str, Any]] = []

        for _ in range(limit):
            job = await self.curiosity_repo.claim_next()
            if not job:
                break

            metadata = job.get("metadata_") or {}

            result = await self._process_curiosity_job(
                job=job,
                thread_id=job.get("thread_id") or metadata.get("origin_thread_id") or thread_id,
                project_id=job.get("project_id") or metadata.get("origin_project_id"),
                turn_id=turn_id,
                trace=[],
                progress_cb=None,
                turn_interpretation={},
            )
            processed.append(result)

        return {
            "ok": True,
            "processed_count": len(processed),
            "processed": processed,
        }

    async def _run_cognition_router(
            self,
            *,
            question: str,
            answer: str,
            existing_context: Dict[str, Any],
            thread_id: Optional[str],
            project_id: Optional[str] = None,
            turn_id: int,
            trace: List[dict],
            progress_cb=None,
    ) -> Dict[str, Any]:
        compact_context = compact_existing_context(existing_context, max_memories=3, max_beliefs=3)

        result = await self.system_runner.run(
            assistant=self.cognition_router_assistant,
            prompt_kwargs={
                "question": question,
                "answer": answer,
                "existing_context": compact_context,
                "project_id": project_id,
            },
            session_id=thread_id,
            turn_id=turn_id,
            trace=trace,
            model=self.default_model,
            progress_cb=progress_cb,
        )

        if not result.get("ok"):
            return {
                "ok": False,
                "mode": "standard",
                "reason": "Router failed; defaulting to standard cognition.",
                "raw": result,
            }

        plan = result.get("result") or {}
        mode = (plan.get("mode") or "standard").strip()

        if mode not in {"skip", "light", "standard", "deep"}:
            mode = "standard"

        if mode == "skip":
            return {
                "ok": True,
                "mode": "skip",
                "reason": plan.get("reason") or "",
                "run_interpretation": False,
                "run_memory": False,
                "run_curiosity": False,
                "run_deep_research": False,
                "priority": 0.0,
                "raw": result,
            }

        return {
            "ok": True,
            "mode": mode,
            "reason": plan.get("reason") or "",
            "run_interpretation": bool(plan.get("run_interpretation", mode != "skip")),
            "run_memory": bool(plan.get("run_memory", mode in {"light", "standard", "deep"})),
            "run_curiosity": bool(plan.get("run_curiosity", mode in {"standard", "deep"})),
            "run_deep_research": bool(plan.get("run_deep_research", mode == "deep")),
            "priority": float(plan.get("priority", 0.5)),
            "raw": result,
        }

    async def _run_research_observation(
            self,
            *,
            topic: str,
            reason: str,
            depth: str,
            question: str,
            answer: str,
            research_docs: Dict[str, Any],
            existing_context: Dict[str, Any],
            turn_interpretation: Dict[str, Any],
            thread_id: Optional[str],
            turn_id: int,
            trace: List[dict],
            progress_cb=None,
    ) -> Dict[str, Any]:
        compact_context = compact_existing_context(existing_context, max_memories=3, max_beliefs=3)
        compact_research_docs = compact_research_docs_for_belief(research_docs)
        compact_interpretation = compact_interpretation_for_belief(turn_interpretation)

        result = await self.system_runner.run(
            assistant=self.research_observation_assistant,
            prompt_kwargs={
                "topic": topic,
                "reason": reason,
                "depth": depth,
                "question": question,
                "answer": answer,
                "research_docs": compact_research_docs,
                "existing_context": compact_context,
                "turn_interpretation": compact_interpretation,
            },
            session_id=thread_id,
            turn_id=turn_id,
            trace=trace,
            model=self.default_model,
            progress_cb=progress_cb,
        )

        if not result.get("ok"):
            return {
                "ok": False,
                "observation_pack": {},
                "raw": result,
            }

        plan = result.get("result") or {}
        observation_pack = plan.get("observation_pack") or {}

        if not isinstance(observation_pack, dict):
            observation_pack = {}

        return {
            "ok": True,
            "observation_pack": observation_pack,
            "raw": result,
        }

    async def _run_turn_interpretation(
            self,
            *,
            question: str,
            answer: str,
            existing_context: Dict[str, Any],
            thread_id: Optional[str],
            turn_id: int,
            trace: List[dict],
            progress_cb=None,
    ) -> Dict[str, Any]:
        log.infox(
            "Turn interpretation assistant uitvoeren gestart",
            thread_id=thread_id,
            turn_id=turn_id,
            question_length=len(question or ""),
            answer_length=len(answer or ""),
            existing_context_keys=list(existing_context.keys()) if isinstance(existing_context, dict) else None,
            memory_count=len(existing_context.get("memories") or []) if isinstance(existing_context, dict) else None,
            belief_count=len(existing_context.get("beliefs") or []) if isinstance(existing_context, dict) else None,
            trace_count=len(trace or []),
            has_progress_cb=progress_cb is not None,
            model=self.default_model,
        )

        t0 = time.time()

        try:
            result = await self.system_runner.run(
                assistant=self.turn_interpretation_assistant,
                prompt_kwargs={
                    "question": question,
                    "answer": answer,
                    "existing_context": existing_context,
                },
                session_id=thread_id,
                turn_id=turn_id,
                trace=trace,
                model=self.default_model,
                progress_cb=progress_cb,
            )
        except Exception as exc:
            log.errorx(
                "Turn interpretation assistant uitvoeren mislukt",
                thread_id=thread_id,
                turn_id=turn_id,
                elapsed_ms=int((time.time() - t0) * 1000),
                error=repr(exc),
                exc_info=True,
            )
            return {
                "ok": False,
                "interpretation": {},
                "raw": {
                    "ok": False,
                    "error": repr(exc),
                },
            }

        elapsed_ms = int((time.time() - t0) * 1000)

        log.debugx(
            "Turn interpretation assistant resultaat ontvangen",
            thread_id=thread_id,
            turn_id=turn_id,
            elapsed_ms=elapsed_ms,
            ok=result.get("ok") if isinstance(result, dict) else None,
            result_keys=list(result.keys()) if isinstance(result, dict) else None,
        )

        if not result.get("ok"):
            log.warningx(
                "Turn interpretation assistant niet succesvol",
                thread_id=thread_id,
                turn_id=turn_id,
                elapsed_ms=elapsed_ms,
                result_keys=list(result.keys()) if isinstance(result, dict) else None,
                error=result.get("error") if isinstance(result, dict) else None,
            )
            return {
                "ok": False,
                "interpretation": {},
                "raw": result,
            }

        plan = result.get("result") or {}
        interpretation = plan.get("interpretation") or {}

        if not isinstance(interpretation, dict):
            log.warningx(
                "Turn interpretation assistant gaf ongeldige interpretation shape terug",
                thread_id=thread_id,
                turn_id=turn_id,
                elapsed_ms=elapsed_ms,
                interpretation_type=type(interpretation).__name__,
                plan_keys=list(plan.keys()) if isinstance(plan, dict) else None,
            )
            interpretation = {}

        log.infox(
            "Turn interpretation assistant afgerond",
            thread_id=thread_id,
            turn_id=turn_id,
            elapsed_ms=elapsed_ms,
            interpretation_keys=list(interpretation.keys()),
            intent=interpretation.get("intent"),
            domain=interpretation.get("domain"),
            importance=interpretation.get("importance"),
            should_remember=interpretation.get("should_remember"),
            should_research=interpretation.get("should_research"),
        )

        return {
            "ok": True,
            "interpretation": interpretation,
            "raw": result,
        }

    async def pre_context(
            self,
            *,
            question: str,
            thread_id: Optional[str],
            project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        log.infox(
            "System cognition pre_context bouwen gestart",
            thread_id=thread_id,
            project_id=project_id,
            question_length=len(question or ""),
            top_k_memories=8,
            top_k_beliefs=8,
        )
        result = await self.context_builder.build(
            query=question,
            thread_id=thread_id,
            project_id=project_id,
            top_k_memories=8,
            top_k_beliefs=8,
        )
        log.infox(
            "System cognition pre_context bouwen afgerond",
            thread_id=thread_id,
            project_id=project_id,
            context_keys=list(result.keys()) if isinstance(result, dict) else None,
            memory_count=len(result.get("memories", []) or []) if isinstance(result, dict) else None,
            belief_count=len(result.get("beliefs", []) or []) if isinstance(result, dict) else None,
        )
        return result

    # ── Fase 3: agent-blackbox cognition ─────────────────────────────────────────

    @staticmethod
    def _cognition_is_agent_mode() -> bool:
        """True when the chat.cognition slot resolves to a CLI-agent provider."""
        try:
            from db.database import SessionLocal
            from services.providers.execution_mode import slot_mode
            with SessionLocal() as db:
                return slot_mode(db, "chat.cognition") == "agent"
        except Exception:  # noqa: BLE001 — on lookup failure, use the model pipeline
            return False

    @staticmethod
    def _clamp01(value: Any, default: float) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        return max(0.0, min(1.0, v))

    def _agent_memory_record(self, item: Any, thread_id, project_id) -> Optional[MemoryRecord]:
        if not isinstance(item, dict):
            return None
        content = str(item.get("content") or "").strip()
        if not content:
            return None
        tgt = self._resolve_scoped_target(
            requested_scope=item.get("scope"), fallback_scope=None,
            thread_id=thread_id, project_id=project_id)
        return MemoryRecord(
            type=str(item.get("type") or "note"), content=content,
            scope=tgt["scope"], thread_id=tgt["thread_id"], project_id=tgt["project_id"],
            importance=self._clamp01(item.get("importance"), 0.5),
            metadata_={"source": "agent_cognition"})

    def _agent_belief_record(self, item: Any, thread_id, project_id) -> Optional[BeliefRecord]:
        if not isinstance(item, dict):
            return None
        topic = str(item.get("topic") or "").strip()
        summary = str(item.get("summary") or "").strip()
        content = str(item.get("content") or "").strip()
        if not (topic and (summary or content)):
            return None
        tgt = self._resolve_scoped_target(
            requested_scope=None, fallback_scope=None,
            thread_id=thread_id, project_id=project_id)
        return BeliefRecord(
            topic=topic, summary=summary or None, content=content or summary,
            domain=(str(item.get("domain")) if item.get("domain") else None),
            confidence=self._clamp01(item.get("confidence"), 0.5), status="tentative",
            scope=tgt["scope"], thread_id=tgt["thread_id"], project_id=tgt["project_id"],
            metadata_={"source": "agent_cognition"})

    def _agent_curiosity_job(self, item: Any, question, answer, thread_id, project_id) -> Optional[CuriosityJob]:
        if not isinstance(item, dict):
            return None
        topic = str(item.get("topic") or "").strip()
        if not topic:
            return None
        return CuriosityJob(
            topic=topic, reason=str(item.get("reason") or ""),
            scope=("project" if project_id else "thread"),
            project_id=project_id, thread_id=thread_id,
            source_question=question, source_answer=answer,
            metadata_={"source": "agent_cognition"})

    async def _post_turn_via_agent(self, *, question, answer, thread_id, project_id, turn_id, t0) -> Dict[str, Any]:
        from db.database import SessionLocal
        from services.system_cognition.cognition_agent_runner import CognitionAgentRunner
        try:
            with SessionLocal() as db:
                env = await CognitionAgentRunner(db).extract(
                    question=question, answer=answer, model=self.default_model)
        except Exception as exc:  # noqa: BLE001 — never let cognition break the turn
            log.warningx("Agent-cognition extractie mislukt", turn_id=turn_id, error=str(exc))
            return {"ok": False, "mode": "agent", "elapsed_ms": int((time.time() - t0) * 1000),
                    "route": {"engine": "agent", "error": str(exc)}, "interpretation": None,
                    "memory": None, "curiosity": None, "processed_jobs": []}

        saved_mem, saved_bel, queued_cur = [], [], []
        for m in env.get("memories") or []:
            rec = self._agent_memory_record(m, thread_id, project_id)
            if rec is not None:
                self._attach_memory_embedding(rec)
                await self.memory_repo.upsert(rec)
                saved_mem.append(rec.id)
        for b in env.get("beliefs") or []:
            rec = self._agent_belief_record(b, thread_id, project_id)
            if rec is not None:
                self._attach_belief_embedding(rec)
                await self.belief_repo.upsert(rec)
                saved_bel.append(rec.id)
        for c in env.get("curiosity") or []:
            job = self._agent_curiosity_job(c, question, answer, thread_id, project_id)
            if job is not None:
                await self.curiosity_repo.enqueue(job)
                queued_cur.append(job.id)

        log.infox("Agent-cognition post_turn afgerond", turn_id=turn_id,
                  memories=len(saved_mem), beliefs=len(saved_bel), curiosity=len(queued_cur),
                  decision=env.get("decision"))
        return {"ok": True, "mode": "agent", "elapsed_ms": int((time.time() - t0) * 1000),
                "route": {"engine": "agent", "decision": env.get("decision")},
                "interpretation": None,
                "memory": {"saved_ids": saved_mem, "saved_belief_ids": saved_bel},
                "curiosity": {"queued_ids": queued_cur}, "processed_jobs": []}

    async def post_turn(
            self,
            *,
            question: str,
            answer: str,
            thread_id: Optional[str],
            project_id: Optional[str] = None,
            turn_id: int,
            trace: Optional[List[dict]] = None,
            progress_cb=None,
            force_important: bool = False,
    ) -> Dict[str, Any]:
        log.infox(
            "System cognition post_turn gestart",
            thread_id=thread_id,
            project_id=project_id,
            turn_id=turn_id,
            question_length=len(question or ""),
            answer_length=len(answer or ""),
            trace_count=len(trace or []),
            has_progress_cb=progress_cb is not None,
            max_jobs_per_turn=self.max_jobs_per_turn,
            default_model=self.default_model,
        )
        trace = trace or []
        t0 = time.time()

        # Fase 3 — agent blackbox: when the cognition slot resolves to a CLI-agent
        # provider, do the whole decide+extract in ONE call and persist, instead of
        # the multi-step model pipeline below (which a CLI agent can't schema-enforce).
        if self._cognition_is_agent_mode():
            return await self._post_turn_via_agent(
                question=question, answer=answer, thread_id=thread_id,
                project_id=project_id, turn_id=turn_id, t0=t0)

        existing_context = await self.pre_context(question=question, thread_id=thread_id, project_id=project_id)
        log.debugx(
            "System cognition bestaande context opgehaald",
            thread_id=thread_id,
            turn_id=turn_id,
            context_keys=list(existing_context.keys()) if isinstance(existing_context, dict) else None,
        )
        cognition_route = await self._run_cognition_router(
            question=question,
            answer=answer,
            existing_context=existing_context,
            thread_id=thread_id,
            project_id=project_id,
            turn_id=turn_id,
            trace=trace,
            progress_cb=progress_cb,
        )

        mode = cognition_route.get("mode") or "standard"

        # A user-flagged "important" message always feeds memory/belief/curiosity,
        # overriding the triviality router's skip decision.
        if force_important:
            mode = "standard"
            cognition_route["run_memory"] = True
            cognition_route["run_curiosity"] = True

        if mode == "skip":
            final_result = {
                "ok": True,
                "mode": mode,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "route": cognition_route,
                "interpretation": None,
                "memory": None,
                "curiosity": None,
                "processed_jobs": [],
            }
            log.infox(
                "System cognition post_turn overgeslagen door router",
                thread_id=thread_id,
                turn_id=turn_id,
                mode=mode,
                reason=cognition_route.get("reason"),
            )
            return final_result

        interpretation_result = await self._run_turn_interpretation(
            question=question,
            answer=answer,
            existing_context=compact_existing_context(existing_context, max_memories=3, max_beliefs=3),
            thread_id=thread_id,
            turn_id=turn_id,
            trace=trace,
            progress_cb=progress_cb,
        )

        turn_interpretation = interpretation_result.get("interpretation") or {}

        memory_result = None
        if cognition_route.get("run_memory", True):
            memory_result = await self._run_memory_write(
                question=question,
                answer=answer,
                existing_context=compact_existing_context(existing_context, max_memories=4, max_beliefs=2),
                turn_interpretation=compact_interpretation_for_memory(turn_interpretation),
                thread_id=thread_id,
                project_id=project_id,
                turn_id=turn_id,
                trace=trace,
                progress_cb=progress_cb,
            )

        curiosity_result = None
        if cognition_route.get("run_curiosity", mode in {"standard", "deep"}):
            curiosity_result = await self._run_curiosity_gate(
                question=question,
                answer=answer,
                existing_context=compact_existing_context(existing_context, max_memories=3, max_beliefs=4),
                turn_interpretation=compact_interpretation_for_curiosity(turn_interpretation),
                thread_id=thread_id,
                project_id=project_id,
                turn_id=turn_id,
                trace=trace,
                progress_cb=progress_cb,
            )

        processed_jobs = []

        if mode == "deep" or cognition_route.get("run_deep_research"):
            for _ in range(self.max_jobs_per_turn):
                job = await self.curiosity_repo.claim_next()
                if not job:
                    break

                metadata = job.get("metadata_") or {}

                result = await self._process_curiosity_job(
                    job=job,
                    thread_id=job.get("thread_id") or metadata.get("origin_thread_id") or thread_id,
                    project_id=job.get("project_id") or metadata.get("origin_project_id") or project_id,
                    turn_id=turn_id,
                    trace=trace,
                    progress_cb=progress_cb,
                    turn_interpretation=compact_interpretation_for_belief(turn_interpretation),
                )
                processed_jobs.append(result)
        else:
            log.infox(
                "Deep research processing overgeslagen voor cognition mode",
                thread_id=thread_id,
                turn_id=turn_id,
                mode=mode,
                queued_only=True,
            )

        final_result = {
            "ok": True,
            "mode": mode,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "route": cognition_route,
            "interpretation": interpretation_result,
            "memory": memory_result,
            "curiosity": curiosity_result,
            "processed_jobs": processed_jobs,
        }

        log.infox(
            "System cognition post_turn afgerond",
            thread_id=thread_id,
            turn_id=turn_id,
            mode=mode,
            elapsed_ms=final_result["elapsed_ms"],
            memory_ok=memory_result.get("ok") if isinstance(memory_result, dict) else None,
            curiosity_ok=curiosity_result.get("ok") if isinstance(curiosity_result, dict) else None,
            processed_jobs_count=len(processed_jobs),
        )

        return final_result

    async def _run_memory_write(
            self,
            *,
            question: str,
            answer: str,
            existing_context: Dict[str, Any],
            turn_interpretation: Dict[str, Any],
            thread_id: Optional[str],
            project_id: Optional[str] = None,
            turn_id: int,
            trace: List[dict],
            progress_cb=None,
    ) -> Dict[str, Any]:
        log.infox(
            "System cognition memory write gestart",
            thread_id=thread_id,
            turn_id=turn_id,
            question_length=len(question or ""),
            answer_length=len(answer or ""),
            existing_context_keys=list(existing_context.keys()) if isinstance(existing_context, dict) else None,
            trace_count=len(trace or []),
            has_progress_cb=progress_cb is not None,
            model=self.default_model,
        )
        result = await self.system_runner.run(
            assistant=self.memory_assistant,
            prompt_kwargs={
                "question": question,
                "answer": answer,
                "existing_context": existing_context,
                "turn_interpretation": turn_interpretation,
            },
            session_id=thread_id,
            turn_id=turn_id,
            trace=trace,
            model=self.default_model,
            progress_cb=progress_cb,
        )

        log.debugx(
            "System cognition memory assistant resultaat ontvangen",
            thread_id=thread_id,
            turn_id=turn_id,
            ok=result.get("ok") if isinstance(result, dict) else None,
            result_keys=list(result.keys()) if isinstance(result, dict) else None,
        )

        if not result.get("ok"):
            log.warningx(
                "System cognition memory write niet succesvol",
                thread_id=thread_id,
                turn_id=turn_id,
                result_keys=list(result.keys()) if isinstance(result, dict) else None,
            )
            return result

        plan = result.get("result") or {}
        memories = plan.get("memories") or []
        stored = []

        log.infox(
            "System cognition memories uit plan verwerken",
            thread_id=thread_id,
            turn_id=turn_id,
            memory_count=len(memories) if isinstance(memories, list) else None,
        )

        for item in memories:
            if not isinstance(item, dict):
                log.debugx(
                    "Memory item overgeslagen: geen dict",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_type=type(item).__name__,
                )
                continue

            content = (item.get("content") or "").strip()
            if not content:
                log.debugx(
                    "Memory item overgeslagen: content ontbreekt",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_keys=list(item.keys()),
                )
                continue

            log.debugx(
                "MemoryRecord aanmaken vanuit assistant plan",
                thread_id=thread_id,
                turn_id=turn_id,
                memory_type=item.get("type") or "note",
                content_length=len(content),
                importance=item.get("importance", 0.5),
                pinned=bool(item.get("pinned", False)),
                metadata_keys=list((item.get("metadata_") or {}).keys()) if isinstance(item.get("metadata_"), dict) else None,
            )
            memory_type = (item.get("type") or "note").strip()

            if memory_type == "router_memory":
                target_scope = "global"
                target_thread_id = "cognition_router"
                target_project_id = None
            else:
                target_scope = self._decide_memory_scope(
                    item=item,
                    turn_interpretation=turn_interpretation,
                    thread_id=thread_id,
                    project_id=project_id,
                )
                target_thread_id = thread_id if target_scope == "thread" else None
                target_project_id = project_id if target_scope == "project" else None

            metadata = item.get("metadata_") if isinstance(item.get("metadata_"), dict) else {}
            metadata.setdefault("source", "memory_system_assistant")
            metadata.setdefault("origin_thread_id", thread_id)
            metadata.setdefault("origin_project_id", project_id)

            if memory_type == "router_memory":
                metadata.setdefault("router_memory", True)

            record = MemoryRecord(
                type=memory_type,
                content=content,
                scope=target_scope,
                thread_id=target_thread_id,
                project_id=target_project_id,
                importance=float(item.get("importance", 0.5)),
                pinned=bool(item.get("pinned", False)),
                metadata_=metadata,
            )
            stored_record = await self.memory_repo.upsert(record)
            stored.append(stored_record.to_dict())
            log.infox(
                "MemoryRecord opgeslagen",
                thread_id=thread_id,
                turn_id=turn_id,
                memory_id=getattr(stored_record, "id", None),
                stored_count=len(stored),
            )

        final_result = {
            "ok": True,
            "stored_count": len(stored),
            "stored": stored,
        }
        log.infox(
            "System cognition memory write afgerond",
            thread_id=thread_id,
            turn_id=turn_id,
            stored_count=len(stored),
        )
        return final_result

    async def _run_curiosity_gate(
            self,
            *,
            question: str,
            answer: str,
            existing_context: Dict[str, Any],
            turn_interpretation: Dict[str, Any],
            thread_id: Optional[str],
            project_id: Optional[str] = None,
            turn_id: int,
            trace: List[dict],
            progress_cb=None,
    ) -> Dict[str, Any]:
        log.infox(
            "System cognition curiosity gate gestart",
            thread_id=thread_id,
            project_id=project_id,
            turn_id=turn_id,
            question_length=len(question or ""),
            answer_length=len(answer or ""),
            existing_context_keys=list(existing_context.keys()) if isinstance(existing_context, dict) else None,
            trace_count=len(trace or []),
            has_progress_cb=progress_cb is not None,
            model=self.default_model,
        )

        result = await self.system_runner.run(
            assistant=self.curiosity_assistant,
            prompt_kwargs={
                "question": question,
                "answer": answer,
                "existing_context": existing_context,
                "turn_interpretation": turn_interpretation,
                "project_id": project_id,
            },
            session_id=thread_id,
            turn_id=turn_id,
            trace=trace,
            model=self.default_model,
            progress_cb=progress_cb,
        )

        log.debugx(
            "Curiosity assistant resultaat ontvangen",
            thread_id=thread_id,
            project_id=project_id,
            turn_id=turn_id,
            ok=result.get("ok") if isinstance(result, dict) else None,
            result_keys=list(result.keys()) if isinstance(result, dict) else None,
        )

        if not result.get("ok"):
            log.warningx(
                "System cognition curiosity gate niet succesvol",
                thread_id=thread_id,
                project_id=project_id,
                turn_id=turn_id,
                result_keys=list(result.keys()) if isinstance(result, dict) else None,
            )
            return result

        plan = result.get("result") or {}
        jobs = plan.get("jobs") or []
        enqueued = []

        log.infox(
            "Curiosity jobs uit plan verwerken",
            thread_id=thread_id,
            project_id=project_id,
            turn_id=turn_id,
            job_count=len(jobs) if isinstance(jobs, list) else None,
        )

        for item in jobs:
            if not isinstance(item, dict):
                log.debugx(
                    "Curiosity job item overgeslagen: geen dict",
                    thread_id=thread_id,
                    project_id=project_id,
                    turn_id=turn_id,
                    item_type=type(item).__name__,
                )
                continue

            topic = (item.get("topic") or "").strip()
            if not topic:
                log.debugx(
                    "Curiosity job item overgeslagen: topic ontbreekt",
                    thread_id=thread_id,
                    project_id=project_id,
                    turn_id=turn_id,
                    item_keys=list(item.keys()),
                )
                continue

            reason = (item.get("reason") or "").strip()
            depth = (item.get("depth") or "small").strip()
            priority = float(item.get("priority", 0.5) or 0.5)

            target = self._resolve_scoped_target(
                requested_scope=item.get("scope"),
                fallback_scope="project" if project_id else "thread",
                thread_id=thread_id,
                project_id=project_id,
            )

            metadata = item.get("metadata_") if isinstance(item.get("metadata_"), dict) else {}
            metadata.setdefault("origin_thread_id", thread_id)
            metadata.setdefault("origin_project_id", project_id)
            metadata.setdefault("source", "curiosity_system_assistant")
            metadata.setdefault("scope", target["scope"])

            log.debugx(
                "CuriosityJob aanmaken vanuit assistant plan",
                thread_id=thread_id,
                project_id=project_id,
                turn_id=turn_id,
                topic=topic,
                depth=depth,
                priority=priority,
                scope=target["scope"],
                has_reason=bool(reason),
                metadata_keys=list(metadata.keys()),
            )

            job = CuriosityJob(
                topic=topic,
                reason=reason,
                depth=depth,
                priority=priority,
                status="queued",
                scope=target["scope"],
                thread_id=target["thread_id"],
                project_id=target["project_id"],
                source_question=question,
                source_answer=answer,
                metadata_=metadata,
            )

            stored = await self.curiosity_repo.enqueue(job)
            enqueued.append(stored.to_dict())

            log.infox(
                "CuriosityJob opgeslagen in queue",
                thread_id=thread_id,
                project_id=project_id,
                turn_id=turn_id,
                job_id=getattr(stored, "id", None),
                topic=topic,
                scope=target["scope"],
                enqueued_count=len(enqueued),
            )

        final_result = {
            "ok": True,
            "enqueued_count": len(enqueued),
            "enqueued": enqueued,
        }

        log.infox(
            "System cognition curiosity gate afgerond",
            thread_id=thread_id,
            project_id=project_id,
            turn_id=turn_id,
            enqueued_count=len(enqueued),
        )

        return final_result

    async def _process_curiosity_job(
            self,
            *,
            job: Dict[str, Any],
            thread_id: Optional[str],
            project_id: Optional[str] = None,
            turn_id: int,
            trace: List[dict],
            progress_cb=None,
            turn_interpretation: Dict[str, Any],
    ) -> Dict[str, Any]:
        log.infox(
            "Curiosity job verwerking gestart",
            job_id=job.get("id") if isinstance(job, dict) else None,
            thread_id=thread_id,
            project_id=project_id,
            turn_id=turn_id,
            topic=job.get("topic") if isinstance(job, dict) else None,
            depth=job.get("depth") if isinstance(job, dict) else None,
            trace_count=len(trace or []),
            has_progress_cb=progress_cb is not None,
            model=self.default_model,
        )

        job_id = job.get("id")
        topic = job.get("topic") or ""
        reason = job.get("reason") or ""
        depth = job.get("depth") or "small"
        job_metadata = job.get("metadata_") or {}

        job_scope = (
                job.get("scope")
                or job_metadata.get("scope")
                or ("project" if project_id else "thread")
        )

        target_for_job = self._resolve_scoped_target(
            requested_scope=job_scope,
            fallback_scope="project" if project_id else "thread",
            thread_id=thread_id,
            project_id=project_id,
        )

        effective_thread_id = thread_id or job_metadata.get("origin_thread_id")
        effective_project_id = project_id or job_metadata.get("origin_project_id")

        try:
            existing_context = await self.pre_context(
                question=f"{topic}\n{reason}",
                thread_id=effective_thread_id,
                project_id=effective_project_id,
            )

            log.debugx(
                "Curiosity job bestaande context opgehaald",
                job_id=job_id,
                thread_id=effective_thread_id,
                project_id=effective_project_id,
                turn_id=turn_id,
                context_keys=list(existing_context.keys()) if isinstance(existing_context, dict) else None,
            )

            log.infox(
                "Curiosity job research assistant uitvoeren gestart",
                job_id=job_id,
                topic=topic,
                depth=depth,
                thread_id=effective_thread_id,
                project_id=effective_project_id,
                turn_id=turn_id,
            )

            research = await self.system_runner.run(
                assistant=self.research_assistant,
                prompt_kwargs={
                    "topic": topic,
                    "reason": reason,
                    "depth": depth,
                    "existing_context": compact_existing_context(existing_context, max_memories=2, max_beliefs=2),
                    "project_id": effective_project_id,
                },
                session_id=effective_thread_id,
                turn_id=turn_id,
                trace=trace,
                model=self.default_model,
                progress_cb=progress_cb,
            )

            log.infox(
                "Curiosity job research assistant afgerond",
                job_id=job_id,
                topic=topic,
                ok=research.get("ok") if isinstance(research, dict) else None,
                tool_call_count=len(research.get("tool_calls") or []) if isinstance(research, dict) else None,
                tool_result_count=len(research.get("tool_results") or []) if isinstance(research, dict) else None,
            )

            research_docs = {
                "research_result": research.get("result"),
                "tool_calls": research.get("tool_calls") or [],
                "tool_results": research.get("tool_results") or [],
            }

            observation_result = await self._run_research_observation(
                topic=topic,
                reason=reason,
                depth=depth,
                question=job.get("source_question") or "",
                answer=job.get("source_answer") or "",
                research_docs=research_docs,
                existing_context=existing_context,
                turn_interpretation=turn_interpretation or {},
                thread_id=effective_thread_id,
                turn_id=turn_id,
                trace=trace,
                progress_cb=progress_cb,
            )

            observation_pack = observation_result.get("observation_pack") or {}

            log.infox(
                "Curiosity job research observation afgerond",
                job_id=job_id,
                topic=topic,
                depth=depth,
                ok=observation_result.get("ok") if isinstance(observation_result, dict) else None,
                observation_keys=list(observation_pack.keys()) if isinstance(observation_pack, dict) else None,
                core_observation_count=len(observation_pack.get("core_observations") or []) if isinstance(
                    observation_pack, dict) else None,
                worldview_candidate_count=len(observation_pack.get("worldview_candidates") or []) if isinstance(
                    observation_pack, dict) else None,
            )

            log.infox(
                "Curiosity job belief assistant uitvoeren gestart",
                job_id=job_id,
                topic=topic,
                depth=depth,
                thread_id=effective_thread_id,
                project_id=effective_project_id,
                turn_id=turn_id,
            )

            belief_result = await self.system_runner.run(
                assistant=self.belief_assistant,
                prompt_kwargs={
                    "topic": topic,
                    "reason": reason,
                    "depth": depth,
                    "question": job.get("source_question") or "",
                    "answer": job.get("source_answer") or "",
                    "research_docs": compact_research_docs_for_belief(research_docs),
                    "existing_context": compact_existing_context(existing_context, max_memories=3, max_beliefs=4),
                    "turn_interpretation": compact_interpretation_for_belief(turn_interpretation or {}),
                    "observation_pack": observation_pack,
                    "scope_context": {
                        "job_scope": target_for_job["scope"],
                        "thread_id": effective_thread_id,
                        "project_id": effective_project_id,
                    },
                },
                session_id=effective_thread_id,
                turn_id=turn_id,
                trace=trace,
                model=self.default_model,
                progress_cb=progress_cb,
            )

            log.infox(
                "Curiosity job belief assistant afgerond",
                job_id=job_id,
                topic=topic,
                ok=belief_result.get("ok") if isinstance(belief_result, dict) else None,
            )

            stored_beliefs = []
            stored_memories = []

            if belief_result.get("ok"):
                plan = belief_result.get("result") or {}

                log.infox(
                    "Curiosity job belief plan verwerken",
                    job_id=job_id,
                    topic=topic,
                    belief_count=len(plan.get("beliefs") or []),
                    memory_count=len(plan.get("memories") or []),
                )

                for item in plan.get("beliefs") or []:
                    if not isinstance(item, dict):
                        log.debugx(
                            "Belief item overgeslagen: geen dict",
                            job_id=job_id,
                            item_type=type(item).__name__,
                        )
                        continue

                    raw_content = (item.get("content") or "").strip()
                    summary = (item.get("summary") or "").strip()
                    insights = item.get("insights") if isinstance(item.get("insights"), list) else []
                    future_use = item.get("future_use") if isinstance(item.get("future_use"), list) else []

                    content = raw_content or summary or "\n".join(
                        str(x).strip() for x in insights if str(x).strip()
                    )

                    if not content:
                        log.debugx(
                            "Belief item overgeslagen: content/summary/insights ontbreken",
                            job_id=job_id,
                            item_keys=list(item.keys()),
                        )
                        continue

                    target = self._resolve_scoped_target(
                        requested_scope=item.get("scope"),
                        fallback_scope=target_for_job["scope"],
                        thread_id=effective_thread_id,
                        project_id=effective_project_id,
                    )

                    metadata = item.get("metadata_") if isinstance(item.get("metadata_"), dict) else {}
                    metadata.setdefault("source", "belief_system_assistant")
                    metadata.setdefault("origin_thread_id", effective_thread_id)
                    metadata.setdefault("origin_project_id", effective_project_id)
                    metadata.setdefault("origin_job_id", job_id)

                    log.debugx(
                        "BeliefRecord aanmaken vanuit curiosity job",
                        job_id=job_id,
                        topic=item.get("topic") or topic,
                        domain=item.get("domain"),
                        confidence=item.get("confidence", 0.5),
                        status=item.get("status") or "tentative",
                        importance=item.get("importance", 0.5),
                        scope=target["scope"],
                        content_length=len(content),
                    )

                    belief = BeliefRecord(
                        topic=item.get("topic") or topic,
                        content=content,
                        summary=summary or None,
                        insights=insights,
                        future_use=future_use,
                        domain=item.get("domain"),
                        confidence=float(item.get("confidence", 0.5)),
                        status=item.get("status") or "tentative",
                        importance=float(item.get("importance", 0.5)),
                        scope=target["scope"],
                        thread_id=target["thread_id"],
                        project_id=target["project_id"],
                        use_when=item.get("use_when") if isinstance(item.get("use_when"), list) else [],
                        evidence_refs=item.get("evidence_refs") if isinstance(item.get("evidence_refs"), list) else [],
                        contradictions=item.get("contradictions") if isinstance(item.get("contradictions"),
                                                                                list) else [],
                        metadata_=metadata,
                    )

                    belief = self._attach_belief_embedding(belief)
                    stored = await self.belief_repo.upsert(belief)
                    stored_beliefs.append(stored.to_dict())

                    log.infox(
                        "BeliefRecord opgeslagen vanuit curiosity job",
                        job_id=job_id,
                        belief_id=getattr(stored, "id", None),
                        topic=item.get("topic") or topic,
                        scope=target["scope"],
                        stored_beliefs_count=len(stored_beliefs),
                    )

                for item in plan.get("memories") or []:
                    if not isinstance(item, dict):
                        log.debugx(
                            "Memory item uit belief plan overgeslagen: geen dict",
                            job_id=job_id,
                            item_type=type(item).__name__,
                        )
                        continue

                    content = (item.get("content") or "").strip()
                    if not content:
                        log.debugx(
                            "Memory item uit belief plan overgeslagen: content ontbreekt",
                            job_id=job_id,
                            item_keys=list(item.keys()),
                        )
                        continue

                    target = self._resolve_scoped_target(
                        requested_scope=item.get("scope"),
                        fallback_scope=target_for_job["scope"],
                        thread_id=effective_thread_id,
                        project_id=effective_project_id,
                    )

                    metadata = item.get("metadata_") if isinstance(item.get("metadata_"), dict) else {}
                    metadata.setdefault("source", "belief_system_assistant")
                    metadata.setdefault("origin_thread_id", effective_thread_id)
                    metadata.setdefault("origin_project_id", effective_project_id)
                    metadata.setdefault("origin_job_id", job_id)

                    log.debugx(
                        "MemoryRecord aanmaken vanuit belief plan",
                        job_id=job_id,
                        memory_type=item.get("type") or "semantic_memory",
                        scope=target["scope"],
                        content_length=len(content),
                        importance=item.get("importance", 0.5),
                        pinned=bool(item.get("pinned", False)),
                    )

                    memory = MemoryRecord(
                        type=item.get("type") or "semantic_memory",
                        content=content,
                        scope=target["scope"],
                        thread_id=target["thread_id"],
                        project_id=target["project_id"],
                        importance=float(item.get("importance", 0.5)),
                        pinned=bool(item.get("pinned", False)),
                        metadata_=metadata,
                    )

                    memory = self._attach_memory_embedding(memory)
                    stored = await self.memory_repo.upsert(memory)
                    stored_memories.append(stored.to_dict())

                    log.infox(
                        "MemoryRecord opgeslagen vanuit belief plan",
                        job_id=job_id,
                        memory_id=getattr(stored, "id", None),
                        scope=target["scope"],
                        stored_memories_count=len(stored_memories),
                    )

            else:
                log.warningx(
                    "Curiosity job belief assistant niet succesvol, geen beliefs/memories opgeslagen",
                    job_id=job_id,
                    topic=topic,
                    result_keys=list(belief_result.keys()) if isinstance(belief_result, dict) else None,
                )

            final = {
                "ok": True,
                "job_id": job_id,
                "topic": topic,
                "research": research,
                "observation_result": observation_result,
                "belief_result": belief_result,
                "stored_beliefs_count": len(stored_beliefs),
                "stored_memories_count": len(stored_memories),
                "stored_beliefs": stored_beliefs,
                "stored_memories": stored_memories,
            }

            log.infox(
                "Curiosity job markeren als completed",
                job_id=job_id,
                topic=topic,
                stored_beliefs_count=len(stored_beliefs),
                stored_memories_count=len(stored_memories),
            )

            await self.curiosity_repo.mark_completed(job_id, final)

            log.infox(
                "Curiosity job verwerking afgerond",
                job_id=job_id,
                topic=topic,
                ok=True,
                stored_beliefs_count=len(stored_beliefs),
                stored_memories_count=len(stored_memories),
            )

            return final

        except Exception as e:
            log.errorx(
                "Curiosity job verwerking mislukt",
                job_id=job_id,
                topic=topic,
                thread_id=thread_id,
                project_id=project_id,
                turn_id=turn_id,
                error=repr(e),
                exc_info=True,
            )

            if job_id:
                await self.curiosity_repo.mark_failed(job_id, repr(e))

            log.infox(
                "Curiosity job gemarkeerd als failed",
                job_id=job_id,
                error=repr(e),
            )

            return {
                "ok": False,
                "job_id": job_id,
                "error": repr(e),
            }

    async def create_router_memory(
        self,
        *,
        content: str,
        type: str = "router_memory",
        importance: float = 0.75,
        pinned: bool = False,
        metadata_: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        content = (content or "").strip()
        if not content:
            raise ValueError("Router memory content is required.")

        metadata = metadata_ or {}
        metadata.setdefault("source", "manual_router_memory")

        record = MemoryRecord(
            type=type or "router_memory",
            content=content,
            scope="global",
            thread_id="cognition_router",
            project_id=None,
            importance=float(importance),
            pinned=bool(pinned),
            metadata_=metadata,
        )

        record = self._attach_memory_embedding(record)
        stored = await self.memory_repo.upsert(record)
        return stored.to_dict()

    async def delete_memory(self, memory_id: str) -> Dict[str, Any]:
        from repository.system_cognition.memory_injection_repository import MemoryInjectionRepository

        deleted = await self.memory_repo.delete(memory_id)
        injection_deleted_count = await MemoryInjectionRepository().delete_for_memory(
            memory_kind="memory",
            memory_id=memory_id,
        )

        return {
            "ok": True,
            "deleted": deleted,
            "id": memory_id,
            "injection_deleted_count": injection_deleted_count,
        }

    async def delete_belief(self, belief_id: str) -> Dict[str, Any]:
        from repository.system_cognition.memory_injection_repository import MemoryInjectionRepository

        deleted = await self.belief_repo.delete(belief_id)
        injection_deleted_count = await MemoryInjectionRepository().delete_for_memory(
            memory_kind="belief",
            memory_id=belief_id,
        )

        return {
            "ok": True,
            "deleted": deleted,
            "id": belief_id,
            "injection_deleted_count": injection_deleted_count,
        }

    async def delete_curiosity_job(self, job_id: str) -> Dict[str, Any]:
        deleted = await self.curiosity_repo.delete(job_id)

        return {
            "ok": True,
            "deleted": deleted,
            "id": job_id,
        }