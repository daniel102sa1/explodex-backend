from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database import SessionLocal
from app.services.edge_engine import capture_recent_signals, label_due_observations
from app.services.explosion_intelligence import (
    enrich_verdict_features,
    label_explosion_outcomes,
    load_timing_model,
)
from app.services.multi_horizon_outcomes import update_multi_horizon_outcomes
from app.services.outcome_shadow_model import build_tp1_stop_shadow_report
from app.services.paper_fast_cycle import run_fast_paper_cycle
from app.services.paper_trading import manage_open_paper_trades
from app.services.scanner_guarded import run_scanner
from app.services.sequential_microstructure import flush_pending_snapshots, hydrate_recent_histories, prune_persistent_history
from app.services.trade_time_manager import manage_trade_time_stops
from app.services.verdict_memory import capture_enter_verdicts, resolve_verdict_outcomes, verdict_memory_stats
from app.services.walk_forward import build_walk_forward_report

logger = logging.getLogger("explodex.runtime")

SCANNER_LOOP_SECONDS = min(max(30, int(settings.scanner_interval_seconds)), 60)
PAPER_HEART_LOOP_SECONDS = min(max(15, int(settings.paper_sync_interval_seconds)), 30)


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

        self.last_edge_at: datetime | None = None
        self.last_edge_ok: bool | None = None
        self.last_edge_error: str | None = None
        self.last_edge_result: dict[str, Any] | None = None

        self.last_verdict_memory_at: datetime | None = None
        self.last_verdict_memory_ok: bool | None = None
        self.last_verdict_memory_error: str | None = None
        self.last_verdict_memory_result: dict[str, Any] | None = None

        self.last_microstructure_memory_at: datetime | None = None
        self.last_microstructure_memory_ok: bool | None = None
        self.last_microstructure_memory_error: str | None = None
        self.last_microstructure_memory_result: dict[str, Any] | None = None

        self.scanner_running = False
        self.paper_manage_running = False
        self.paper_sync_running = False
        self.edge_running = False
        self.verdict_memory_running = False
        self.microstructure_memory_running = False

    def as_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "scheduler_enabled": settings.scheduler_enabled,
            "started_at": iso(self.started_at),
            "scanner": {
                "running": self.scanner_running,
                "interval_seconds": SCANNER_LOOP_SECONDS,
                "configured_interval_seconds": settings.scanner_interval_seconds,
                "deep_limit": settings.scanner_deep_limit,
                "last_run_at": iso(self.last_scanner_at),
                "last_ok": self.last_scanner_ok,
                "last_error": self.last_scanner_error,
                "last_result": self.last_scanner_result,
            },
            "paper_manager": {
                "running": self.paper_manage_running,
                "interval_seconds": settings.paper_manage_interval_seconds,
                "last_run_at": iso(self.last_paper_manage_at),
                "last_ok": self.last_paper_manage_ok,
                "last_error": self.last_paper_manage_error,
                "last_result": self.last_paper_manage_result,
            },
            "paper_sync": {
                "running": self.paper_sync_running,
                "engine": "paper_fast_cycle_v1",
                "portfolio": "paper_positions_visible_in_/paper",
                "interval_seconds": PAPER_HEART_LOOP_SECONDS,
                "configured_interval_seconds": settings.paper_sync_interval_seconds,
                "last_run_at": iso(self.last_paper_sync_at),
                "last_ok": self.last_paper_sync_ok,
                "last_error": self.last_paper_sync_error,
                "last_result": self.last_paper_sync_result,
            },
            "edge_engine": {
                "running": self.edge_running,
                "interval_seconds": 180,
                "last_run_at": iso(self.last_edge_at),
                "last_ok": self.last_edge_ok,
                "last_error": self.last_edge_error,
                "last_result": self.last_edge_result,
            },
            "verdict_memory": {
                "running": self.verdict_memory_running,
                "interval_seconds": 120,
                "horizons": ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "6h", "24h"],
                "explosion_labels": ["EXPLOSION_LONG", "EXPLOSION_SHORT", "DELAYED_EXPLOSION", "FAKE_BREAKOUT", "SWEEP_AND_REVERSE_TO_THESIS", "DIRECTION_WRONG", "NO_MOVE"],
                "last_run_at": iso(self.last_verdict_memory_at),
                "last_ok": self.last_verdict_memory_ok,
                "last_error": self.last_verdict_memory_error,
                "last_result": self.last_verdict_memory_result,
            },
            "microstructure_memory": {
                "running": self.microstructure_memory_running,
                "interval_seconds": 30,
                "last_run_at": iso(self.last_microstructure_memory_at),
                "last_ok": self.last_microstructure_memory_ok,
                "last_error": self.last_microstructure_memory_error,
                "last_result": self.last_microstructure_memory_result,
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
            price_result = await manage_open_paper_trades(db)
            time_result = await manage_trade_time_stops(db)
        runtime_state.last_paper_manage_result = {
            "managed": price_result.get("managed", 0),
            "actions": (price_result.get("actions", []) + time_result.get("actions", []))[:15],
            "time_stop_actions": time_result.get("actions", [])[:10],
        }
        runtime_state.last_paper_manage_ok = True
        runtime_state.last_paper_manage_error = None
    except Exception as exc:
        logger.exception("Automatic legacy paper manager cycle failed")
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
            result = await run_fast_paper_cycle(db)
        trend = result.get("trend") or {}
        diagnostics = result.get("heart_diagnostics") or {}
        runtime_state.last_paper_sync_result = {
            "version": result.get("version"),
            "opened": result.get("opened", 0),
            "closed": result.get("closed", 0),
            "reason": result.get("reason"),
            "equity": result.get("equity"),
            "open_positions": result.get("open_positions"),
            "signals_checked": trend.get("signals_checked"),
            "heart_enter_signals": trend.get("heart_enter_signals"),
            "blockers": trend.get("blockers"),
            "heart_actions": diagnostics.get("actions"),
            "missing_checks": diagnostics.get("missing_checks"),
            "enter_symbols": diagnostics.get("enter_symbols"),
            "aggressive_learning": result.get("aggressive_learning"),
        }
        runtime_state.last_paper_sync_ok = True
        runtime_state.last_paper_sync_error = None
    except Exception as exc:
        logger.exception("Automatic visible Heart PAPER cycle failed")
        runtime_state.last_paper_sync_ok = False
        runtime_state.last_paper_sync_error = str(exc)[:1000]
    finally:
        runtime_state.last_paper_sync_at = datetime.now(timezone.utc)
        runtime_state.paper_sync_running = False


async def _run_edge_once() -> None:
    if runtime_state.edge_running:
        return
    runtime_state.edge_running = True
    try:
        async with SessionLocal() as db:
            captured = await capture_recent_signals(db, limit=150)
            labeled = await label_due_observations(db, limit=40)
        runtime_state.last_edge_result = {"capture": captured, "label": labeled}
        runtime_state.last_edge_ok = True
        runtime_state.last_edge_error = None
    except Exception as exc:
        logger.exception("Edge Engine cycle failed")
        runtime_state.last_edge_ok = False
        runtime_state.last_edge_error = str(exc)[:1000]
    finally:
        runtime_state.last_edge_at = datetime.now(timezone.utc)
        runtime_state.edge_running = False


async def _run_verdict_memory_once() -> None:
    if runtime_state.verdict_memory_running:
        return
    runtime_state.verdict_memory_running = True
    try:
        async with SessionLocal() as db:
            captured = await capture_enter_verdicts(db, limit=200)
            features = await enrich_verdict_features(db, limit=250)
            horizons = await update_multi_horizon_outcomes(db, limit=120)
            explosion_labels = await label_explosion_outcomes(db, limit=250)
            timing_model = await load_timing_model(db, force=True)
            resolved = await resolve_verdict_outcomes(db, limit=60)
            stats = await verdict_memory_stats(db)
            shadow_model = await build_tp1_stop_shadow_report(db)
            walk_forward = await build_walk_forward_report(db)
        runtime_state.last_verdict_memory_result = {
            "capture": captured,
            "feature_enrichment": features,
            "multi_horizon": horizons,
            "explosion_labels": explosion_labels,
            "timing_model": timing_model,
            "resolve": resolved,
            "stats": stats,
            "tp1_stop_shadow_model": shadow_model,
            "walk_forward": walk_forward,
        }
        runtime_state.last_verdict_memory_ok = True
        runtime_state.last_verdict_memory_error = None
    except Exception as exc:
        logger.exception("Verdict Memory cycle failed")
        runtime_state.last_verdict_memory_ok = False
        runtime_state.last_verdict_memory_error = str(exc)[:1000]
    finally:
        runtime_state.last_verdict_memory_at = datetime.now(timezone.utc)
        runtime_state.verdict_memory_running = False


async def _run_microstructure_memory_once(*, hydrate: bool = False, prune: bool = False) -> None:
    if runtime_state.microstructure_memory_running:
        return
    runtime_state.microstructure_memory_running = True
    try:
        async with SessionLocal() as db:
            restored = await hydrate_recent_histories(db) if hydrate else None
            flushed = await flush_pending_snapshots(db, limit=500)
            pruned = await prune_persistent_history(db, retention_hours=24) if prune else 0
        runtime_state.last_microstructure_memory_result = {
            "restored": restored,
            "flushed": flushed,
            "pruned": pruned,
            "retention_hours": 24,
        }
        runtime_state.last_microstructure_memory_ok = True
        runtime_state.last_microstructure_memory_error = None
    except Exception as exc:
        logger.exception("Microstructure Memory cycle failed")
        runtime_state.last_microstructure_memory_ok = False
        runtime_state.last_microstructure_memory_error = str(exc)[:1000]
    finally:
        runtime_state.last_microstructure_memory_at = datetime.now(timezone.utc)
        runtime_state.microstructure_memory_running = False


async def _scanner_loop() -> None:
    await asyncio.sleep(8)
    while True:
        await _run_scanner_once()
        await asyncio.sleep(SCANNER_LOOP_SECONDS)


async def _paper_manage_loop() -> None:
    await asyncio.sleep(12)
    while True:
        await _run_paper_manage_once()
        await asyncio.sleep(settings.paper_manage_interval_seconds)


async def _paper_sync_loop() -> None:
    await asyncio.sleep(15)
    while True:
        await _run_paper_sync_once()
        await asyncio.sleep(PAPER_HEART_LOOP_SECONDS)


async def _edge_loop() -> None:
    await asyncio.sleep(45)
    while True:
        await _run_edge_once()
        await asyncio.sleep(180)


async def _verdict_memory_loop() -> None:
    await asyncio.sleep(70)
    while True:
        await _run_verdict_memory_once()
        await asyncio.sleep(120)


async def _microstructure_memory_loop() -> None:
    await asyncio.sleep(3)
    await _run_microstructure_memory_once(hydrate=True, prune=True)
    cycles = 0
    while True:
        await asyncio.sleep(30)
        cycles += 1
        await _run_microstructure_memory_once(prune=(cycles % 120 == 0))


async def start_runtime() -> list[asyncio.Task[Any]]:
    runtime_state.started_at = datetime.now(timezone.utc)
    if not settings.scheduler_enabled:
        return []

    return [
        asyncio.create_task(_scanner_loop(), name="explodex-scanner-loop"),
        asyncio.create_task(_paper_manage_loop(), name="explodex-paper-manage-loop"),
        asyncio.create_task(_paper_sync_loop(), name="explodex-visible-paper-heart-loop"),
        asyncio.create_task(_edge_loop(), name="explodex-edge-loop"),
        asyncio.create_task(_verdict_memory_loop(), name="explodex-verdict-memory-loop"),
        asyncio.create_task(_microstructure_memory_loop(), name="explodex-microstructure-memory-loop"),
    ]


async def stop_runtime(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
