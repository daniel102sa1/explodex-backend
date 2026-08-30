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
from app.services.paper_signal_bridge import ensure_signal_fk, heart_diagnostics
from app.services.trade_thesis import mark_thesis_entered

EXECUTION_VERSION = "paper_execution_v2_multi_strategy_v11_canonical_visible_heart"


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


def _prediction_meta(reason: dict[str, Any]) -> tuple[float, str | None, str | None]:
    prediction = _as_dict(reason.get("prediction"))
    fingerprint = _as_dict(prediction.get("premove_fingerprint"))
    stack = _as_dict(prediction.get("prediction_stack_v5"))
    catalyst = _as_dict(stack.get("catalyst"))
    fp_score = base._f(fingerprint.get("fingerprint_score"), base._f(prediction.get("preactivation_score")))
    grade = fingerprint.get("grade") or stack.get("grade")
    catalyst_state = catalyst.get("state") or stack.get("catalyst_state")
    return fp_score, str(grade) if grade else None, str(catalyst_state) if catalyst_state else None


async def open_new_positions_live_fill(
    db: AsyncSession,
    *,
    risk_multiplier: float = 1.0,
    regime: dict[str, Any] | None = None,
    defensive: bool = False,
) -> dict[str, Any]:
    """Open the PAPER positions visible in the app directly from Heart ENTER.

    Validation is intentionally not an execution prerequisite. The canonical
    Heart already fuses the guarded prediction, fixed thesis, risk vetoes and
    no-chase logic. We only re-check live price/geometry and portfolio risk here.
    """
    regime = regime or {}
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one())
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if slots <= 0:
        return {
            "opened": 0,
            "reason": "max_open_positions",
            "signals_checked": 0,
            "heart_enter_signals": 0,
            "stale_skipped": 0,
            "heart_blocked": 0,
            "no_chase": 0,
            "risk_multiplier": risk_multiplier,
        }

    candidates = (await db.execute(text("""
        SELECT DISTINCT ON (s.symbol_id)
               s.id::text AS signal_id,
               sy.symbol,
               s.created_at AS observed_at,
               s.direction,
               s.state AS signal_state,
               s.setup_score,
               s.risk_score,
               s.reason AS signal_reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        LEFT JOIN paper_positions pp ON pp.signal_id=s.id
        WHERE pp.signal_id IS NULL
          AND s.is_active=TRUE
          AND s.created_at >= NOW() - INTERVAL '30 minutes'
        ORDER BY s.symbol_id, s.created_at DESC
    """))).mappings().all()

    opened = 0
    stale_skipped = 0
    anti_loss_skipped = 0
    heart_blocked = 0
    no_chase = 0
    heart_enter_signals = 0
    blockers: dict[str, int] = {}

    def block(reason: str) -> None:
        blockers[reason] = blockers.get(reason, 0) + 1

    for raw in candidates:
        if opened >= slots:
            break
        row = dict(raw)
        reason = _as_dict(row.get("signal_reason"))
        prediction = _as_dict(reason.get("prediction"))
        heart = _as_dict(reason.get("explodex_heart")) or _as_dict(prediction.get("explodex_heart"))
        thesis = _as_dict(heart.get("thesis"))
        plan = _as_dict(heart.get("plan"))
        decision = _as_dict(heart.get("action_decision"))

        should_enter = bool(heart.get("execution_allowed")) and bool(
            decision.get("should_enter", heart.get("execution_allowed"))
        )
        if should_enter:
            heart_enter_signals += 1

        side = str(heart.get("direction") or row.get("direction") or "").upper()
        if not should_enter:
            heart_blocked += 1
            block(f"heart_{str(decision.get('action') or 'esperar').lower()}")
            continue
        if str(row.get("signal_state") or "") != "READY":
            heart_blocked += 1
            block("signal_not_ready")
            continue
        if not bool(thesis.get("frozen_plan")):
            heart_blocked += 1
            block("no_frozen_thesis")
            continue

        entry_low = base._f(plan.get("entry_low"), base._f(thesis.get("entry_low")))
        entry_high = base._f(plan.get("entry_high"), base._f(thesis.get("entry_high")))
        stop = base._f(plan.get("stop_loss"), base._f(thesis.get("stop_loss")))
        tp = base._f(plan.get("tp1"), base._f(thesis.get("tp1")))
        fingerprint_score, grade, catalyst_state = _prediction_meta(reason)
        fill = await base._latest_price(row["symbol"])

        if fill <= 0 or entry_low <= 0 or entry_high <= 0 or stop <= 0 or tp <= 0:
            stale_skipped += 1
            block("missing_or_invalid_plan")
            continue
        lo, hi = min(entry_low, entry_high), max(entry_low, entry_high)
        if not (lo <= fill <= hi):
            if (side == "LONG" and fill > hi) or (side == "SHORT" and fill < lo):
                no_chase += 1
                block("no_chase_price_left_zone")
            else:
                block("waiting_for_entry_zone")
            heart_blocked += 1
            continue
        if not _valid_geometry(side, fill, stop, tp):
            stale_skipped += 1
            block("invalid_geometry")
            continue

        regime_name = str(regime.get("regime") or regime.get("name") or "")
        regime_aligned = (side == "LONG" and regime_name == "TREND_UP") or (side == "SHORT" and regime_name == "TREND_DOWN")
        leverage = adaptive_leverage(
            grade=grade,
            fingerprint_score=fingerprint_score,
            catalyst_state=catalyst_state,
            regime_aligned=regime_aligned,
            defensive=defensive,
        )
        sizing = base.size_position(balance, fill, stop, leverage)
        combined_risk_multiplier = min(1.0, max(0.0, risk_multiplier))
        sizing = _scale_sizing(sizing, combined_risk_multiplier)
        if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
            anti_loss_skipped += 1
            block("position_size_zero")
            continue

        opened_at = datetime.now(timezone.utc)
        metadata = json.dumps({
            "execution_version": EXECUTION_VERSION,
            "strategy_mode": "TREND_PREMOVE",
            "signal_observed_at": row["observed_at"].isoformat() if row.get("observed_at") else None,
            "simulated_fill_price": fill,
            "uses_current_observable_price": True,
            "explodex_heart": heart,
            "heart_approved": True,
            "action_decision": decision,
            "plan_is_frozen": True,
            "validation_is_shadow_only": True,
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
            "signal_id": row["signal_id"],
            "symbol": row["symbol"],
            "side": side,
            "grade": grade,
            "fingerprint_score": fingerprint_score,
            "leverage": leverage,
            "entry_price": fill,
            "stop_loss": stop,
            "take_profit": tp,
            "quantity": sizing["quantity"],
            "notional": sizing["notional"],
            "margin_used": sizing["margin"],
            "risk_usdt": sizing["risk_usdt"],
            "opened_at": opened_at,
            "metadata": metadata,
        })
        if result.rowcount:
            await mark_thesis_entered(db, row["symbol"])
            opened += 1
            block("opened")

    await db.commit()
    primary_reason = "opened" if opened else (
        max(blockers, key=blockers.get) if blockers else "no_recent_signals"
    )
    return {
        "opened": opened,
        "reason": primary_reason,
        "signals_checked": len(candidates),
        "heart_enter_signals": heart_enter_signals,
        "stale_skipped": stale_skipped,
        "anti_loss_skipped": anti_loss_skipped,
        "heart_blocked": heart_blocked,
        "no_chase": no_chase,
        "blockers": blockers,
        "risk_multiplier": risk_multiplier,
    }


async def run_paper_cycle_v2(db: AsyncSession) -> dict[str, Any]:
    await base.ensure_paper_schema(db)
    await ensure_signal_fk(db)
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

    trend_opened = await open_new_positions_live_fill(
        db,
        risk_multiplier=trend_risk_multiplier,
        regime=regime,
        defensive=defensive,
    )

    # Secondary PAPER strategies remain research-only and cannot override the
    # canonical Heart trend decision. They are kept for comparison/learning.
    range_scan = await scan_all_eligible_ranges(db)
    range_opened = await open_range_positions(db) if range_enabled else {
        "opened": 0,
        "skipped": 0,
        "regime_blocked": not range_enabled_by_regime,
        "loss_brake_blocked": defensive and not secondary_entries_enabled,
    }

    micro_scan = await scan_micro_scalps(db)
    advanced_prefilter = await prefilter_new_micro_signals(db)
    micro_opened = await open_micro_positions(db) if micro_enabled else {
        "opened": 0,
        "skipped": 0,
        "regime_blocked": not micro_enabled_by_regime,
        "loss_brake_blocked": defensive and not secondary_entries_enabled,
    }

    order_sync = await sync_paper_orders(db)
    diagnostics = await heart_diagnostics(db, minutes=30)
    summary = await base.paper_summary(db)
    await db.execute(text("""
        INSERT INTO paper_equity_curve (cash_balance, unrealized_pnl, equity, open_positions)
        VALUES (:cash, :unrealized, :equity, :open_positions)
    """), {
        "cash": summary["cash_balance"],
        "unrealized": summary["unrealized_pnl"],
        "equity": summary["equity"],
        "open_positions": len(summary["open_positions"]),
    })
    await db.commit()
    return {
        "execution_version": EXECUTION_VERSION,
        "regime_router": regime,
        "loss_brake": loss_brake,
        "thesis_reconciliation": thesis_reconciliation,
        "heart_diagnostics": diagnostics,
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
