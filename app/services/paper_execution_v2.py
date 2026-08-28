from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.paper_orders import sync_paper_orders


def _valid_geometry(side: str, entry: float, stop: float, tp: float) -> bool:
    if side == "LONG":
        return stop < entry < tp
    if side == "SHORT":
        return tp < entry < stop
    return False


async def open_new_positions_live_fill(db: AsyncSession) -> dict[str, int]:
    """Open PAPER positions only at the price observable when this cycle executes."""
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one())
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if slots <= 0:
        return {"opened": 0, "stale_skipped": 0}

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
        if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
            continue

        opened_at = datetime.now(timezone.utc)
        metadata = json.dumps({
            "execution_version": "paper_execution_v2",
            "signal_observed_at": row["observed_at"].isoformat() if row.get("observed_at") else None,
            "signal_entry_price": signal_entry,
            "simulated_fill_price": fill,
            "uses_current_observable_price": True,
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
    return {"opened": opened, "stale_skipped": stale_skipped}


async def run_paper_cycle_v2(db: AsyncSession) -> dict[str, Any]:
    await base.ensure_paper_schema(db)
    closed = await base._close_due_positions(db)
    opened = await open_new_positions_live_fill(db)
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
        "execution_version": "paper_execution_v2",
        **closed,
        **opened,
        "orders": order_sync,
        "equity": summary["equity"],
    }
