from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.heart_reconciliation import reconcile_canonical_theses
from app.services.paper_adaptive_risk import adaptive_leverage
from app.services.paper_advanced_prefilter import prefilter_new_micro_signals
from app.services.paper_loss_autopsy import portfolio_loss_brake
from app.services.paper_micro_scalp import (
    close_expired_micro_positions,
    open_micro_positions,
    scan_micro_scalps,
)
from app.services.paper_orders import sync_paper_orders
from app.services.paper_range_micro import (
    close_expired_range_positions,
    open_range_positions,
    scan_all_eligible_ranges,
)
from app.services.paper_regime_router import current_paper_regime
from app.services.trade_thesis import mark_thesis_entered


def _valid_geometry(side: str, entry: float, stop: float, tp: float) -> bool:
    if side == "LONG":
        return stop < entry < tp
    if side == "SHORT":
        return tp < entry < stop
    return False


def _scale_sizing(sizing: dict[str, Any], multiplier: float) -> dict[str, Any]:
    multiplier = max(0.0, min(1.0, float(multiplier)))
    if 0.999 <= multiplier <= 1.001:
        return dict(sizing)
    out = dict(sizing)
    for key in ("quantity", "notional", "margin", "risk_usdt"):
        if key in out:
            out[key] = round(base._f(out.get(key)) * multiplier, 10)
    return out


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


async def open_new_positions_live_fill(
    db: AsyncSession,
    *,
    risk_multiplier: float = 1.0,
    regime: dict[str, Any] | None = None,
    defensive: bool = False,
) -> dict[str, Any]:
    """Open PAPER positions only from the canonical ExplodeX Heart ENTER decision."""
    regime = regime or {}
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one())
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if slots <= 0:
        return {"opened": 0, "stale_skipped": 0, "heart_blocked": 0, "no_chase": 0, "risk_multiplier": risk_multiplier}

    candidates = (await db.execute(text("""
        SELECT vo.signal_id::text, vo.symbol, vo.observed_at,
               s.direction, s.state AS signal_state, s.reason AS signal_reason,
               vo.trade_class, vo.grade, vo.fingerprint_score,
               vo.catalyst_state, vo.master_state
        FROM validation_observations vo
        JOIN signals s ON s.id=vo.signal_id
        LEFT JOIN paper_positions pp ON pp.signal_id=vo.signal_id
        WHERE pp.signal_id IS NULL
          AND s.state='READY'
          AND COALESCE((s.reason->'explodex_heart'->>'execution_allowed')::boolean, FALSE)=TRUE
          AND vo.trade_class='TRADE_NOW'
          AND COALESCE(vo.master_state,'YES')='YES'
          AND vo.observed_at >= NOW() - INTERVAL '20 minutes'
        ORDER BY vo.observed_at ASC
        LIMIT :slots
    """), {"slots": max(slots * 4, slots)})).mappings().all()

    opened = 0
    stale_skipped = 0
    anti_loss_skipped = 0
    heart_blocked = 0
    no_chase = 0

    for raw in candidates:
        if opened >= slots:
            break
        row = dict(raw)
        reason = _as_dict(row.get("signal_reason"))
        heart = _as_dict(reason.get("explodex_heart"))
        thesis = _as_dict(heart.get("thesis"))
        plan = _as_dict(heart.get("plan"))
        decision = _as_dict(heart.get("action_decision"))

        side = str(heart.get("direction") or row.get("direction") or "").upper()
        if (
            not bool(heart.get("execution_allowed"))
            or not bool(decision.get("should_enter", heart.get("execution_allowed")))
            or str(row.get("signal_state") or "") != "READY"
            or not bool(thesis.get("frozen_plan"))
        ):
            heart_blocked += 1
            continue

        entry_low = base._f(plan.get("entry_low"), base._f(thesis.get("entry_low")))
        entry_high = base._f(plan.get("entry_high"), base._f(thesis.get("entry_high")))
        stop = base._f(plan.get("stop_loss"), base._f(thesis.get("stop_loss")))
        tp = base._f(plan.get("tp1"), base._f(thesis.get("tp1")))
        fingerprint_score = base._f(row.get("fingerprint_score"))
        fill = await base._latest_price(row["symbol"])

        if fill <= 0 or entry_low <= 0 or entry_high <= 0 or stop <= 0 or tp <= 0:
            stale_skipped += 1
            continue
        if not (entry_low <= fill <= entry_high):
            if (side == "LONG" and fill > entry_high) or (side == "SHORT" and fill < entry_low):
                no_chase += 1
            heart_blocked += 1
            continue
        if not _valid_geometry(side, fill, stop, tp):
            stale_skipped += 1
            continue

        regime_name = str(regime.get("regime") or regime.get("name") or "")
        regime_aligned = (side == "LONG" and regime_name == "TREND_UP") or (side == "SHORT" and regime_name == "TREND_DOWN")
        leverage = adaptive_leverage(
            grade=row.get("grade"),
            fingerprint_score=fingerprint_score,
            catalyst_state=row.get("catalyst_state"),
            regime_aligned=regime_aligned,
            defensive=defensive,
        )
        sizing = base.size_position(balance, fill, stop, leverage)
        combined_risk_multiplier = min(1.0, max(0.0, risk_multiplier))
        sizing = _scale_sizing(sizing, combined_risk_multiplier)
        if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
            anti_loss_skipped += 1
            continue

        opened_at = datetime.now(timezone.utc)
        metadata = json.dumps({
            "execution_version": "paper_execution_v2_multi_strategy_v10_actionable_heart",
            "strategy_mode": "TREND_PREMOVE",
            "signal_observed_at": row["observed_at"].isoformat() if row.get("observed_at") else None,
            "simulated_fill_price": fill,
            "uses_current_observable_price": True,
            "explodex_heart": heart,
            "heart_approved": True,
            "action_decision": decision,
            "plan_is_frozen": True,
            "legacy_phase_required": False,
            "post_signal_direction_recalculation": False,
            "post_signal_stop_widening": False,
            "adaptive_leverage": leverage,
            "combined_risk_multiplier": combined_risk_multiplier,
        })
        result = await db.execute(text("""
            INSERT INTO paper_positions (
                signal_id, symbol, side, grade, fingerprint_score, leverage, entry_price, stop_loss,
                take_profit, quantity, notional, margin_used, risk_usdt, opened_at, metadata
            ) VALUES (
                CAST(:signal_id AS UUID), :symbol, :side, :grade, :fingerprint_score, :leverage,
                :entry_price, :stop_loss, :take_profit, :quantity, :notional, :margin_used,
                :risk_usdt, :opened_at, CAST(:metadata AS JSONB)
            ) ON CONFLICT (signal_id) DO NOTHING
        """), {
            "signal_id": row["signal_id"], "symbol": row["symbol"], "side": side,
            "grade": row.get("grade"), "fingerprint_score": fingerprint_score,
            "leverage": leverage, "entry_price": fill, "stop_loss": stop, "take_profit": tp,
            "quantity": sizing["quantity"], "notional": sizing["notional"], "margin_used": sizing["margin"],
            "risk_usdt": sizing["risk_usdt"], "opened_at": opened_at, "metadata": metadata,
        })
        if result.rowcount:
            await mark_thesis_entered(db, row["symbol"])
            opened += 1

    await db.commit()
    return {
        "opened": opened,
        "stale_skipped": stale_skipped,
        "anti_loss_skipped": anti_loss_skipped,
        "heart_blocked": heart_blocked,
        "no_chase": no_chase,
        "risk_multiplier": risk_multiplier,
    }


async def run_paper_cycle_v2(db: AsyncSession) -> dict[str, Any]:
    await base.ensure_paper_schema(db)
    closed = await base._close_due_positions(db)
    thesis_reconciliation = await reconcile_canonical_theses(db)
    range_expired = await close_expired_range_positions(db)
    micro_expired = await close_expired_micro_positions(db)

    regime = await current_paper_regime()
    policy = regime.get("policy") or {}
    range_enabled_by_regime = bool((policy.get("range_micro") or {}).get("enabled", True))
    micro_enabled_by_regime = bool((policy.get("micro_scalp") or {}).get("enabled", True))

    loss_brake = await portfolio_loss_brake(db)
    defensive = str(loss_brake.get("mode") or "NORMAL").upper() == "DEFENSIVE"
    secondary_entries_enabled = bool(loss_brake.get("secondary_entries_enabled", True))
    trend_risk_multiplier = float(loss_brake.get("trend_risk_multiplier") or 1.0)
    trend_regime_multiplier = float(((policy.get("trend_premove") or {}).get("risk_multiplier")) or 1.0)
    trend_risk_multiplier *= trend_regime_multiplier

    range_enabled = range_enabled_by_regime and secondary_entries_enabled
    micro_enabled = micro_enabled_by_regime and secondary_entries_enabled

    trend_opened = await open_new_positions_live_fill(db, risk_multiplier=trend_risk_multiplier, regime=regime, defensive=defensive)

    range_scan = await scan_all_eligible_ranges(db)
    range_opened = await open_range_positions(db) if range_enabled else {
        "opened": 0, "skipped": 0,
        "regime_blocked": not range_enabled_by_regime,
        "loss_brake_blocked": defensive and not secondary_entries_enabled,
    }

    micro_scan = await scan_micro_scalps(db)
    advanced_prefilter = await prefilter_new_micro_signals(db)
    micro_opened = await open_micro_positions(db) if micro_enabled else {
        "opened": 0, "skipped": 0,
        "regime_blocked": not micro_enabled_by_regime,
        "loss_brake_blocked": defensive and not secondary_entries_enabled,
    }

    order_sync = await sync_paper_orders(db)
    summary = await base.paper_summary(db)
    await db.execute(text("""
        INSERT INTO paper_equity_curve (cash_balance, unrealized_pnl, equity, open_positions)
        VALUES (:cash, :unrealized, :equity, :open_positions)
    """), {
        "cash": summary["cash_balance"], "unrealized": summary["unrealized_pnl"],
        "equity": summary["equity"], "open_positions": len(summary["open_positions"]),
    })
    await db.commit()
    return {
        "execution_version": "paper_execution_v2_multi_strategy_v10_actionable_heart",
        "regime_router": regime,
        "loss_brake": loss_brake,
        "thesis_reconciliation": thesis_reconciliation,
        "trend": {**closed, **trend_opened},
        "range_micro": {
            "expired": range_expired.get("closed", 0),
            "enabled_by_regime": range_enabled_by_regime,
            "enabled_after_loss_brake": range_enabled,
            "scan": range_scan,
            **range_opened,
        },
        "micro_scalp": {
            "expired": micro_expired.get("closed", 0),
            "enabled_by_regime": micro_enabled_by_regime,
            "enabled_after_loss_brake": micro_enabled,
            "scan": micro_scan,
            "advanced_prefilter": advanced_prefilter,
            **micro_opened,
        },
        "orders": order_sync,
        "equity": summary["equity"],
    }
