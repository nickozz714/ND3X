from __future__ import annotations

from component.config import settings
from component.logging import get_logger
from services.assistants.orchestration.pipeline_runner import AssistantPipelineRunner
from services.assistants.orchestration.runtime import RuntimeResolver
from services.assistants.orchestration.tool_execution import ToolExecutionRunner
from services.assistants.orchestration.tracing import OrchestratorTracer
from services.assistants.tool_guard import AssistantToolGuard
from services.audit_service import AuditService



log = get_logger(__name__)


class AssistantPipelineRunnerFactory:
    def __init__(
        self,
        *,
        openai_service,
        assistant_service,
        tool_execution_service,
        assistant_output_store_service
    ):
        log.infox(
            "AssistantPipelineRunnerFactory initialiseren",
            has_openai_service=openai_service is not None,
            has_assistant_service=assistant_service is not None,
            has_tool_execution_service=tool_execution_service is not None,
            has_assistant_output_store_service=assistant_output_store_service is not None,
            max_tool_steps=getattr(settings, "MAX_TOOL_STEPS", None),
        )
        self.openai_service = openai_service
        self.assistant_service = assistant_service
        self.tool_execution_service = tool_execution_service
        self.assistant_output_store_service = assistant_output_store_service
        log.infox(
            "AssistantPipelineRunnerFactory geïnitialiseerd"
        )

    def create(
        self,
        *,
        require_mutation_confirmation: bool,
        pending_store=None,
    ) -> AssistantPipelineRunner:
        log.infox(
            "AssistantPipelineRunner aanmaken gestart",
            require_mutation_confirmation=require_mutation_confirmation,
            has_pending_store=pending_store is not None,
            max_tool_steps=getattr(settings, "MAX_TOOL_STEPS", None),
        )

        runtime = RuntimeResolver(self.assistant_service)
        log.debugx(
            "RuntimeResolver aangemaakt voor AssistantPipelineRunner",
            has_assistant_service=self.assistant_service is not None,
        )

        tool_guard = AssistantToolGuard()
        log.debugx("AssistantToolGuard aangemaakt voor AssistantPipelineRunner")

        audit = AuditService()
        log.debugx("AuditService aangemaakt voor AssistantPipelineRunner")

        tracer = OrchestratorTracer(audit)
        log.debugx(
            "OrchestratorTracer aangemaakt voor AssistantPipelineRunner",
            has_audit=audit is not None,
        )

        tool_runner = ToolExecutionRunner(
            tool_execution_service=self.tool_execution_service,
            ingest_wait_timeout_s=600.0,
            ingest_poll_interval_s=0.75,
            max_tool_calls_per_turn=settings.MAX_TOOL_STEPS,
        )
        log.infox(
            "ToolExecutionRunner aangemaakt voor AssistantPipelineRunner",
            ingest_wait_timeout_s=600.0,
            ingest_poll_interval_s=0.75,
            max_tool_calls_per_turn=settings.MAX_TOOL_STEPS,
            has_tool_execution_service=self.tool_execution_service is not None
        )

        runner = AssistantPipelineRunner(
            openai_service=self.openai_service,
            runtime_resolver=runtime,
            tool_runner=tool_runner,
            tool_guard=tool_guard,
            assistant_output_store_service=self.assistant_output_store_service,
            trace_fn=tracer.trace,
            pending_store=pending_store,
            max_tool_calls_per_turn=settings.MAX_TOOL_STEPS,
            require_mutation_confirmation=require_mutation_confirmation,
        )

        log.infox(
            "AssistantPipelineRunner aanmaken afgerond",
            require_mutation_confirmation=require_mutation_confirmation,
            has_pending_store=pending_store is not None,
            max_tool_calls_per_turn=settings.MAX_TOOL_STEPS,
            runner_type=type(runner).__name__,
        )
        return runner