from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client


def _meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


async def manage_trade_time_stops(db: AsyncSession) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT t.id::text trade_id, t.signal_id::text signal_id, sy.symbol,
                   t.direction, t.entry_price, t.quantity, t.notional_usdt,
                   t.opened_at, t.metadata, s.reason,
                   s.expected_duration_max_minutes
            FROM trades t
            JOIN symbols sy ON sy.id = t.symbol_id
            LEFT JOIN signals s ON s.id = t.signal_id
            WHERE t.mode = 'PAPER' AND t.status IN ('OPEN','PARTIAL')
            ORDER BY t.opened_at ASC
            """
        )
    )
    trades = [dict(row) for row in result.mappings().all()]
    actions: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for trade in trades:
        metadata = _meta(trade.get("metadata"))
        reason = _meta(trade.get("reason"))
        prediction = _meta(reason.get("prediction"))
        time_stop = int(prediction.get("time_stop_minutes") or metadata.get("time_stop_minutes") or 45)
        max_duration = int(
            prediction.get("expected_duration_max_minutes")
            or trade.get("expected_duration_max_minutes")
            or metadata.get("estimated_duration_max_minutes")
            or 360
        )
        opened_at = trade.get("opened_at")
        if not opened_at:
            continue
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        elapsed = (now - opened_at).total_seconds() / 60
        if elapsed < min(time_stop, max_duration):
            continue

        try:
            price_payload = await binance_client.price(trade["symbol"])
            live_price = float(price_payload["price"])
        except Exception as exc:
            actions.append({"symbol": trade["symbol"], "action": "PRICE_ERROR", "detail": str(exc)[:200]})
            continue

        entry = float(trade["entry_price"])
        qty = float(trade["quantity"])
        direction = str(trade["direction"])
        initial_risk = float(metadata.get("initial_risk_usdt") or 0)
        unrealized = (live_price - entry) * qty if direction == "LONG" else (entry - live_price) * qty
        current_r = unrealized / initial_risk if initial_risk > 0 else 0.0

        close_reason: str | None = None
        # At the early time stop we only close when the trade has failed to achieve
        # meaningful follow-through. Positive >=0.5R trades are allowed to continue.
        if elapsed >= max_duration:
            close_reason = "MAX_DURATION"
        elif elapsed >= time_stop and current_r < 0.5 and not bool(metadata.get("tp1_hit")):
            close_reason = "TIME_STOP"

        if not close_reason:
            continue

        # Time exits use the current observed price and include the same conservative
        # fee estimate used by the paper manager.
        fee_rate = 0.0005
        gross = unrealized
        fees = (entry * qty + live_price * qty) * fee_rate
        pnl = gross - fees
        r_multiple = pnl / initial_risk if initial_risk > 0 else None
        notional = float(trade["notional_usdt"] or 0)

        await db.execute(
            text(
                """
                UPDATE trades
                SET status='CLOSED', closed_at=NOW(), exit_price=:exit_price,
                    pnl_usdt=:pnl, pnl_pct=:pnl_pct, r_multiple=:r_multiple,
                    fees_usdt=:fees, close_reason=:reason
                WHERE id=:trade_id AND status IN ('OPEN','PARTIAL')
                """
            ),
            {
                "exit_price": live_price,
                "pnl": pnl,
                "pnl_pct": (pnl / notional) * 100 if notional else 0,
                "r_multiple": r_multiple,
                "fees": fees,
                "reason": close_reason,
                "trade_id": trade["trade_id"],
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO trade_events (trade_id,event_type,price,message,data)
                VALUES (:trade_id,:event_type,:price,:message,CAST(:data AS JSONB))
                """
            ),
            {
                "trade_id": trade["trade_id"],
                "event_type": close_reason,
                "price": live_price,
                "message": "Salida por falta de seguimiento" if close_reason == "TIME_STOP" else "Salida por duración máxima del setup",
                "data": json.dumps({"elapsed_minutes": round(elapsed, 1), "r_multiple": r_multiple, "pnl_usdt": pnl}),
            },
        )
        if trade.get("signal_id"):
            await db.execute(
                text("UPDATE signals SET state='EXIT', is_active=FALSE, updated_at=NOW() WHERE id=:signal_id"),
                {"signal_id": trade["signal_id"]},
            )
        await db.execute(
            text(
                """
                INSERT INTO alerts(signal_id,trade_id,channel,severity,title,message,is_sent)
                VALUES (:signal_id,:trade_id,'APP','EXIT',:title,:message,FALSE)
                """
            ),
            {
                "signal_id": trade.get("signal_id"),
                "trade_id": trade["trade_id"],
                "title": f"{close_reason.replace('_',' ')} {trade['symbol']}",
                "message": f"Salida PAPER a {live_price:.10g} | {elapsed:.0f} min | R {current_r:.2f}",
            },
        )
        actions.append({"symbol": trade["symbol"], "action": close_reason, "elapsed_minutes": round(elapsed, 1), "r": round(current_r, 3)})

    await db.commit()
    return {"managed": len(trades), "actions": actions}
