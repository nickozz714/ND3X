from __future__ import annotations

from sqlalchemy.orm import Session

from component.logging import get_logger
from repository.prompt_variable_repository import PromptVariableRepository
from repository.workflow_repository import WorkflowRepository
from repository.workflow_run_repository import WorkflowRunRepository
from services.assistant_output_store_service import AssistantOutputStoreService
from services.assistants.assistant_service import AssistantService
from services.assistants.orchestration.pipeline_runner_factory import AssistantPipelineRunnerFactory
from services.mcp.tool_execution_service import ToolExecutionService
from services.openai_service import OpenAIResponsesService
from services.workflows.assistant_operation_runner import AssistantOperationRunner
from services.workflows.claude_code_operation_runner import ClaudeCodeOperationRunner
from services.workflows.prompt_varable_resolver import PromptVariableResolver
from services.workflows.prompt_variable_executor import PromptVariableExecutor
from services.workflows.workflow_executor import WorkflowExecutor


log = get_logger(__name__)


class WorkflowExecutionProvider:
    """Builds a fully-wired WorkflowExecutor for background workflow execution.

    This keeps worker/scheduler files clean and avoids coupling workflows to the
    chat AssistantOrchestrator instance.
    """

    def __init__(self, *, db: Session):
        log.infox(
            "WorkflowExecutionProvider initialiseren",
            has_db=db is not None,
            db_type=type(db).__name__,
        )
        self.db = db
        log.infox("WorkflowExecutionProvider geïnitialiseerd")

    def build_executor(self) -> WorkflowExecutor:
        log.infox("WorkflowExecutor bouwen gestart")

        openai = OpenAIResponsesService(
            # OpenAI key resolved lazily from the registry's OpenAI provider
            model=None,  # chat model comes from the routing slots (registry), not config
            embedding_model=None,  # embeddings model comes from the embeddings slot
        )
        # Route through the provider registry so workflow assistants honor the
        # capability/model selections too (empty registry = OpenAI as before).
        from services.providers.provider_factory import build_llm_router
        llm = build_llm_router(openai, self.db)
        log.infox("LLMRouter aangemaakt voor WorkflowExecutor")

        assistant_service = AssistantService(self.db)
        log.debugx(
            "AssistantService aangemaakt voor WorkflowExecutor",
            service_type=type(assistant_service).__name__,
        )

        tool_execution_service = ToolExecutionService(self.db)
        log.debugx(
            "ToolExecutionService aangemaakt voor WorkflowExecutor",
            service_type=type(tool_execution_service).__name__,
        )

        assistant_output_store_service = AssistantOutputStoreService(self.db)
        log.debugx(
            "AssistantOutputStoreService aangemaakt voor WorkflowExecutor",
            service_type=type(assistant_output_store_service).__name__,
        )

        log.infox(
            "AssistantPipelineRunner bouwen voor WorkflowExecutor gestart",
            require_mutation_confirmation=False,
            has_pending_store=False,
        )
        pipeline_runner = AssistantPipelineRunnerFactory(
            openai_service=llm,
            assistant_service=assistant_service,
            tool_execution_service=tool_execution_service,
            assistant_output_store_service=assistant_output_store_service,
        ).create(
            require_mutation_confirmation=False,
            pending_store=None,
        )
        log.infox(
            "AssistantPipelineRunner gebouwd voor WorkflowExecutor",
            runner_type=type(pipeline_runner).__name__,
        )

        assistant_runner = AssistantOperationRunner(
            assistant_service=assistant_service,
            pipeline_runner=pipeline_runner,
        )
        log.infox(
            "AssistantOperationRunner aangemaakt voor WorkflowExecutor",
            runner_type=type(assistant_runner).__name__,
        )

        prompt_variable_resolver = PromptVariableResolver(
            repository=PromptVariableRepository(self.db),
            executor=PromptVariableExecutor(),
        )
        log.infox(
            "PromptVariableResolver aangemaakt voor WorkflowExecutor",
            resolver_type=type(prompt_variable_resolver).__name__,
        )

        # Alternative per-operation engine: run an activity as an autonomous
        # Claude Code CLI task. Only used when an operation opts in via
        # config.execution.engine == "claude_code".
        claude_code_runner = ClaudeCodeOperationRunner(self.db)

        executor = WorkflowExecutor(
            workflow_repository=WorkflowRepository(self.db),
            run_repository=WorkflowRunRepository(self.db),
            assistant_runner=assistant_runner,
            prompt_variable_resolver=prompt_variable_resolver,
            claude_code_runner=claude_code_runner,
        )

        log.infox(
            "WorkflowExecutor bouwen afgerond",
            executor_type=type(executor).__name__,
            has_assistant_runner=assistant_runner is not None,
            has_prompt_variable_resolver=prompt_variable_resolver is not None,
        )
        return executor