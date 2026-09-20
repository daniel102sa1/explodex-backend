from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.paper_edge_lab import edge_lab_report
from app.services.chati_sarpon_612_monitor import open_monitor_report
from app.services.paper_fast_cycle import VERSION as EXECUTION_VERSION, latest_fast_cycle_result, run_fast_paper_cycle
from app.services.paper_loss_autopsy import loss_autopsy_report
from app.services.paper_micro_scalp import micro_summary, scan_micro_scalps
from app.services.paper_orders import paper_order_history, paper_order_stats
from app.services.paper_portfolio import ensure_paper_schema, paper_history, paper_summary
from app.services.paper_quant_risk_guard import paper_quant_risk_guard
from app.services.quant_brain_persistence import quant_brain_report
from app.services.paper_range_micro import range_summary, scan_all_eligible_ranges
from app.services.paper_signal_bridge import ensure_signal_fk, heart_diagnostics
from app.services.paper_trade_auditor import paper_trade_audit_report, run_paper_trade_audits
from app.services.sarpon_knowledge import sarpon_knowledge_registry
from app.services.validation_mode import ensure_validation_schema

router = APIRouter(prefix="/api/v1/paper-trading", tags=["paper-trading"])


async def _ensure_paper_dependencies(db: AsyncSession) -> None:
    await ensure_validation_schema(db)
    await ensure_paper_schema(db)
    await ensure_signal_fk(db)


async def _safe_component(
    db: AsyncSession,
    name: str,
    loader: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    try:
        return await loader(db)
    except Exception as exc:
        await db.rollback()
        return {"available": False, "paper_only": True, "component": name, "error_type": type(exc).__name__}


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    result = await paper_summary(db)
    result["execution_version"] = EXECUTION_VERSION
    result["schema_bridge"] = "signals_fk_ready"
    result["heart_diagnostics"] = await _safe_component(db, "heart_diagnostics", lambda session: heart_diagnostics(session, minutes=30))
    latest = latest_fast_cycle_result()
    if latest:
        execution = latest.get("unified_execution") or {}
        result["unified_paper_diagnostics"] = {
            "version": latest.get("version"),
            "single_paper_authority": True,
            "opened_last_cycle": latest.get("opened", 0),
            "closed_last_cycle": latest.get("closed", 0),
            "reason": latest.get("reason"),
            "signals_checked": execution.get("signals_checked"),
            "candidates": execution.get("candidates"),
            "rejected": execution.get("rejected") or {},
            "defensive": execution.get("defensive"),
            "defensive_learning_enabled": execution.get("defensive_learning_enabled"),
            "risk_policy": execution.get("risk_policy"),
            "quant_risk_guard": latest.get("quant_risk_guard"),
            "effective_new_entry_risk_multiplier": latest.get("effective_new_entry_risk_multiplier"),
            "regime": latest.get("regime") or {},
            "btc_overlay": latest.get("btc_overlay") or {},
            "chati_sarpon_612_live_monitor": latest.get("chati_sarpon_612_live_monitor") or {},
            "trades": (execution.get("trades") or [])[:8],
            "pre_event_execution": latest.get("pre_event_execution") or {},
            "structure_retest_execution": latest.get("structure_retest_execution") or {},
            "structure_retest_learning": latest.get("structure_retest_learning") or {},
        }
    else:
        result["unified_paper_diagnostics"] = {
            "single_paper_authority": True,
            "status": "WAITING_FIRST_CYCLE",
            "rejected": {},
        }
    result["orders"] = await _safe_component(db, "orders", paper_order_stats)
    result["range_micro"] = await _safe_component(db, "range_micro", range_summary)
    result["micro_scalp"] = await _safe_component(db, "micro_scalp", micro_summary)
    result["loss_autopsy"] = await _safe_component(db, "loss_autopsy", lambda session: loss_autopsy_report(session, days=30))
    result["quant_risk_guard"] = await _safe_component(db, "quant_risk_guard", paper_quant_risk_guard)
    result["quant_brain"] = await _safe_component(db, "quant_brain", quant_brain_report)
    result["chati_sarpon_612_monitor"] = await _safe_component(db, "chati_sarpon_612_monitor", open_monitor_report)
    result["sarpon_knowledge"] = sarpon_knowledge_registry()
    result["trade_audit"] = await _safe_component(db, "trade_audit", paper_trade_audit_report)
    return result


@router.get("/edge-lab")
async def adaptive_edge_lab(days: int = Query(default=30, ge=1, le=365), db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await edge_lab_report(db, days=days)


@router.get("/loss-autopsy")
async def paper_loss_autopsy(days: int = Query(default=30, ge=1, le=365), db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await loss_autopsy_report(db, days=days)


@router.get("/chati-sarpon-612")
async def chati_sarpon_612(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await open_monitor_report(db)


@router.get("/sarpon-knowledge")
async def sarpon_knowledge():
    return sarpon_knowledge_registry()


@router.get("/quant-brain")
async def quant_brain(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await quant_brain_report(db)


@router.get("/quant-risk")
async def quant_risk(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await paper_quant_risk_guard(db)


@router.get("/audit")
async def trade_audit(closed_limit: int = Query(default=20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await paper_trade_audit_report(db, closed_limit=closed_limit)


@router.post("/audit/run")
async def run_trade_audit(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return {
        "paper_only": True,
        "result": await run_paper_trade_audits(db),
        "note": "Audita stops, +1R, TP1 y manejo. No ensancha ni modifica stops vivos.",
    }


@router.get("/history")
async def history(limit: int = Query(default=100, ge=1, le=500), db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return {"version": "paper_portfolio_v1", "execution_version": EXECUTION_VERSION, "paper_only": True, "rows": await paper_history(db, limit=limit)}


@router.get("/orders")
async def orders(limit: int = Query(default=200, ge=1, le=1000), status: str | None = Query(default=None), db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    normalized = str(status or "").upper() or None
    if normalized not in {None, "PENDING", "FILLED", "CANCELED"}:
        normalized = None
    return {"version": "paper_orders_v1", "paper_only": True, "stats": await paper_order_stats(db), "rows": await paper_order_history(db, limit=limit, status=normalized)}


@router.get("/range-micro")
async def range_micro_summary(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await range_summary(db)


@router.post("/range-micro/scan")
async def range_micro_scan(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return {"paper_only": True, "result": await scan_all_eligible_ranges(db, force=True), "note": "Escanea Futures USDT elegibles para investigación PAPER; no envía órdenes reales."}


@router.get("/micro-scalp")
async def micro_scalp_summary(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await micro_summary(db)


@router.post("/micro-scalp/scan")
async def micro_scalp_scan(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return {"paper_only": True, "result": await scan_micro_scalps(db, force=True), "note": "MICRO SCALP es investigación PAPER y no cambia la decisión canónica del Heart."}


@router.post("/run-fast")
async def run_fast_cycle(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return {"paper_only": True, "result": await run_fast_paper_cycle(db), "note": "Ciclo rápido del PAPER visible: un solo Heart canónico + precio actual + zona + riesgo. No envía órdenes reales."}


@router.post("/run")
async def run_cycle(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return {
        "version": "paper_portfolio_v1",
        "execution_version": EXECUTION_VERSION,
        "paper_only": True,
        "result": await run_fast_paper_cycle(db),
        "note": "Alias seguro del único ciclo PAPER canónico. Los laboratorios legacy ya no pueden abrir posiciones desde este endpoint.",
    }
