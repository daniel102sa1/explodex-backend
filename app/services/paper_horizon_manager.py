from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.binance import binance_client

VERSION = "paper_horizon_manager_v2_stop_survival"
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


def evaluate_survival_candle(
    *,
    side: str,
    high: float,
    low: float,
    close: float,
    hard_stop: float,
    soft_stop: float,
    target: float,
    survival_enabled: bool,
) -> tuple[float | None, str | None]:
    """Evaluate one completed candle using the pre-entry stop plan.

    Hard stop is always absolute. With survival enabled, merely wicking through
    the soft invalidation is tolerated; the candle must close beyond it to exit.
    This function intentionally never moves either stop.
    """
    side = str(side or "").upper()
    if side not in {"LONG", "SHORT"}:
        return None, None

    hard_hit = low <= hard_stop if side == "LONG" else high >= hard_stop
    target_hit = high >= target if side == "LONG" else low <= target
    soft_close_invalid = close <= soft_stop if side == "LONG" else close >= soft_stop

    # If hard stop and target both occur inside the same candle, sequence is
    # unknowable from OHLC alone, so remain conservative and record stop.
    if hard_hit and target_hit:
        return hard_stop, "AMBIGUOUS_HARD_STOP"
    if hard_hit:
        return hard_stop, "HARD_STOP"
    if target_hit:
        return target, "TP1"

    if survival_enabled:
        if soft_close_invalid:
            return close, "STRUCTURAL_CLOSE_INVALIDATION"
        return None, None

    normal_hit = low <= soft_stop if side == "LONG" else high >= soft_stop
    if normal_hit:
        return soft_stop, "STOP"
    return None, None


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

        survival = _d(metadata.get("stop_survival"))
        survival_enabled = bool(metadata.get("stop_survival_enabled")) and bool(survival.get("enabled"))
        confirmation_minutes = int(survival.get("confirmation_minutes") or 5)

        # For a soft-stop close confirmation we need completed candles near the
        # requested confirmation interval. Swing plans use 15m; tactical uses 5m.
        if survival_enabled:
            interval = "15m" if confirmation_minutes >= 15 else "5m"
            interval_minutes = 15 if interval == "15m" else 5
        else:
            interval = "1m" if age_minutes <= 480 else "5m" if age_minutes <= 1440 else "15m"
            interval_minutes = 1 if interval == "1m" else 5 if interval == "5m" else 15

        limit = min(1000, max(50, int(age_minutes / interval_minutes) + 10))
        try:
            klines = await binance_client.klines(row["symbol"], interval=interval, limit=limit)
        except Exception:
            klines = []

        start_ms = int(opened_at.timestamp() * 1000)
        future = [k for k in klines if len(k) >= 5 and int(k[0]) >= start_ms]
        exit_price = None
        exit_reason = None
        hard_stop = _f(row.get("stop_loss"))
        soft_stop = _f(metadata.get("soft_invalidation_stop"), hard_stop)
        tp_value = _f(row.get("take_profit"))

        for candle in future:
            high, low, close = _f(candle[2]), _f(candle[3]), _f(candle[4])
            exit_price, exit_reason = evaluate_survival_candle(
                side=str(row.get("side") or ""),
                high=high,
                low=low,
                close=close,
                hard_stop=hard_stop,
                soft_stop=soft_stop,
                target=tp_value,
                survival_enabled=survival_enabled,
            )
            if exit_price is not None:
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
            "stop_survival_enabled": survival_enabled,
            "soft_invalidation_stop": soft_stop,
            "hard_stop": hard_stop,
            "confirmation_minutes": confirmation_minutes if survival_enabled else None,
        })

    await db.commit()
    return {"version": VERSION, "closed": closed, "actions": actions}
