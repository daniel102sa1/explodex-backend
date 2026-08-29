from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
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
    multiplier = max(0.0, min(1.0, float(multiplier)))
    if multiplier >= 0.999:
        return dict(sizing)
    out = dict(sizing)
    for key in ("quantity", "notional", "margin", "risk_usdt"):
        if key in out:
            out[key] = round(base._f(out.get(key)) * multiplier, 10)
    return out


async def open_new_positions_live_fill(db: AsyncSession, *, risk_multiplier: float = 1.0) -> dict[str, Any]:
    """Open PAPER trend positions only at the price observable when this cycle executes."""
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one())
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if slots <= 0:
        return {"opened": 0, "stale_skipped": 0, "risk_multiplier": risk_multiplier}

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
    for raw in candidates:
        row = dict(raw)
        side = str(row.get("direction") or "").upper()
        stop = base._f(row.get("stop_loss"))
        tp = base._f(row.get("tp1"))
        signal_entry = base._f(row.get("signal_entry_price"))
        fill = await base._latest_price(row["symbol"])
        if fill <= 0 or stop <= 0 or tp <= 0 or not _valid_geometry(side, fill, stop, tp):
            stale_skipped += 1
            continue

        leverage = base.choose_leverage(row.get("grade"), base._f(row.get("fingerprint_score")), row.get("catalyst_state"))
        sizing = base.size_position(balance, fill, stop, leverage)
        sizing = _scale_sizing(sizing, risk_multiplier)
        if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
            anti_loss_skipped += 1
            continue

        opened_at = datetime.now(timezone.utc)
        metadata = json.dumps({
            "execution_version": "paper_execution_v2",
            "strategy_mode": "TREND_PREMOVE",
            "signal_observed_at": row["observed_at"].isoformat() if row.get("observed_at") else None,
            "signal_entry_price": signal_entry,
            "simulated_fill_price": fill,
            "uses_current_observable_price": True,
            "portfolio_loss_risk_multiplier": risk_multiplier,
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
            "grade": row.get("grade"), "fingerprint_score": base._f(row.get("fingerprint_score")),
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

    range_enabled = range_enabled_by_regime and secondary_entries_enabled
    micro_enabled = micro_enabled_by_regime and secondary_entries_enabled

    # Validated TREND/PRE-MOVE remains the highest-priority path. The loss brake can
    # only reduce PAPER sizing; it never creates or promotes a signal.
    trend_opened = await open_new_positions_live_fill(db, risk_multiplier=trend_risk_multiplier)

    # Keep scanning even when opening is disabled so the laboratory retains evidence
    # of what would have been available during defensive periods.
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
        "execution_version": "paper_execution_v2_multi_strategy_v5_anti_loss",
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
