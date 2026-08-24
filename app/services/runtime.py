from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database import SessionLocal
from app.services.paper_time_management import manage_open_paper_trades_with_time
from app.services.paper_trading import sync_ready_signals
from app.services.scanner import run_scanner

logger = logging.getLogger("explodex.runtime")


class RuntimeState:
    def __init__(self) -> None:
        self.started_at: datetime | None = None
        self.last_scanner_at: datetime | None = None
        self.last_scanner_ok: bool | None = None
        self.last_scanner_error: str | None = None
        self.last_scanner_result: dict[str, Any] | None = None

        self.last_paper_manage_at: datetime | None = None
        self.last_paper_manage_ok: bool | None = None
        self.last_paper_manage_error: str | None = None
        self.last_paper_manage_result: dict[str, Any] | None = None

        self.last_paper_sync_at: datetime | None = None
        self.last_paper_sync_ok: bool | None = None
        self.last_paper_sync_error: str | None = None
        self.last_paper_sync_result: dict[str, Any] | None = None

        self.scanner_running = False
        self.paper_manage_running = False
        self.paper_sync_running = False

    def as_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "scheduler_enabled": settings.scheduler_enabled,
            "started_at": iso(self.started_at),
            "scanner": {
                "running": self.scanner_running,
                "interval_seconds": settings.scanner_interval_seconds,
                "deep_limit": settings.scanner_deep_limit,
                "last_run_at": iso(self.last_scanner_at),
                "last_ok": self.last_scanner_ok,
                "last_error": self.last_scanner_error,
                "last_result": self.last_scanner_result,
            },
            "paper_manager": {
                "running": self.paper_manage_running,
                "interval_seconds": settings.paper_manage_interval_seconds,
                "time_stop_enabled": True,
                "last_run_at": iso(self.last_paper_manage_at),
                "last_ok": self.last_paper_manage_ok,
                "last_error": self.last_paper_manage_error,
                "last_result": self.last_paper_manage_result,
            },
            "paper_sync": {
                "running": self.paper_sync_running,
                "interval_seconds": settings.paper_sync_interval_seconds,
                "last_run_at": iso(self.last_paper_sync_at),
                "last_ok": self.last_paper_sync_ok,
                "last_error": self.last_paper_sync_error,
                "last_result": self.last_paper_sync_result,
            },
        }


runtime_state = RuntimeState()


async def _run_scanner_once() -> None:
    if runtime_state.scanner_running:
        return
    runtime_state.scanner_running = True
    try:
        async with SessionLocal() as db:
            result = await run_scanner(db, deep_limit=settings.scanner_deep_limit)
        runtime_state.last_scanner_result = {
            "run_id": result.get("run_id"),
            "symbols_scanned": result.get("symbols_scanned"),
            "candidates_found": result.get("candidates_found"),
            "btc_context": result.get("btc_context"),
            "errors": result.get("errors", [])[:3],
        }
        runtime_state.last_scanner_ok = True
        runtime_state.last_scanner_error = None
    except Exception as exc:
        logger.exception("Automatic scanner cycle failed")
        runtime_state.last_scanner_ok = False
        runtime_state.last_scanner_error = str(exc)[:1000]
    finally:
        runtime_state.last_scanner_at = datetime.now(timezone.utc)
        runtime_state.scanner_running = False


async def _run_paper_manage_once() -> None:
    if runtime_state.paper_manage_running:
        return
    runtime_state.paper_manage_running = True
    try:
        async with SessionLocal() as db:
            result = await manage_open_paper_trades_with_time(db)
        runtime_state.last_paper_manage_result = {
            "managed": result.get("managed", 0),
            "actions": result.get("actions", [])[:10],
            "time_management": result.get("time_management", [])[:10],
        }
        runtime_state.last_paper_manage_ok = True
        runtime_state.last_paper_manage_error = None
    except Exception as exc:
        logger.exception("Automatic paper manager cycle failed")
        runtime_state.last_paper_manage_ok = False
        runtime_state.last_paper_manage_error = str(exc)[:1000]
    finally:
        runtime_state.last_paper_manage_at = datetime.now(timezone.utc)
        runtime_state.paper_manage_running = False


async def _run_paper_sync_once() -> None:
    if runtime_state.paper_sync_running:
        return
    runtime_state.paper_sync_running = True
    try:
        async with SessionLocal() as db:
            result = await sync_ready_signals(db)
        runtime_state.last_paper_sync_result = {
            "opened": result.get("opened", 0),
            "reason": result.get("reason"),
            "equity_usdt": result.get("equity_usdt"),
            "daily_pnl_usdt": result.get("daily_pnl_usdt"),
        }
        runtime_state.last_paper_sync_ok = True
        runtime_state.last_paper_sync_error = None
    except Exception as exc:
        logger.exception("Automatic paper sync cycle failed")
        runtime_state.last_paper_sync_ok = False
        runtime_state.last_paper_sync_error = str(exc)[:1000]
    finally:
        runtime_state.last_paper_sync_at = datetime.now(timezone.utc)
        runtime_state.paper_sync_running = False


async def _scanner_loop() -> None:
    await asyncio.sleep(8)
    while True:
        await _run_scanner_once()
        await asyncio.sleep(settings.scanner_interval_seconds)


async def _paper_manage_loop() -> None:
    await asyncio.sleep(12)
    while True:
        await _run_paper_manage_once()
        await asyncio.sleep(settings.paper_manage_interval_seconds)


async def _paper_sync_loop() -> None:
    await asyncio.sleep(25)
    while True:
        await _run_paper_sync_once()
        await asyncio.sleep(settings.paper_sync_interval_seconds)


async def start_runtime() -> list[asyncio.Task[Any]]:
    runtime_state.started_at = datetime.now(timezone.utc)
    if not settings.scheduler_enabled:
        return []

    return [
        asyncio.create_task(_scanner_loop(), name="explodex-scanner-loop"),
        asyncio.create_task(_paper_manage_loop(), name="explodex-paper-manage-loop"),
        asyncio.create_task(_paper_sync_loop(), name="explodex-paper-sync-loop"),
    ]


async def stop_runtime(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
