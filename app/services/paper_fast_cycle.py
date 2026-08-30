from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.paper_aggressive_learning import open_aggressive_learning_position
from app.services.paper_execution_v2 import open_new_positions_live_fill
from app.services.paper_loss_autopsy import portfolio_loss_brake
from app.services.paper_regime_router import current_paper_regime
from app.services.paper_signal_bridge import ensure_signal_fk, heart_diagnostics
from app.services.validation_mode import ensure_validation_schema

VERSION = "paper_fast_cycle_v2_aggressive_learning"


async def run_fast_paper_cycle(db: AsyncSession) -> dict[str, Any]:
    """Frequent PAPER execution loop for the portfolio visible at /paper.

    Canonical Heart ENTER is always tried first. If it opens nothing, a separate
    reduced-risk PAPER-only experiment may sample an ARMED/IGNITING setup. The
    experiment never upgrades the user's Heart recommendation and is disabled
    while the portfolio loss brake is defensive.
    """
    await ensure_validation_schema(db)
    await base.ensure_paper_schema(db)
    await ensure_signal_fk(db)

    closed = await base._close_due_positions(db)
    regime = await current_paper_regime()
    policy = regime.get("policy") or {}
    loss_brake = await portfolio_loss_brake(db)
    defensive = str(loss_brake.get("mode") or "NORMAL").upper() == "DEFENSIVE"
    trend_risk_multiplier = float(loss_brake.get("trend_risk_multiplier") or 1.0)
    trend_risk_multiplier *= float(((policy.get("trend_premove") or {}).get("risk_multiplier")) or 1.0)

    canonical = await open_new_positions_live_fill(
        db,
        risk_multiplier=trend_risk_multiplier,
        regime=regime,
        defensive=defensive,
    )

    aggressive = await open_aggressive_learning_position(
        db,
        normal_opened=int(canonical.get("opened") or 0),
        defensive=defensive,
    )

    diagnostics = await heart_diagnostics(db, minutes=30)
    summary = await base.paper_summary(db)
    total_opened = int(canonical.get("opened") or 0) + int(aggressive.get("opened") or 0)
    reason = (
        "canonical_opened"
        if int(canonical.get("opened") or 0) > 0
        else "aggressive_learning_opened"
        if int(aggressive.get("opened") or 0) > 0
        else canonical.get("reason") or aggressive.get("reason")
    )

    return {
        "version": VERSION,
        "closed": closed.get("closed", 0),
        "opened": total_opened,
        "reason": reason,
        "trend": canonical,
        "aggressive_learning": aggressive,
        "heart_diagnostics": diagnostics,
        "equity": summary.get("equity"),
        "open_positions": len(summary.get("open_positions") or []),
    }
