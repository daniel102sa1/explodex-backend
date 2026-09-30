from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.paper_edge_lab import edge_lab_report
from app.services.chati_sarpon_612_monitor import open_monitor_report
from app.services.paper_fast_cycle import VERSION as EXECUTION_VERSION, latest_fast_cycle_result, run_fast_paper_cycle
from app.services.formula_brain import formula_brain_calibration_report, formula_registry
from app.services.macro_cycle_persistence import macro_cycle_report
from app.services.paper_loss_autopsy import loss_autopsy_report
from app.services.paper_micro_scalp import micro_summary, scan_micro_scalps
from app.services.paper_manual import (
    cancel_manual_order,
    close_manual_position,
    create_manual_order,
    manual_account_snapshot,
    manual_history,
    move_manual_stop_to_be,
    reset_manual_practice_account,
    update_manual_risk,
)
from app.services.paper_orders import paper_order_history, paper_order_stats
from app.services.paper_portfolio import ARSENAL_DISPLAY_START, ensure_paper_schema, paper_arsenal_summary, paper_equity_curve, paper_history, paper_signal_history, paper_summary
from app.services.paper_quant_risk_guard import paper_quant_risk_guard
from app.services.quant_brain_persistence import quant_brain_report
from app.services.paper_range_micro import range_summary, scan_all_eligible_ranges
from app.services.paper_signal_bridge import ensure_signal_fk, heart_diagnostics
from app.services.paper_trade_auditor import paper_trade_audit_report, run_paper_trade_audits
from app.services.sarpon_knowledge import sarpon_knowledge_registry
from app.services.validation_mode import ensure_validation_schema
from app.services.vnext_evaluation import vnext_evaluation_report

router = APIRouter(prefix="/api/v1/paper-trading", tags=["paper-trading"])


class ManualPracticeOpen(BaseModel):
    symbol: str = Field(min_length=3, max_length=32)
    side: str
    order_type: str = "MARKET"
    margin_usdt: float = Field(gt=0, le=100000)
    leverage: int = Field(ge=1, le=20)
    stop_loss: float = Field(gt=0)
    tp1: float | None = Field(default=None, gt=0)
    tp2: float | None = Field(default=None, gt=0)
    tp3: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    practice_note: str | None = Field(default=None, max_length=500)
    auto_be_after_tp1: bool = False


class ManualRiskUpdate(BaseModel):
    stop_loss: float = Field(gt=0)
    tp1: float = Field(gt=0)
    tp2: float = Field(gt=0)
    tp3: float = Field(gt=0)


class ManualPartialClose(BaseModel):
    fraction: float = Field(default=1.0, gt=0, le=1)


def _safe_manual_symbol(symbol: str) -> str:
    value = str(symbol or "").upper().strip().replace("/", "")
    if not value.endswith("USDT"):
        value += "USDT"
    if not value.replace("USDT", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid symbol")
    return value



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
async def summary(
    scope: str = Query(default="arsenal"),
    details: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_paper_dependencies(db)
    normalized_scope = "all" if str(scope).lower() == "all" else "arsenal"
    result = await (paper_summary(db) if normalized_scope == "all" else paper_arsenal_summary(db))
    result["requested_scope"] = normalized_scope
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
    # The dashboard needs balance/open positions quickly. Heavy research and
    # audit reports are opt-in because calculating all of them on every refresh
    # previously made this endpoint take ~30 seconds.
    result["details_included"] = bool(details)
    if details:
        result["orders"] = await _safe_component(db, "orders", paper_order_stats)
        result["range_micro"] = await _safe_component(db, "range_micro", range_summary)
        result["micro_scalp"] = await _safe_component(db, "micro_scalp", micro_summary)
        result["loss_autopsy"] = await _safe_component(db, "loss_autopsy", lambda session: loss_autopsy_report(session, days=30))
        result["quant_risk_guard"] = await _safe_component(db, "quant_risk_guard", paper_quant_risk_guard)
        result["quant_brain"] = await _safe_component(db, "quant_brain", quant_brain_report)
        result["chati_sarpon_612_monitor"] = await _safe_component(db, "chati_sarpon_612_monitor", open_monitor_report)
        result["sarpon_knowledge"] = sarpon_knowledge_registry()
        result["formula_brain"] = await _safe_component(db, "formula_brain", formula_brain_calibration_report)
        result["macro_cycle"] = await _safe_component(db, "macro_cycle", macro_cycle_report)
        result["trade_audit"] = await _safe_component(db, "trade_audit", paper_trade_audit_report)
        result["vnext_evaluation"] = await _safe_component(db, "vnext_evaluation", vnext_evaluation_report)
    return result


@router.get("/vnext-evaluation")
async def vnext_evaluation(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await vnext_evaluation_report(db)


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


@router.get("/macro-cycle")
async def macro_cycle(minutes: int = Query(default=180, ge=30, le=1440), limit: int = Query(default=20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await macro_cycle_report(db, minutes=minutes, limit=limit)


@router.get("/formula-brain")
async def formula_brain(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await formula_brain_calibration_report(db)


@router.get("/formula-registry")
async def formula_registry_endpoint():
    return formula_registry()


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


@router.get("/equity-curve")
async def equity_curve(
    limit: int = Query(default=500, ge=1, le=5000),
    scope: str = Query(default="arsenal"),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_paper_dependencies(db)
    normalized_scope = "all" if str(scope).lower() == "all" else "arsenal"
    return await paper_equity_curve(
        db,
        limit=limit,
        opened_after=None if normalized_scope == "all" else ARSENAL_DISPLAY_START,
    )


@router.get("/signal-history")
async def signal_history(
    limit: int = Query(default=200, ge=1, le=1000),
    scope: str = Query(default="arsenal"),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_paper_dependencies(db)
    normalized_scope = "all" if str(scope).lower() == "all" else "arsenal"
    return {
        "version": "paper_signal_history_v1",
        "paper_only": True,
        "scope": normalized_scope,
        "rows": await paper_signal_history(
            db,
            limit=limit,
            created_after=None if normalized_scope == "all" else ARSENAL_DISPLAY_START,
        ),
    }


@router.get("/history")
async def history(
    limit: int = Query(default=100, ge=1, le=500),
    scope: str = Query(default="arsenal"),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_paper_dependencies(db)
    normalized_scope = "all" if str(scope).lower() == "all" else "arsenal"
    return {
        "version": "paper_portfolio_v1",
        "execution_version": EXECUTION_VERSION,
        "paper_only": True,
        "scope": normalized_scope,
        "rows": await paper_history(
            db,
            limit=limit,
            opened_after=None if normalized_scope == "all" else ARSENAL_DISPLAY_START,
        ),
    }


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


@router.get("/manual/account")
async def manual_practice_account(db: AsyncSession = Depends(get_db)):
    return await manual_account_snapshot(db)


@router.get("/manual/history")
async def manual_practice_history(limit: int = Query(default=50, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    return {"paper_only": True, "rows": await manual_history(db, limit=limit)}


@router.post("/manual/open")
async def manual_practice_open(payload: ManualPracticeOpen, db: AsyncSession = Depends(get_db)):
    try:
        fallback_tp = payload.take_profit
        tp1 = payload.tp1 or fallback_tp
        tp2 = payload.tp2 or fallback_tp
        tp3 = payload.tp3 or fallback_tp
        if not all([tp1, tp2, tp3]):
            raise ValueError("missing_take_profit")
        return await create_manual_order(
            db,
            symbol=_safe_manual_symbol(payload.symbol),
            side=payload.side,
            order_type=payload.order_type,
            margin_usdt=payload.margin_usdt,
            leverage=payload.leverage,
            stop_loss=payload.stop_loss,
            tp1=float(tp1),
            tp2=float(tp2),
            tp3=float(tp3),
            limit_price=payload.limit_price,
            practice_note=payload.practice_note,
            auto_be_after_tp1=payload.auto_be_after_tp1,
        )
    except ValueError as exc:
        await db.rollback()
        messages = {
            "invalid_side": "Side must be LONG or SHORT.",
            "invalid_order_type": "Order type must be MARKET or LIMIT.",
            "invalid_margin": "Paper margin must be greater than zero.",
            "invalid_limit_price": "A valid limit price is required.",
            "long_limit_must_be_below_market": "A LONG limit entry must be below the current market price.",
            "short_limit_must_be_above_market": "A SHORT limit entry must be above the current market price.",
            "invalid_long_geometry": "For LONG use SL < entry < TP1 <= TP2 <= TP3.",
            "invalid_short_geometry": "For SHORT use TP3 <= TP2 <= TP1 < entry < SL.",
            "insufficient_paper_margin": "Not enough fictitious available margin.",
            "market_price_unavailable": "Live market price is unavailable.",
            "missing_take_profit": "TP1, TP2 and TP3 are required.",
        }
        raise HTTPException(status_code=400, detail=messages.get(str(exc), str(exc))) from exc


@router.post("/manual/close/{position_id}")
async def manual_practice_close(position_id: int, payload: ManualPartialClose = ManualPartialClose(), db: AsyncSession = Depends(get_db)):
    try:
        return await close_manual_position(db, position_id, fraction=payload.fraction)
    except ValueError as exc:
        await db.rollback()
        messages = {
            "position_not_open": "Paper position is not open.",
            "market_price_unavailable": "Live market price is unavailable.",
        }
        raise HTTPException(status_code=400, detail=messages.get(str(exc), str(exc))) from exc


@router.post("/manual/be/{position_id}")
async def manual_practice_break_even(position_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await move_manual_stop_to_be(db, position_id)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/manual/update/{position_id}")
async def manual_practice_update(position_id: int, payload: ManualRiskUpdate, db: AsyncSession = Depends(get_db)):
    try:
        return await update_manual_risk(
            db,
            position_id,
            stop_loss=payload.stop_loss,
            tp1=payload.tp1,
            tp2=payload.tp2,
            tp3=payload.tp3,
        )
    except ValueError as exc:
        await db.rollback()
        messages = {
            "position_not_open": "Paper position is not open.",
            "invalid_long_geometry": "For LONG use SL <= entry < TP1 <= TP2 <= TP3.",
            "invalid_short_geometry": "For SHORT use TP3 <= TP2 <= TP1 < entry <= SL.",
        }
        raise HTTPException(status_code=400, detail=messages.get(str(exc), str(exc))) from exc


@router.post("/manual/cancel/{order_id}")
async def manual_practice_cancel(order_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await cancel_manual_order(db, order_id)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/manual/reset")
async def manual_practice_reset(db: AsyncSession = Depends(get_db)):
    return await reset_manual_practice_account(db)
