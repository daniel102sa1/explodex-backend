from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.paper_adaptive_risk import (
    adaptive_leverage,
    market_direction_guard,
    symbol_adaptive_risk,
)
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


def _valid_geometry(side: str, entry: float, stop: float, tp: float) -> bool:
    if side == "LONG":
        return stop < entry < tp
    if side == "SHORT":
        return tp < entry < stop
    return False


def _scale_sizing(sizing: dict[str, Any], multiplier: float) -> dict[str, Any]:
    multiplier = max(0.0, min(1.25, float(multiplier)))
    if 0.999 <= multiplier <= 1.001:
        return dict(sizing)
    out = dict(sizing)
    for key in ("quantity", "notional", "margin", "risk_usdt"):
        if key in out:
            out[key] = round(base._f(out.get(key)) * multiplier, 10)
    return out


async def open_new_positions_live_fill(
    db: AsyncSession,
    *,
    risk_multiplier: float = 1.0,
    regime: dict[str, Any] | None = None,
    defensive: bool = False,
) -> dict[str, Any]:
    """Open PAPER trend positions at observable prices with adaptive risk controls."""
    regime = regime or {}
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one())
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if slots <= 0:
        return {
            "opened": 0,
            "stale_skipped": 0,
            "direction_blocked": 0,
            "risk_multiplier": risk_multiplier,
        }

    candidates = (await db.execute(text("""
        SELECT vo.signal_id::text, vo.symbol, vo.observed_at, vo.direction, vo.entry_price AS signal_entry_price,
               vo.stop_loss, vo.tp1, vo.trade_class, vo.grade, vo.fingerprint_score,
               vo.catalyst_state, vo.master_state
        FROM validation_observations vo
        LEFT JOIN paper_positions pp ON pp.signal_id=vo.signal_id
        WHERE pp.signal_id IS NULL
          AND vo.trade_class='TRADE_NOW'
          AND COALESCE(vo.master_state,'YES')='YES'
          AND vo.observed_at >= NOW() - INTERVAL '20 minutes'
        ORDER BY vo.observed_at ASC
        LIMIT :slots
    """), {"slots": slots})).mappings().all()

    opened = 0
    stale_skipped = 0
    anti_loss_skipped = 0
    direction_blocked = 0
    adaptive_errors = 0

    for raw in candidates:
        row = dict(raw)
        side = str(row.get("direction") or "").upper()
        original_stop = base._f(row.get("stop_loss"))
        original_tp = base._f(row.get("tp1"))
        signal_entry = base._f(row.get("signal_entry_price"))
        fingerprint_score = base._f(row.get("fingerprint_score"))
        fill = await base._latest_price(row["symbol"])
        if fill <= 0 or original_stop <= 0 or original_tp <= 0 or not _valid_geometry(side, fill, original_stop, original_tp):
            stale_skipped += 1
            continue

        direction_guard = market_direction_guard(side, regime)
        if not bool(direction_guard.get("allowed")):
            direction_blocked += 1
            continue

        try:
            adaptive = await symbol_adaptive_risk(
                row["symbol"],
                side=side,
                entry=fill,
                original_stop=original_stop,
                original_tp=original_tp,
                fingerprint_score=fingerprint_score,
            )
        except Exception:
            adaptive_errors += 1
            adaptive = {
                "version": "paper_adaptive_risk_fallback",
                "stop": original_stop,
                "tp": original_tp,
                "rr": abs(original_tp - fill) / max(abs(fill - original_stop), 1e-12),
                "stop_widened": False,
                "atr": 0.0,
            }

        stop = base._f(adaptive.get("stop"), original_stop)
        tp = base._f(adaptive.get("tp"), original_tp)
        if not _valid_geometry(side, fill, stop, tp):
            stale_skipped += 1
            continue

        aligned = str(direction_guard.get("reason") or "").startswith("aligned_with_")
        leverage = adaptive_leverage(
            grade=row.get("grade"),
            fingerprint_score=fingerprint_score,
            catalyst_state=row.get("catalyst_state"),
            regime_aligned=aligned,
            defensive=defensive,
        )
        sizing = base.size_position(balance, fill, stop, leverage)
        combined_risk_multiplier = risk_multiplier * float(direction_guard.get("risk_multiplier") or 1.0)
        sizing = _scale_sizing(sizing, combined_risk_multiplier)
        if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
            anti_loss_skipped += 1
            continue

        opened_at = datetime.now(timezone.utc)
        metadata = json.dumps({
            "execution_version": "paper_execution_v2_multi_strategy_v6_adaptive_risk",
            "strategy_mode": "TREND_PREMOVE",
            "signal_observed_at": row["observed_at"].isoformat() if row.get("observed_at") else None,
            "signal_entry_price": signal_entry,
            "simulated_fill_price": fill,
            "uses_current_observable_price": True,
            "portfolio_loss_risk_multiplier": risk_multiplier,
            "market_direction_guard": direction_guard,
            "adaptive_risk": adaptive,
            "adaptive_leverage": leverage,
            "combined_risk_multiplier": combined_risk_multiplier,
            "original_stop_loss": original_stop,
            "original_take_profit": original_tp,
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
            opened += 1

    await db.commit()
    return {
        "opened": opened,
        "stale_skipped": stale_skipped,
        "anti_loss_skipped": anti_loss_skipped,
        "direction_blocked": direction_blocked,
        "adaptive_errors": adaptive_errors,
        "risk_multiplier": risk_multiplier,
    }


async def run_paper_cycle_v2(db: AsyncSession) -> dict[str, Any]:
    await base.ensure_paper_schema(db)

    closed = await base._close_due_positions(db)
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

    range_scan = await scan_all_eligible_ranges(db)
    range_opened = (
        await open_range_positions(db)
        if range_enabled
        else {
            "opened": 0,
            "skipped": 0,
            "regime_blocked": not range_enabled_by_regime,
            "loss_brake_blocked": defensive and not secondary_entries_enabled,
        }
    )

    micro_scan = await scan_micro_scalps(db)
    advanced_prefilter = await prefilter_new_micro_signals(db)
    micro_opened = (
        await open_micro_positions(db)
        if micro_enabled
        else {
            "opened": 0,
            "skipped": 0,
            "regime_blocked": not micro_enabled_by_regime,
            "loss_brake_blocked": defensive and not secondary_entries_enabled,
        }
    )

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
        "execution_version": "paper_execution_v2_multi_strategy_v6_adaptive_risk",
        "regime_router": regime,
        "loss_brake": loss_brake,
        "trend": {
            **closed,
            **trend_opened,
        },
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
