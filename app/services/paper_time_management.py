from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client
from app.services.paper_trading import manage_open_paper_trades


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def _close_by_time(
    db: AsyncSession,
    trade: dict[str, Any],
    live_price: float,
    reason: str,
    *,
    fee_rate: float = 0.0005,
) -> dict[str, Any]:
    direction = str(trade["direction"])
    entry = _f(trade["entry_price"])
    quantity = _f(trade["quantity"])
    notional = _f(trade["notional_usdt"])
    metadata = _as_dict(trade.get("metadata"))
    initial_risk = _f(metadata.get("initial_risk_usdt"))

    gross_pnl = (live_price - entry) * quantity if direction == "LONG" else (entry - live_price) * quantity
    fees = (entry * quantity + live_price * quantity) * fee_rate
    pnl = gross_pnl - fees
    r_multiple = pnl / initial_risk if initial_risk > 0 else None
    pnl_pct = (pnl / notional) * 100 if notional > 0 else 0.0

    await db.execute(
        text(
            """
            UPDATE trades
            SET status = 'CLOSED', closed_at = NOW(), exit_price = :exit_price,
                pnl_usdt = :pnl, pnl_pct = :pnl_pct, r_multiple = :r_multiple,
                fees_usdt = :fees, close_reason = :reason
            WHERE id = :trade_id
            """
        ),
        {
            "exit_price": live_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "r_multiple": r_multiple,
            "fees": fees,
            "reason": reason,
            "trade_id": trade["trade_id"],
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO trade_events (trade_id, event_type, price, message, data)
            VALUES (:trade_id, 'TIME_EXIT', :price, :message, CAST(:data AS JSONB))
            """
        ),
        {
            "trade_id": trade["trade_id"],
            "price": live_price,
            "message": "Paper exit by maximum duration" if reason == "TIME_MAX_DURATION" else "Paper exit because expected impulse did not follow through",
            "data": json.dumps({
                "reason": reason,
                "pnl_usdt": pnl,
                "r_multiple": r_multiple,
            }),
        },
    )
    await db.execute(
        text("UPDATE signals SET state = 'EXIT', is_active = FALSE, updated_at = NOW() WHERE id = :signal_id"),
        {"signal_id": trade["signal_id"]},
    )
    await db.execute(
        text(
            """
            INSERT INTO alerts (signal_id, trade_id, channel, severity, title, message, is_sent)
            VALUES (:signal_id, :trade_id, 'APP', 'EXIT', :title, :message, FALSE)
            """
        ),
        {
            "signal_id": trade["signal_id"],
            "trade_id": trade["trade_id"],
            "title": f"SALIDA POR TIEMPO {trade['symbol']}",
            "message": f"{reason} | precio {live_price:.12g} | PnL {pnl:.2f} USDT"
            + (f" | R {r_multiple:.2f}" if r_multiple is not None else ""),
        },
    )
    return {
        "symbol": trade["symbol"],
        "action": reason,
        "price": live_price,
        "pnl_usdt": round(pnl, 4),
        "r_multiple": round(r_multiple, 4) if r_multiple is not None else None,
    }


async def manage_open_paper_trades_with_time(db: AsyncSession) -> dict[str, Any]:
    """Run normal SL/TP management, then enforce paper-only time exits.

    A time stop is not assumed profitable; it is deliberately logged so we can
    compare it against a no-time-stop policy after enough paper trades.
    """
    base = await manage_open_paper_trades(db)

    result = await db.execute(
        text(
            """
            SELECT t.id::text AS trade_id, t.signal_id::text AS signal_id,
                   sy.symbol, t.direction, t.status, t.entry_price, t.quantity,
                   t.notional_usdt, t.stop_loss, t.tp1, t.tp2, t.tp3,
                   t.opened_at, t.metadata, s.reason AS signal_reason
            FROM trades t
            JOIN symbols sy ON sy.id = t.symbol_id
            JOIN signals s ON s.id = t.signal_id
            WHERE t.mode = 'PAPER' AND t.status IN ('OPEN','PARTIAL')
            ORDER BY t.opened_at ASC
            """
        )
    )
    trades = [dict(row) for row in result.mappings().all()]
    if not trades:
        return {**base, "time_management": []}

    semaphore = asyncio.Semaphore(5)

    async def fetch_price(trade: dict[str, Any]):
        async with semaphore:
            try:
                payload = await binance_client.price(trade["symbol"])
                return trade, _f(payload.get("price")), None
            except Exception as exc:
                return trade, 0.0, str(exc)[:250]

    priced = await asyncio.gather(*(fetch_price(t) for t in trades))
    now = datetime.now(timezone.utc)
    time_actions: list[dict[str, Any]] = []

    for trade, live_price, error in priced:
        if error or live_price <= 0:
            continue

        opened_at = trade.get("opened_at")
        if not isinstance(opened_at, datetime):
            continue
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        elapsed_minutes = (now - opened_at).total_seconds() / 60

        metadata = _as_dict(trade.get("metadata"))
        signal_reason = _as_dict(trade.get("signal_reason"))
        prediction = _as_dict(signal_reason.get("prediction"))

        estimated_max = int(
            _f(
                prediction.get("expected_duration_max_minutes"),
                _f(metadata.get("estimated_duration_max_minutes"), 360),
            )
        )
        estimated_min = int(
            _f(
                prediction.get("expected_duration_min_minutes"),
                _f(metadata.get("estimated_duration_min_minutes"), 30),
            )
        )
        time_stop = int(_f(prediction.get("time_stop_minutes"), max(30, min(45, estimated_min))))
        estimated_max = max(time_stop + 5, min(estimated_max, 720))

        entry = _f(trade["entry_price"])
        original_stop = _f(metadata.get("original_stop"), _f(trade["stop_loss"]))
        one_r_price = max(abs(entry - original_stop), entry * 0.001)
        directional_progress = live_price - entry if trade["direction"] == "LONG" else entry - live_price
        progress_r = directional_progress / one_r_price if one_r_price > 0 else 0.0
        tp1_hit = bool(metadata.get("tp1_hit"))

        close_reason: str | None = None
        if elapsed_minutes >= estimated_max:
            close_reason = "TIME_MAX_DURATION"
        elif not tp1_hit and elapsed_minutes >= time_stop and progress_r < 0.50:
            close_reason = "TIME_NO_FOLLOWTHROUGH"

        if close_reason:
            action = await _close_by_time(db, trade, live_price, close_reason)
            action.update({
                "elapsed_minutes": round(elapsed_minutes, 1),
                "progress_r_before_exit": round(progress_r, 3),
                "time_stop_minutes": time_stop,
                "max_duration_minutes": estimated_max,
            })
            time_actions.append(action)

    if time_actions:
        await db.commit()

    return {**base, "time_management": time_actions}
