from __future__ import annotations

from component.logging import get_logger

log = get_logger(__name__)


class SystemCuriosityTickService:
    def __init__(
        self,
        *,
        cognition_service,
        batch_size: int = 1,
    ):
        self.cognition_service = cognition_service
        self.batch_size = batch_size

    async def tick_once(self) -> None:
        log.infox(
            "System curiosity tick gestart",
            batch_size=self.batch_size,
        )

        result = await self.cognition_service.process_queued_curiosity_jobs(
            limit=self.batch_size,
            thread_id="cognition_curiosity_scheduler",
            turn_id=0,
        )

        log.infox(
            "System curiosity tick afgerond",
            processed_count=result.get("processed_count") if isinstance(result, dict) else None,
        )