from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Callable, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from repository.workflow_run_repository import WorkflowRunRepository
from services.workflows.workflow_execution_provider import WorkflowExecutionProvider

logger = logging.getLogger(__name__)
log = get_logger(__name__)

SessionFactory = Callable[[], Session]


class WorkflowWorker:
    """Executes queued workflow runs.

    Scheduling and execution are deliberately separated:
    - WorkflowScheduler enqueues cron-due workflow runs.
    - WorkflowWorker polls queued runs and executes them.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        interval_seconds: int = 10,
        batch_size: int = 3,
    ):
        log.infox(
            "WorkflowWorker initialiseren",
            has_session_factory=session_factory is not None,
            interval_seconds=interval_seconds,
            batch_size=batch_size,
        )
        self.session_factory = session_factory
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        log.infox(
            "WorkflowWorker geïnitialiseerd",
            interval_seconds=self.interval_seconds,
            batch_size=self.batch_size,
            has_task=self._task is not None,
            has_stop_event=self._stop_event is not None,
        )

    def start(self) -> None:
        log.infox(
            "WorkflowWorker start aangeroepen",
            has_existing_task=self._task is not None,
            existing_task_done=self._task.done() if self._task else None,
            interval_seconds=self.interval_seconds,
            batch_size=self.batch_size,
        )
        if self._task and not self._task.done():
            log.infox(
                "WorkflowWorker start overgeslagen: task draait al",
                task_name=self._task.get_name() if hasattr(self._task, "get_name") else None,
            )
            return

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="workflow-worker")
        log.infox(
            "WorkflowWorker gestart",
            task_name=self._task.get_name() if hasattr(self._task, "get_name") else None,
            has_stop_event=self._stop_event is not None,
            interval_seconds=self.interval_seconds,
            batch_size=self.batch_size,
        )
        logger.info("Workflow worker started")

    async def stop(self) -> None:
        log.infox(
            "WorkflowWorker stop aangeroepen",
            has_task=self._task is not None,
            task_done=self._task.done() if self._task else None,
            has_stop_event=self._stop_event is not None,
        )
        if not self._task:
            log.infox("WorkflowWorker stop overgeslagen: geen task")
            return

        if self._stop_event:
            log.debugx("WorkflowWorker stop_event zetten")
            self._stop_event.set()

        log.infox(
            "WorkflowWorker task annuleren",
            task_name=self._task.get_name() if hasattr(self._task, "get_name") else None,
        )
        self._task.cancel()

        with suppress(asyncio.CancelledError):
            await self._task

        self._task = None
        self._stop_event = None
        log.infox("WorkflowWorker gestopt")
        logger.info("Workflow worker stopped")

    async def _run_loop(self) -> None:
        log.infox(
            "WorkflowWorker run loop gestart",
            interval_seconds=self.interval_seconds,
            batch_size=self.batch_size,
            has_stop_event=self._stop_event is not None,
        )
        assert self._stop_event is not None

        while not self._stop_event.is_set():
            try:
                log.debugx("WorkflowWorker tick loop iteratie gestart")
                await self.tick_once()
                log.debugx("WorkflowWorker tick loop iteratie afgerond")
            except asyncio.CancelledError:
                log.infox("WorkflowWorker run loop geannuleerd")
                raise
            except Exception:
                log.warningx("WorkflowWorker tick mislukt")
                logger.exception("Workflow worker tick failed")

            try:
                log.debugx(
                    "WorkflowWorker wacht op volgende tick",
                    interval_seconds=self.interval_seconds,
                )
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
                log.debugx("WorkflowWorker stop_event ontvangen tijdens wachten")
            except asyncio.TimeoutError:
                log.debugx("WorkflowWorker interval verstreken, volgende tick starten")

        log.infox("WorkflowWorker run loop beëindigd door stop_event")

    async def tick_once(self) -> None:
        log.infox(
            "WorkflowWorker tick_once gestart",
            batch_size=self.batch_size,
        )
        db = self.session_factory()
        log.debugx(
            "WorkflowWorker database sessie aangemaakt",
            has_db=db is not None,
            db_type=type(db).__name__,
        )
        try:
            run_repository = WorkflowRunRepository(db)
            log.debugx(
                "WorkflowRunRepository aangemaakt voor WorkflowWorker",
                repository_type=type(run_repository).__name__,
            )

            queued_runs = run_repository.list_queued_runs(limit=self.batch_size)

            log.infox(
                "WorkflowWorker queued runs opgehaald",
                batch_size=self.batch_size,
                queued_run_count=len(queued_runs or []),
                queued_run_ids=[getattr(run, "id", None) for run in (queued_runs or [])],
            )

            if not queued_runs:
                log.infox("WorkflowWorker tick_once afgerond: geen queued runs")
                return

            log.infox(
                "WorkflowExecutor bouwen voor queued workflow runs",
                queued_run_count=len(queued_runs or []),
            )
            executor = WorkflowExecutionProvider(db=db).build_executor()
            log.infox(
                "WorkflowExecutor gebouwd voor WorkflowWorker",
                executor_type=type(executor).__name__,
            )

            for run in queued_runs:
                try:
                    log.infox(
                        "Queued workflow run uitvoeren gestart",
                        workflow_run_id=getattr(run, "id", None),
                        workflow_id=getattr(run, "workflow_id", None),
                        status=getattr(run, "status", None),
                        trigger_type=getattr(run, "trigger_type", None),
                        parent_run_id=getattr(run, "parent_run_id", None),
                    )
                    await executor.execute_run(run.id)
                    log.infox(
                        "Queued workflow run uitvoeren afgerond",
                        workflow_run_id=getattr(run, "id", None),
                        workflow_id=getattr(run, "workflow_id", None),
                    )
                except Exception:
                    run_id = getattr(run, "id", None)
                    log.warningx(
                        "Queued workflow run uitvoeren mislukt",
                        workflow_run_id=run_id,
                        workflow_id=getattr(run, "workflow_id", None),
                        trigger_type=getattr(run, "trigger_type", None),
                    )
                    db.rollback()
                    log.debugx(
                        "Database rollback uitgevoerd na workflow run fout",
                        workflow_run_id=run_id,
                    )
                    logger.exception("Workflow run failed: %s", run_id)

            log.infox(
                "WorkflowWorker tick_once afgerond",
                processed_run_count=len(queued_runs or []),
                processed_run_ids=[getattr(run, "id", None) for run in (queued_runs or [])],
            )
        finally:
            log.debugx("WorkflowWorker database sessie sluiten")
            db.close()