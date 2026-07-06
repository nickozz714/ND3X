from __future__ import annotations

import asyncio
from typing import Optional

from component.logging import get_logger


log = get_logger(__name__)


class SystemCognitionDispatcher:
    def __init__(
        self,
        *,
        cognition_service,
        max_queue_size: int = 100,
        worker_concurrency: int = 1,
    ):
        log.infox(
            "SystemCognitionDispatcher initialiseren",
            has_cognition_service=cognition_service is not None,
            max_queue_size=max_queue_size,
            worker_concurrency=worker_concurrency,
        )
        self.cognition_service = cognition_service
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self.worker_concurrency = worker_concurrency
        self._workers: list[asyncio.Task] = []
        self._started = False
        log.debugx(
            "SystemCognitionDispatcher geïnitialiseerd",
            queue_maxsize=max_queue_size,
            worker_concurrency=self.worker_concurrency,
            started=self._started,
        )

    def start(self) -> None:
        log.infox(
            "SystemCognitionDispatcher start aangeroepen",
            already_started=self._started,
            worker_concurrency=self.worker_concurrency,
            existing_worker_count=len(self._workers),
        )
        if self._started:
            log.debugx(
                "SystemCognitionDispatcher start overgeslagen: al gestart",
                worker_count=len(self._workers),
            )
            return

        self._started = True
        log.debugx("SystemCognitionDispatcher status op gestart gezet")

        for _ in range(self.worker_concurrency):
            log.debugx(
                "System cognition worker task aanmaken",
                current_worker_count=len(self._workers),
                worker_concurrency=self.worker_concurrency,
            )
            self._workers.append(asyncio.create_task(self._worker_loop()))

        log.infox(
            "SystemCognitionDispatcher gestart",
            worker_count=len(self._workers),
            queue_size=self.queue.qsize(),
        )

    def enqueue(
            self,
            *,
            question: str,
            answer: str,
            thread_id: Optional[str],
            project_id: Optional[str] = None,
            turn_id: int,
            trace: Optional[list[dict]] = None,
            progress_cb=None,
    ) -> None:
        log.infox(
            "System cognition item enqueue gestart",
            thread_id=thread_id,
            project_id=project_id,
            turn_id=turn_id,
            question_length=len(question or ""),
            answer_length=len(answer or ""),
            trace_count=len(trace or []),
            has_progress_cb=progress_cb is not None,
            dispatcher_started=self._started,
            queue_size=self.queue.qsize(),
        )

        if not self._started:
            log.debugx(
                "SystemCognitionDispatcher nog niet gestart, start wordt aangeroepen",
                thread_id=thread_id,
                project_id=project_id,
                turn_id=turn_id,
            )
            self.start()

        item = {
            "question": question,
            "answer": answer,
            "thread_id": thread_id,
            "project_id": project_id,
            "turn_id": turn_id,
            "trace": trace or [],
            "progress_cb": progress_cb,
        }

        try:
            self.queue.put_nowait(item)
            log.infox(
                "System cognition item succesvol toegevoegd aan queue",
                thread_id=thread_id,
                turn_id=turn_id,
                queue_size=self.queue.qsize(),
            )
        except asyncio.QueueFull:
            log.warningx(
                "System cognition item niet toegevoegd: queue is vol",
                thread_id=thread_id,
                turn_id=turn_id,
                queue_size=self.queue.qsize(),
            )

    async def _worker_loop(self) -> None:
        log.infox("System cognition worker loop gestart")
        while True:
            log.debugx(
                "System cognition worker wacht op item",
                queue_size=self.queue.qsize(),
            )
            item = await self.queue.get()
            log.infox(
                "System cognition worker item ontvangen",
                thread_id=item.get("thread_id"),
                turn_id=item.get("turn_id"),
                question_length=len(item.get("question") or ""),
                answer_length=len(item.get("answer") or ""),
                trace_count=len(item.get("trace") or []),
                queue_size=self.queue.qsize(),
            )

            try:
                log.debugx(
                    "System cognition post_turn uitvoeren gestart",
                    thread_id=item["thread_id"],
                    turn_id=item["turn_id"],
                )
                await self.cognition_service.post_turn(
                    question=item["question"],
                    answer=item["answer"],
                    thread_id=item["thread_id"],
                    project_id=item.get("project_id"),
                    turn_id=item["turn_id"],
                    trace=item["trace"],
                    progress_cb=item["progress_cb"],
                )
                log.infox(
                    "System cognition post_turn succesvol afgerond",
                    thread_id=item.get("thread_id"),
                    turn_id=item.get("turn_id"),
                )
            except Exception as e:
                log.errorx(
                    "System cognition worker item verwerken mislukt",
                    thread_id=item.get("thread_id"),
                    turn_id=item.get("turn_id"),
                    error=repr(e),
                    error_type=type(e).__name__,
                )
            finally:
                self.queue.task_done()
                log.debugx(
                    "System cognition queue item afgerond",
                    thread_id=item.get("thread_id"),
                    turn_id=item.get("turn_id"),
                    queue_size=self.queue.qsize(),
                )