"""Scheduler tick that polls ACTIVE transfer records and moves any pending files.

Mirrors Transfer-Hub's DB-poll model (it polled every ~20s and kept routes in sync);
here a DynamicScheduler task runs `poll_active` on each tick. Blocking connector IO
runs off the event loop via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from component.logging import get_logger
from services.transfer import transfer_engine

log = get_logger(__name__)


class TransferPollService:
    def __init__(self, session_factory: Callable):
        self.session_factory = session_factory

    async def tick_once(self):
        return await asyncio.to_thread(self._tick)

    def _tick(self):
        db = self.session_factory()
        try:
            scheduled = transfer_engine.run_scheduled(db)   # cron-due routes
            watched = transfer_engine.poll_active(db)        # continuous-watcher routes
            return {"scheduled": scheduled, "watched": watched}
        finally:
            db.close()
