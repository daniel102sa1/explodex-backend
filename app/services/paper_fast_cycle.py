from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.chati_sarpon_612_monitor import monitor_open_paper_positions
from app.services.paper_horizon_manager import close_due_positions
from app.services.paper_loss_autopsy import portfolio_loss_brake
from app.services.paper_pre_event_executor import execute_pre_event_contracts
from app.services.paper_quant_risk_guard import paper_quant_risk_guard
from app.services.paper_regime_router import current_paper_regime
from app.services.paper_signal_bridge import ensure_signal_fk, heart_diagnostics
from app.services.paper_structure_retest_executor import execute_structure_retest_contracts
from app.services.paper_trade_auditor import run_paper_trade_audits
from app.services.paper_unified_heart_executor import (
    PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP,
    execute_unified_heart_contracts,
)
from app.services.validation_mode import ensure_validation_schema

VERSION = "paper_fast_cycle_v14_single_authority_hardened"
_LAST_FAST_CYCLE_RESULT: dict[str, Any] | None = None
_PAPER_CYCLE_LOCK = asyncio.Lock()


def _probation_risk_multiplier(base_non_quant_multiplier: float) -> float:
    """Tiny PAPER risk used only to validate VNext while legacy history is HALT."""
    base = max(0.0, float(base_non_quant_multiplier or 0.0))
    if base <= 0:
        return 0.0
    return min(PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP, base * 0.10)


def latest_fast_cycle_result() -> dict[str, Any] | None:
    return dict(_LAST_FAST_CYCLE_RESULT) if isinstance(_LAST_FAST_CYCLE_RESULT, dict) else None


async def run_fast_paper_cycle(db: AsyncSession) -> dict[str, Any]:
    """Serialize canonical PAPER execution inside this process.

    Runtime scheduling and compatibility API calls share this same lock so a
    manual request cannot race the automatic Heart cycle and exceed portfolio
    limits or double-process exits.
    """
    if _PAPER_CYCLE_LOCK.locked():
        return {
            "version": VERSION,
            "opened": 0,
            "closed": 0,
            "reason": "paper_cycle_already_running",
            "single_paper_authority": True,
            "authority": "UNIFIED_HEART_CONTRACT_ONLY",
        }
    async with _PAPER_CYCLE_LOCK:
        return await _run_fast_paper_cycle_unlocked(db)


async def _run_fast_paper_cycle_unlocked(db: AsyncSession) -> dict[str, Any]:
    """Visible PAPER portfolio driven only by lanes emitted by the unified Heart."""
    global _LAST_FAST_CYCLE_RESULT
    await ensure_validation_schema(db)
    await base.ensure_paper_schema(db)
    await ensure_signal_fk(db)

    # Existing positions are always managed first. The quant guard is only
    # allowed to restrict NEW PAPER exposure; it never abandons exits.
    closed = await close_due_positions(db)

    regime = await current_paper_regime()
    policy = regime.get("policy") or {}
    loss_brake = await portfolio_loss_brake(db)
    quant_guard = await paper_quant_risk_guard(db)

    btc_overlay = regime.get("btc_overlay") or {}
    try:
        live_monitor = await monitor_open_paper_positions(db, btc_overlay=btc_overlay)
    except Exception as exc:
        await db.rollback()
        live_monitor = {
            "version": "chati_sarpon_612_monitor_v1",
            "paper_only": True,
            "available": False,
            "error_type": type(exc).__name__,
            "portfolio_new_entry_risk_multiplier": 1.0,
            "positions": [],
            "counts": {},
        }

    live_counts = live_monitor.get("counts") or {}
    live_red = int(live_counts.get("RED_DAMAGED") or 0)
    defensive = (
        str(loss_brake.get("mode") or "NORMAL").upper() == "DEFENSIVE"
        or bool(btc_overlay.get("force_defensive"))
        or live_red > 0
    )
    # Keep market/regime/live-monitor brakes independent from the historical
    # portfolio quant guard. A bad legacy cohort must not erase new signals.
    base_risk_multiplier = float(loss_brake.get("trend_risk_multiplier") or 1.0)
    base_risk_multiplier *= float(((policy.get("trend_premove") or {}).get("risk_multiplier")) or 0.0)
    base_risk_multiplier *= float(live_monitor.get("portfolio_new_entry_risk_multiplier") or 1.0)
    risk_multiplier = base_risk_multiplier * float(quant_guard.get("risk_multiplier") or 0.0)

    btc_blocks_new_entries = bool(btc_overlay.get("block_new_entries"))
    validation_probation = False
    probation_risk_multiplier = 0.0

    if btc_blocks_new_entries:
        # A live market shock is not legacy history; keep this as a real hard stop.
        execution = {
            "version": "paper_unified_heart_executor_blocked_by_btc_shock",
            "opened": 0,
            "trades": [],
            "reason": "btc_shock_block",
            "signals_checked": 0,
            "candidates": 0,
            "rejected": {"btc_shock_block": 1},
            "defensive": defensive,
            "defensive_learning_enabled": defensive,
            "validation_probation": False,
            "risk_policy": {"btc_block": True},
        }
        pre_event_execution = {"opened": 0, "trades": [], "reason": "btc_shock_block", "rejected": {"btc_shock_block": 1}}
        structure_retest_execution = {"opened": 0, "trades": [], "reason": "btc_shock_block", "rejected": {"btc_shock_block": 1}}
    elif quant_guard.get("halt_new_entries"):
        # VNext probation: continue gathering *actual PAPER execution* evidence at
        # tiny risk instead of letting 206 legacy trades permanently deadlock the
        # new generation. One position max, 1x, no aggressive lane. This does not
        # reset or falsify the quant guard; the main portfolio remains HALT.
        validation_probation = base_risk_multiplier > 0
        probation_risk_multiplier = _probation_risk_multiplier(base_risk_multiplier)
        if validation_probation and probation_risk_multiplier > 0:
            execution = await execute_unified_heart_contracts(
                db,
                defensive=True,
                risk_multiplier=probation_risk_multiplier,
                btc_overlay=btc_overlay,
                validation_probation=True,
            )
        else:
            execution = {
                "version": "paper_unified_heart_executor_vnext_probation",
                "opened": 0,
                "trades": [],
                "reason": "probation_blocked_by_non_quant_risk",
                "signals_checked": 0,
                "candidates": 0,
                "rejected": {"non_quant_risk_multiplier_zero": 1},
                "defensive": True,
                "defensive_learning_enabled": True,
                "validation_probation": True,
            }
        # During probation keep the system simple: only the canonical unified
        # Heart may open. Secondary experimental executors stay off.
        pre_event_execution = {"opened": 0, "trades": [], "reason": "disabled_during_vnext_probation", "rejected": {}}
        structure_retest_execution = {"opened": 0, "trades": [], "reason": "disabled_during_vnext_probation", "rejected": {}}
    else:
        execution = await execute_unified_heart_contracts(
            db,
            defensive=defensive,
            risk_multiplier=risk_multiplier,
            btc_overlay=btc_overlay,
        )
        pre_event_execution = {"opened": 0, "reason": "higher_priority_lane_opened", "rejected": {}}
        structure_retest_execution = {"opened": 0, "reason": "higher_priority_lane_opened", "rejected": {}}
        if int(execution.get("opened") or 0) == 0:
            pre_event_execution = await execute_pre_event_contracts(
                db,
                defensive=defensive,
                risk_multiplier=risk_multiplier,
                btc_overlay=btc_overlay,
            )
        if int(execution.get("opened") or 0) == 0 and int(pre_event_execution.get("opened") or 0) == 0:
            structure_retest_execution = await execute_structure_retest_contracts(
                db,
                defensive=defensive,
                risk_multiplier=risk_multiplier,
                btc_overlay=btc_overlay,
            )

    all_trades = (
        list(execution.get("trades") or [])
        + list(pre_event_execution.get("trades") or [])
        + list(structure_retest_execution.get("trades") or [])
    )
    total_opened = (
        int(execution.get("opened") or 0)
        + int(pre_event_execution.get("opened") or 0)
        + int(structure_retest_execution.get("opened") or 0)
    )
    diagnostics = await heart_diagnostics(db, minutes=30)

    # Auditor is deliberately advisory. A failure here must never break the
    # canonical Heart/PAPER execution cycle.
    try:
        trade_audit = await run_paper_trade_audits(db)
    except Exception as exc:
        await db.rollback()
        trade_audit = {
            "version": "paper_trade_auditor_v1",
            "paper_only": True,
            "available": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        }

    summary = await base.paper_summary(db)

    if btc_blocks_new_entries:
        cycle_reason = "btc_shock_block"
    elif quant_guard.get("halt_new_entries"):
        cycle_reason = (
            "opened_vnext_probation"
            if int(execution.get("opened") or 0) > 0
            else str(execution.get("reason") or "vnext_probation_no_candidate")
        )
    elif int(pre_event_execution.get("opened") or 0):
        cycle_reason = pre_event_execution.get("reason")
    elif int(structure_retest_execution.get("opened") or 0):
        cycle_reason = structure_retest_execution.get("reason")
    else:
        cycle_reason = execution.get("reason")

    result = {
        "version": VERSION,
        "closed": closed.get("closed", 0),
        "close_actions": closed.get("actions", [])[:10],
        "opened": total_opened,
        "reason": cycle_reason,
        "unified_execution": execution,
        "pre_event_execution": pre_event_execution,
        "structure_retest_execution": structure_retest_execution,
        "trend": execution,
        "aggressive_learning": {"opened": sum(1 for item in all_trades if item.get("lane") == "AGGRESSIVE_PAPER")},
        "swing_trajectory": {"opened": sum(1 for item in all_trades if item.get("lane") == "SWING_PAPER")},
        "pre_event_learning": {"opened": sum(1 for item in all_trades if item.get("lane") == "PRE_EVENT_PAPER")},
        "structure_retest_learning": {"opened": sum(1 for item in all_trades if item.get("lane") == "STRUCTURE_RETEST_PAPER")},
        "heart_diagnostics": diagnostics,
        "regime": regime,
        "loss_brake": loss_brake,
        "quant_risk_guard": quant_guard,
        "btc_overlay": btc_overlay,
        "trade_audit": trade_audit,
        "chati_sarpon_612_live_monitor": live_monitor,
        "effective_new_entry_risk_multiplier": round(max(0.0, risk_multiplier), 4),
        "base_non_quant_risk_multiplier": round(max(0.0, base_risk_multiplier), 4),
        "validation_probation": {
            "active": validation_probation,
            "risk_multiplier": round(max(0.0, probation_risk_multiplier), 4),
            "main_quant_guard_still_halted": bool(quant_guard.get("halt_new_entries")),
            "one_position_max": True,
            "forces_1x_leverage": True,
            "aggressive_lane_disabled": True,
        },
        "equity": summary.get("equity"),
        "open_positions": len(summary.get("open_positions") or []),
        "single_paper_authority": True,
        "authority": "UNIFIED_HEART_CONTRACT_ONLY",
        "quant_guard_can_choose_direction": False,
        "quant_guard_can_create_entry": False,
        "btc_overlay_can_create_entry": False,
        "btc_overlay_can_widen_stop_after_entry": False,
        "chati_monitor_can_create_entry": False,
        "chati_monitor_can_flip_direction": False,
        "chati_monitor_can_widen_stop_after_entry": False,
    }
    _LAST_FAST_CYCLE_RESULT = result
    return result
