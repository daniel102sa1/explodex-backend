from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.paper_horizon_manager import close_due_positions
from app.services.paper_loss_autopsy import portfolio_loss_brake
from app.services.paper_pre_event_executor import execute_pre_event_contracts
from app.services.paper_regime_router import current_paper_regime
from app.services.paper_signal_bridge import ensure_signal_fk, heart_diagnostics
from app.services.paper_sizing_patch import install_corrected_paper_sizing
from app.services.paper_unified_heart_executor import execute_unified_heart_contracts
from app.services.validation_mode import ensure_validation_schema

VERSION = "paper_fast_cycle_v7_pre_event_learning"
_LAST_FAST_CYCLE_RESULT: dict[str, Any] | None = None

install_corrected_paper_sizing()


def latest_fast_cycle_result() -> dict[str, Any] | None:
    return dict(_LAST_FAST_CYCLE_RESULT) if isinstance(_LAST_FAST_CYCLE_RESULT, dict) else None


async def run_fast_paper_cycle(db: AsyncSession) -> dict[str, Any]:
    """Visible PAPER portfolio driven only by lanes emitted by the unified Heart."""
    global _LAST_FAST_CYCLE_RESULT
    await ensure_validation_schema(db)
    await base.ensure_paper_schema(db)
    await ensure_signal_fk(db)

    closed = await close_due_positions(db)
    regime = await current_paper_regime()
    policy = regime.get("policy") or {}
    loss_brake = await portfolio_loss_brake(db)
    defensive = str(loss_brake.get("mode") or "NORMAL").upper() == "DEFENSIVE"
    risk_multiplier = float(loss_brake.get("trend_risk_multiplier") or 1.0)
    risk_multiplier *= float(((policy.get("trend_premove") or {}).get("risk_multiplier")) or 1.0)

    execution = await execute_unified_heart_contracts(db, defensive=defensive, risk_multiplier=risk_multiplier)
    pre_event_execution = {"opened": 0, "reason": "higher_priority_lane_opened", "rejected": {}}
    if int(execution.get("opened") or 0) == 0:
        pre_event_execution = await execute_pre_event_contracts(db, defensive=defensive, risk_multiplier=risk_multiplier)

    all_trades = list(execution.get("trades") or []) + list(pre_event_execution.get("trades") or [])
    total_opened = int(execution.get("opened") or 0) + int(pre_event_execution.get("opened") or 0)
    diagnostics = await heart_diagnostics(db, minutes=30)
    summary = await base.paper_summary(db)
    result = {
        "version": VERSION,
        "closed": closed.get("closed", 0),
        "close_actions": closed.get("actions", [])[:10],
        "opened": total_opened,
        "reason": pre_event_execution.get("reason") if int(pre_event_execution.get("opened") or 0) else execution.get("reason"),
        "unified_execution": execution,
        "pre_event_execution": pre_event_execution,
        "trend": execution,
        "aggressive_learning": {"opened": sum(1 for item in all_trades if item.get("lane") == "AGGRESSIVE_PAPER")},
        "swing_trajectory": {"opened": sum(1 for item in all_trades if item.get("lane") == "SWING_PAPER")},
        "pre_event_learning": {"opened": sum(1 for item in all_trades if item.get("lane") == "PRE_EVENT_PAPER")},
        "heart_diagnostics": diagnostics,
        "regime": regime,
        "loss_brake": loss_brake,
        "equity": summary.get("equity"),
        "open_positions": len(summary.get("open_positions") or []),
        "single_paper_authority": True,
        "authority": "UNIFIED_HEART_CONTRACT_ONLY",
    }
    _LAST_FAST_CYCLE_RESULT = result
    return result
