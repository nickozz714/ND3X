from __future__ import annotations

from component.config import settings
from component.logging import get_logger

from services.audit_service import AuditService
from services.system_tools.system_tool_execution import SystemToolExecutionRunner
from services.assistants.orchestration.tracing import OrchestratorTracer

from repository.system_cognition.memory_repository import MemoryRepository
from repository.system_cognition.belief_repository import BeliefRepository
from repository.system_cognition.curiosity_job_repository import CuriosityJobRepository
from services.system_cognition.system_pipeline_runner import SystemPipelineRunner
from services.system_cognition.system_cognition_service import SystemCognitionService
from services.system_cognition.system_cognition_dispatcher import SystemCognitionDispatcher

log = get_logger(__name__)

def create_system_cognition_service(
    *,
    openai_service,
) -> tuple[SystemCognitionService, SystemCognitionDispatcher]:
    log.infox(
        "System cognition service factory gestart",
        has_openai_service=openai_service is not None,
        max_hops=getattr(settings, "SYSTEM_COGNITION_MAX_HOPS", 4),
        default_model=None,
        max_jobs_per_turn=getattr(settings, "SYSTEM_COGNITION_MAX_JOBS_PER_TURN", 2),
        queue_size=getattr(settings, "SYSTEM_COGNITION_QUEUE_SIZE", 100),
        worker_concurrency=getattr(settings, "SYSTEM_COGNITION_WORKERS", 1),
    )

    audit = AuditService()
    log.debugx("AuditService aangemaakt voor system cognition")

    tracer = OrchestratorTracer(audit)
    log.debugx(
        "OrchestratorTracer aangemaakt voor system cognition",
        has_audit_service=audit is not None,
    )

    memory_repo = MemoryRepository()
    log.debugx("MemoryRepository aangemaakt voor system cognition")

    belief_repo = BeliefRepository()
    log.debugx("BeliefRepository aangemaakt voor system cognition")

    curiosity_repo = CuriosityJobRepository()
    log.debugx("CuriosityJobRepository aangemaakt voor system cognition")

    tool_runner = SystemToolExecutionRunner()
    log.debugx("SystemToolExecutionRunner aangemaakt voor system cognition")

    log.infox(
        "SystemPipelineRunner aanmaken gestart",
        max_hops=getattr(settings, "SYSTEM_COGNITION_MAX_HOPS", 4),
        default_model=None,
    )
    system_runner = SystemPipelineRunner(
        openai_service=openai_service,
        tool_runner=tool_runner,
        trace_fn=tracer.trace,
        max_hops=getattr(settings, "SYSTEM_COGNITION_MAX_HOPS", 4),
        default_model=None,
    )
    log.infox(
        "SystemPipelineRunner aangemaakt",
        max_hops=getattr(settings, "SYSTEM_COGNITION_MAX_HOPS", 4),
        default_model=None,
    )

    log.infox(
        "SystemCognitionService aanmaken gestart",
        default_model=None,
        max_jobs_per_turn=getattr(settings, "SYSTEM_COGNITION_MAX_JOBS_PER_TURN", 2),
    )
    cognition_service = SystemCognitionService(
        openai_service=openai_service,
        memory_repo=memory_repo,
        belief_repo=belief_repo,
        curiosity_repo=curiosity_repo,
        system_runner=system_runner,
        audit_service=audit,
        default_model=None,
        max_jobs_per_turn=getattr(settings, "SYSTEM_COGNITION_MAX_JOBS_PER_TURN", 2),
    )
    log.infox(
        "SystemCognitionService aangemaakt",
        default_model=None,
        max_jobs_per_turn=getattr(settings, "SYSTEM_COGNITION_MAX_JOBS_PER_TURN", 2),
    )

    log.infox(
        "SystemCognitionDispatcher aanmaken gestart",
        max_queue_size=getattr(settings, "SYSTEM_COGNITION_QUEUE_SIZE", 100),
        worker_concurrency=getattr(settings, "SYSTEM_COGNITION_WORKERS", 1),
    )
    dispatcher = SystemCognitionDispatcher(
        cognition_service=cognition_service,
        max_queue_size=getattr(settings, "SYSTEM_COGNITION_QUEUE_SIZE", 100),
        worker_concurrency=getattr(settings, "SYSTEM_COGNITION_WORKERS", 1),
    )
    log.infox(
        "SystemCognitionDispatcher aangemaakt",
        max_queue_size=getattr(settings, "SYSTEM_COGNITION_QUEUE_SIZE", 100),
        worker_concurrency=getattr(settings, "SYSTEM_COGNITION_WORKERS", 1),
    )

    log.infox(
        "System cognition service factory afgerond",
        has_cognition_service=cognition_service is not None,
        has_dispatcher=dispatcher is not None,
    )
    return cognition_service, dispatcher