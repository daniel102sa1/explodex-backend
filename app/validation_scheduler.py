from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from app.config import settings
from app.database import SessionLocal
from app.services.paper_execution_v2 import run_paper_cycle_v2
from app.services.validation_mode import run_validation_cycle

logger = logging.getLogger("explodex.validation")


class ValidationScheduler:
    """ASGI wrapper that runs research validation and PAPER simulation only."""

    def __init__(self, app: Any, interval_seconds: int = 300, startup_delay_seconds: int = 90) -> None:
        self.app = app
        self.interval_seconds = max(60, interval_seconds)
        self.startup_delay_seconds = max(0, startup_delay_seconds)
        self.task: asyncio.Task[Any] | None = None

    async def _loop(self) -> None:
        await asyncio.sleep(self.startup_delay_seconds)
        while True:
            try:
                async with SessionLocal() as db:
                    await run_validation_cycle(db)
                    # The PAPER cycle now includes TREND/PRE-MOVE plus an independent
                    # all-eligible-universe RANGE MICRO scanner. RANGE MICRO is cached
                    # internally to ~5m, so this loop can stay conservative.
                    await run_paper_cycle_v2(db)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Validation/PAPER cycle failed")
            await asyncio.sleep(self.interval_seconds)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "lifespan" or not settings.scheduler_enabled:
            await self.app(scope, receive, send)
            return

        self.task = asyncio.create_task(self._loop(), name="explodex-validation-paper-loop")
        try:
            await self.app(scope, receive, send)
        finally:
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
                self.task = None
