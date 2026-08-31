from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.binance import binance_client

VERSION = "paper_horizon_manager_v1"
DEFAULT_MAX_HOLD_MINUTES = 120


def _d(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def close_due_positions(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT id, symbol, side, entry_price, stop_loss, take_profit, quantity,
               notional, opened_at, metadata
        FROM paper_positions
        WHERE status='OPEN'
        ORDER BY opened_at ASC
    """))).mappings().all()

    closed = 0
    actions: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        metadata = _d(row.get("metadata"))
        now = datetime.now(timezone.utc)
        opened_at = row.get("opened_at")
        if not opened_at:
            continue
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)

        max_hold = int(metadata.get("max_hold_minutes") or DEFAULT_MAX_HOLD_MINUTES)
        max_hold = max(30, min(max_hold, 4320))
        age_minutes = (now - opened_at).total_seconds() / 60.0

        # For long-horizon positions we do not need to replay every 1m candle for
        # two days. Pull enough recent candles to detect stop/TP since entry when
        # feasible; otherwise use a coarser interval for the older segment.
        interval = "1m" if age_minutes <= 480 else "5m" if age_minutes <= 1440 else "15m"
        limit = min(1000, max(50, int(age_minutes / (1 if interval == "1m" else 5 if interval == "5m" else 15)) + 10))
        try:
            klines = await binance_client.klines(row["symbol"], interval=interval, limit=limit)
        except Exception:
            klines = []

        start_ms = int(opened_at.timestamp() * 1000)
        future = [k for k in klines if len(k) >= 5 and int(k[0]) >= start_ms]
        exit_price = None
        exit_reason = None
        stop_value = _f(row.get("stop_loss"))
        tp_value = _f(row.get("take_profit"))

        for candle in future:
            high, low = _f(candle[2]), _f(candle[3])
            side = str(row.get("side") or "").upper()
            stop_hit = low <= stop_value if side == "LONG" else high >= stop_value
            tp_hit = high >= tp_value if side == "LONG" else low <= tp_value
            if stop_hit and tp_hit:
                exit_price, exit_reason = stop_value, "AMBIGUOUS_STOP"
                break
            if stop_hit:
                exit_price, exit_reason = stop_value, "STOP"
                break
            if tp_hit:
                exit_price, exit_reason = tp_value, "TP1"
                break

        if exit_price is None and age_minutes >= max_hold:
            if future:
                exit_price = _f(future[-1][4])
            else:
                exit_price = await base._latest_price(row["symbol"])
            exit_reason = "TIME_EXIT"

        if exit_price is None or exit_price <= 0:
            continue

        pnl = base.calculate_trade_pnl(
            side=str(row["side"]),
            entry=_f(row["entry_price"]),
            exit_price=exit_price,
            quantity=_f(row["quantity"]),
            notional=_f(row["notional"]),
            opened_at=opened_at,
            closed_at=now,
        )
        await db.execute(text("""
            UPDATE paper_positions
            SET status='CLOSED', closed_at=:closed_at, exit_price=:exit_price,
                exit_reason=:exit_reason, gross_pnl=:gross_pnl, net_pnl=:net_pnl,
                fees=:fees, slippage=:slippage, funding_estimate=:funding_estimate
            WHERE id=:id
        """), {
            "id": row["id"],
            "closed_at": now,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            **pnl,
        })
        await db.execute(text("""
            UPDATE paper_accounts
            SET cash_balance=cash_balance+:net_pnl,
                realized_pnl=realized_pnl+:net_pnl,
                total_fees=total_fees+:fees+:slippage+:funding_estimate,
                updated_at=NOW()
            WHERE id=1
        """), pnl)
        closed += 1
        actions.append({
            "symbol": row["symbol"],
            "reason": exit_reason,
            "age_minutes": round(age_minutes, 1),
            "max_hold_minutes": max_hold,
            "strategy_mode": metadata.get("strategy_mode"),
        })

    await db.commit()
    return {"version": VERSION, "closed": closed, "actions": actions}
