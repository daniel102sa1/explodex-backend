from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.binance import binance_client


DEFAULT_SCANNER_SETTINGS = {
    "min_setup_score": 80.0,
    "max_risk_score": 35.0,
    "max_open_trades": 2,
    "max_daily_loss_pct": 3.0,
    "risk_per_trade_pct": 0.5,
}

DEFAULT_PAPER_ACCOUNT = {
    "starting_equity_usdt": 1000.0,
    "max_leverage": 3.0,
    "estimated_fee_rate": 0.0005,
    "target_policy": "TP2_FULL",
}


def _json_value(value: Any, default: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return {**default, **value}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return {**default, **parsed}
        except json.JSONDecodeError:
            pass
    return dict(default)


async def _get_setting(
    db: AsyncSession,
    key: str,
    default: dict[str, Any],
) -> dict[str, Any]:
    result = await db.execute(
        text("SELECT value FROM system_settings WHERE key = :key"),
        {"key": key},
    )
    value = result.scalar_one_or_none()
    return _json_value(value, default)


async def _paper_equity(db: AsyncSession, starting_equity: float) -> tuple[float, float]:
    result = await db.execute(
        text(
            """
            SELECT COALESCE(SUM(pnl_usdt), 0) AS total_pnl,
                   COALESCE(SUM(CASE WHEN closed_at >= date_trunc('day', NOW()) THEN pnl_usdt ELSE 0 END), 0) AS daily_pnl
            FROM trades
            WHERE mode = 'PAPER' AND status IN ('CLOSED', 'STOPPED')
            """
        )
    )
    row = result.mappings().one()
    total_pnl = float(row["total_pnl"] or 0)
    daily_pnl = float(row["daily_pnl"] or 0)
    return starting_equity + total_pnl, daily_pnl


async def sync_ready_signals(db: AsyncSession) -> dict[str, Any]:
    """Open PAPER trades only from fresh READY signals that still trade inside the entry zone."""
    if not settings.paper_trading_only:
        return {"opened": 0, "skipped": ["paper_trading_only is disabled"]}

    scanner_cfg = await _get_setting(db, "scanner", DEFAULT_SCANNER_SETTINGS)
    paper_cfg = await _get_setting(db, "paper_account", DEFAULT_PAPER_ACCOUNT)

    starting_equity = float(paper_cfg["starting_equity_usdt"])
    equity, daily_pnl = await _paper_equity(db, starting_equity)
    max_daily_loss = equity * float(scanner_cfg["max_daily_loss_pct"]) / 100

    if daily_pnl <= -max_daily_loss:
        return {
            "opened": 0,
            "blocked": True,
            "reason": "daily_loss_limit",
            "equity_usdt": round(equity, 4),
            "daily_pnl_usdt": round(daily_pnl, 4),
        }

    open_result = await db.execute(
        text("SELECT COUNT(*) FROM trades WHERE mode = 'PAPER' AND status IN ('OPEN','PARTIAL')")
    )
    open_count = int(open_result.scalar_one() or 0)
    max_open = int(scanner_cfg["max_open_trades"])
    available_slots = max(0, max_open - open_count)
    if available_slots == 0:
        return {"opened": 0, "reason": "max_open_trades", "open_trades": open_count}

    result = await db.execute(
        text(
            """
            SELECT s.id::text AS signal_id, s.symbol_id::text AS symbol_id, sy.symbol,
                   s.direction, s.setup_score, s.risk_score, s.current_price,
                   s.entry_low, s.entry_high, s.stop_loss, s.tp1, s.tp2, s.tp3,
                   s.expected_duration_min_minutes, s.expected_duration_max_minutes
            FROM signals s
            JOIN symbols sy ON sy.id = s.symbol_id
            WHERE s.is_active = TRUE
              AND s.state = 'READY'
              AND s.setup_score >= :min_setup
              AND s.risk_score <= :max_risk
              AND s.created_at >= NOW() - INTERVAL '2 hours'
              AND NOT EXISTS (
                  SELECT 1 FROM trades t
                  WHERE t.signal_id = s.id AND t.mode = 'PAPER'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM trades t2
                  WHERE t2.symbol_id = s.symbol_id
                    AND t2.mode = 'PAPER'
                    AND t2.status IN ('OPEN','PARTIAL')
              )
            ORDER BY s.setup_score DESC, s.risk_score ASC, s.created_at DESC
            LIMIT 20
            """
        ),
        {
            "min_setup": float(scanner_cfg["min_setup_score"]),
            "max_risk": float(scanner_cfg["max_risk_score"]),
        },
    )
    candidates = [dict(row) for row in result.mappings().all()]

    semaphore = asyncio.Semaphore(5)

    async def fetch_price(candidate: dict[str, Any]) -> tuple[dict[str, Any], float | None, str | None]:
        async with semaphore:
            try:
                payload = await binance_client.price(candidate["symbol"])
                return candidate, float(payload["price"]), None
            except Exception as exc:
                return candidate, None, str(exc)[:250]

    priced = await asyncio.gather(*(fetch_price(c) for c in candidates))

    opened: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for candidate, live_price, error in priced:
        if len(opened) >= available_slots:
            break
        if error or live_price is None:
            skipped.append({"symbol": candidate["symbol"], "reason": "price_error", "detail": error})
            continue

        entry_low = float(candidate["entry_low"])
        entry_high = float(candidate["entry_high"])
        stop_loss = float(candidate["stop_loss"])
        direction = candidate["direction"]

        # Important anti-chase rule: if price already left the planned entry area, do not follow it.
        if not (entry_low <= live_price <= entry_high):
            skipped.append(
                {
                    "symbol": candidate["symbol"],
                    "reason": "outside_entry_zone",
                    "live_price": live_price,
                    "entry_low": entry_low,
                    "entry_high": entry_high,
                }
            )
            continue

        if direction == "LONG" and stop_loss >= live_price:
            skipped.append({"symbol": candidate["symbol"], "reason": "invalid_long_stop"})
            continue
        if direction == "SHORT" and stop_loss <= live_price:
            skipped.append({"symbol": candidate["symbol"], "reason": "invalid_short_stop"})
            continue

        risk_fraction = abs(live_price - stop_loss) / live_price
        if risk_fraction <= 0:
            skipped.append({"symbol": candidate["symbol"], "reason": "zero_risk_distance"})
            continue

        risk_pct = float(scanner_cfg["risk_per_trade_pct"])
        risk_usdt = equity * risk_pct / 100
        max_leverage = float(paper_cfg["max_leverage"])
        raw_notional = risk_usdt / risk_fraction
        notional = min(raw_notional, equity * max_leverage)
        quantity = notional / live_price
        effective_leverage = notional / equity if equity > 0 else 1.0

        trade_id = str(uuid.uuid4())
        metadata = {
            "engine_version": "paper-v1",
            "original_stop": stop_loss,
            "initial_risk_usdt": risk_usdt,
            "virtual_equity_at_entry": equity,
            "setup_score_at_entry": float(candidate["setup_score"]),
            "risk_score_at_entry": float(candidate["risk_score"]),
            "tp1_hit": False,
            "target_policy": paper_cfg["target_policy"],
            "estimated_duration_min_minutes": candidate["expected_duration_min_minutes"],
            "estimated_duration_max_minutes": candidate["expected_duration_max_minutes"],
        }

        await db.execute(
            text(
                """
                INSERT INTO trades (
                    id, signal_id, symbol_id, mode, direction, status,
                    leverage, risk_pct, entry_price, quantity, notional_usdt,
                    stop_loss, tp1, tp2, tp3, opened_at, fees_usdt, metadata
                ) VALUES (
                    :id, :signal_id, :symbol_id, 'PAPER', :direction, 'OPEN',
                    :leverage, :risk_pct, :entry_price, :quantity, :notional,
                    :stop_loss, :tp1, :tp2, :tp3, NOW(), 0, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": trade_id,
                "signal_id": candidate["signal_id"],
                "symbol_id": candidate["symbol_id"],
                "direction": direction,
                "leverage": effective_leverage,
                "risk_pct": risk_pct,
                "entry_price": live_price,
                "quantity": quantity,
                "notional": notional,
                "stop_loss": stop_loss,
                "tp1": float(candidate["tp1"]),
                "tp2": float(candidate["tp2"]),
                "tp3": float(candidate["tp3"]),
                "metadata": json.dumps(metadata),
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO trade_events (trade_id, event_type, price, setup_score, risk_score, message, data)
                VALUES (:trade_id, 'OPEN', :price, :setup_score, :risk_score,
                        'Paper trade opened from READY signal', CAST(:data AS JSONB))
                """
            ),
            {
                "trade_id": trade_id,
                "price": live_price,
                "setup_score": float(candidate["setup_score"]),
                "risk_score": float(candidate["risk_score"]),
                "data": json.dumps({"risk_usdt": risk_usdt, "notional_usdt": notional}),
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO alerts (signal_id, trade_id, channel, severity, title, message, is_sent)
                VALUES (:signal_id, :trade_id, 'APP', 'ENTRY', :title, :message, FALSE)
                """
            ),
            {
                "signal_id": candidate["signal_id"],
                "trade_id": trade_id,
                "title": f"PAPER {direction} {candidate['symbol']}",
                "message": f"Entrada {live_price:.12g} | SL {stop_loss:.12g} | TP2 {float(candidate['tp2']):.12g}",
            },
        )
        await db.execute(
            text("UPDATE signals SET state = 'ENTER', updated_at = NOW() WHERE id = :id"),
            {"id": candidate["signal_id"]},
        )

        opened.append(
            {
                "trade_id": trade_id,
                "symbol": candidate["symbol"],
                "direction": direction,
                "entry": live_price,
                "stop_loss": stop_loss,
                "tp1": float(candidate["tp1"]),
                "tp2": float(candidate["tp2"]),
                "tp3": float(candidate["tp3"]),
                "risk_usdt": round(risk_usdt, 4),
                "risk_pct": risk_pct,
                "notional_usdt": round(notional, 4),
                "effective_leverage": round(effective_leverage, 3),
            }
        )

    await db.commit()
    return {
        "opened": len(opened),
        "open_before": open_count,
        "equity_usdt": round(equity, 4),
        "daily_pnl_usdt": round(daily_pnl, 4),
        "trades": opened,
        "skipped": skipped[:20],
    }


def _hit(direction: str, price: float, level: float, kind: str) -> bool:
    if direction == "LONG":
        return price <= level if kind == "stop" else price >= level
    return price >= level if kind == "stop" else price <= level


async def manage_open_paper_trades(db: AsyncSession) -> dict[str, Any]:
    paper_cfg = await _get_setting(db, "paper_account", DEFAULT_PAPER_ACCOUNT)
    fee_rate = float(paper_cfg["estimated_fee_rate"])

    result = await db.execute(
        text(
            """
            SELECT t.id::text AS trade_id, t.signal_id::text AS signal_id,
                   sy.symbol, t.direction, t.status, t.entry_price, t.quantity,
                   t.notional_usdt, t.stop_loss, t.tp1, t.tp2, t.tp3,
                   t.metadata
            FROM trades t
            JOIN symbols sy ON sy.id = t.symbol_id
            WHERE t.mode = 'PAPER' AND t.status IN ('OPEN','PARTIAL')
            ORDER BY t.opened_at ASC
            """
        )
    )
    trades = [dict(row) for row in result.mappings().all()]

    semaphore = asyncio.Semaphore(5)

    async def fetch_price(trade: dict[str, Any]) -> tuple[dict[str, Any], float | None, str | None]:
        async with semaphore:
            try:
                payload = await binance_client.price(trade["symbol"])
                return trade, float(payload["price"]), None
            except Exception as exc:
                return trade, None, str(exc)[:250]

    priced = await asyncio.gather(*(fetch_price(t) for t in trades))
    actions: list[dict[str, Any]] = []

    for trade, live_price, error in priced:
        if error or live_price is None:
            actions.append({"symbol": trade["symbol"], "action": "ERROR", "detail": error})
            continue

        direction = trade["direction"]
        entry = float(trade["entry_price"])
        quantity = float(trade["quantity"])
        stop = float(trade["stop_loss"])
        tp1 = float(trade["tp1"])
        tp2 = float(trade["tp2"])
        metadata = _json_value(trade.get("metadata"), {})

        # Stop has priority in this polling-based v1 manager.
        if _hit(direction, live_price, stop, "stop"):
            # If price moved through the stop between checks, use the worse live price to avoid optimistic backtests.
            exit_price = min(stop, live_price) if direction == "LONG" else max(stop, live_price)
            gross_pnl = (exit_price - entry) * quantity if direction == "LONG" else (entry - exit_price) * quantity
            fees = (entry * quantity + exit_price * quantity) * fee_rate
            pnl = gross_pnl - fees
            initial_risk = float(metadata.get("initial_risk_usdt", 0) or 0)
            r_multiple = pnl / initial_risk if initial_risk > 0 else None
            tp1_hit = bool(metadata.get("tp1_hit"))
            final_status = "CLOSED" if tp1_hit and pnl >= 0 else "STOPPED"

            await db.execute(
                text(
                    """
                    UPDATE trades
                    SET status = :status, closed_at = NOW(), exit_price = :exit_price,
                        pnl_usdt = :pnl, pnl_pct = :pnl_pct, r_multiple = :r_multiple,
                        fees_usdt = :fees, close_reason = :reason
                    WHERE id = :trade_id
                    """
                ),
                {
                    "status": final_status,
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "pnl_pct": (pnl / float(trade["notional_usdt"])) * 100 if float(trade["notional_usdt"]) else 0,
                    "r_multiple": r_multiple,
                    "fees": fees,
                    "reason": "BREAKEVEN_STOP" if tp1_hit else "STOP_LOSS",
                    "trade_id": trade["trade_id"],
                },
            )
            await db.execute(
                text(
                    """
                    INSERT INTO trade_events (trade_id, event_type, price, message, data)
                    VALUES (:trade_id, 'STOP', :price, :message, CAST(:data AS JSONB))
                    """
                ),
                {
                    "trade_id": trade["trade_id"],
                    "price": exit_price,
                    "message": "Paper trade closed by protected stop" if tp1_hit else "Paper trade closed by stop loss",
                    "data": json.dumps({"pnl_usdt": pnl, "fees_usdt": fees, "r_multiple": r_multiple}),
                },
            )
            await db.execute(
                text(
                    """
                    UPDATE signals SET state = :state, is_active = FALSE, updated_at = NOW()
                    WHERE id = :signal_id
                    """
                ),
                {"state": "EXIT" if tp1_hit and pnl >= 0 else "INVALIDATED", "signal_id": trade["signal_id"]},
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
                    "title": f"SALIDA PAPER {trade['symbol']}",
                    "message": f"Stop ejecutado | PnL {pnl:.2f} USDT | R {r_multiple:.2f}" if r_multiple is not None else f"Stop ejecutado | PnL {pnl:.2f} USDT",
                },
            )
            actions.append({"symbol": trade["symbol"], "action": "STOP", "pnl_usdt": round(pnl, 4)})
            continue

        # At TP2, close the full position in v1. TP3 remains a research/reference target.
        if _hit(direction, live_price, tp2, "target"):
            exit_price = tp2
            gross_pnl = (exit_price - entry) * quantity if direction == "LONG" else (entry - exit_price) * quantity
            fees = (entry * quantity + exit_price * quantity) * fee_rate
            pnl = gross_pnl - fees
            initial_risk = float(metadata.get("initial_risk_usdt", 0) or 0)
            r_multiple = pnl / initial_risk if initial_risk > 0 else None

            await db.execute(
                text(
                    """
                    UPDATE trades
                    SET status = 'CLOSED', closed_at = NOW(), exit_price = :exit_price,
                        pnl_usdt = :pnl, pnl_pct = :pnl_pct, r_multiple = :r_multiple,
                        fees_usdt = :fees, close_reason = 'TP2'
                    WHERE id = :trade_id
                    """
                ),
                {
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "pnl_pct": (pnl / float(trade["notional_usdt"])) * 100 if float(trade["notional_usdt"]) else 0,
                    "r_multiple": r_multiple,
                    "fees": fees,
                    "trade_id": trade["trade_id"],
                },
            )
            await db.execute(
                text(
                    """
                    INSERT INTO trade_events (trade_id, event_type, price, message, data)
                    VALUES (:trade_id, 'TP2', :price, 'Paper trade reached TP2 and closed', CAST(:data AS JSONB))
                    """
                ),
                {
                    "trade_id": trade["trade_id"],
                    "price": exit_price,
                    "data": json.dumps({"pnl_usdt": pnl, "fees_usdt": fees, "r_multiple": r_multiple}),
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
                    "title": f"TP2 PAPER {trade['symbol']}",
                    "message": f"Objetivo alcanzado | PnL {pnl:.2f} USDT | R {r_multiple:.2f}" if r_multiple is not None else f"Objetivo alcanzado | PnL {pnl:.2f} USDT",
                },
            )
            actions.append({"symbol": trade["symbol"], "action": "TP2", "pnl_usdt": round(pnl, 4)})
            continue

        # Once TP1 is reached, protect at breakeven. We do not count it as a partial close in v1.
        if not bool(metadata.get("tp1_hit")) and _hit(direction, live_price, tp1, "target"):
            metadata["tp1_hit"] = True
            metadata["tp1_hit_at"] = datetime.now(timezone.utc).isoformat()
            metadata["stop_moved_to_breakeven"] = True
            await db.execute(
                text(
                    """
                    UPDATE trades
                    SET stop_loss = :entry, metadata = CAST(:metadata AS JSONB)
                    WHERE id = :trade_id
                    """
                ),
                {"entry": entry, "metadata": json.dumps(metadata), "trade_id": trade["trade_id"]},
            )
            await db.execute(
                text(
                    """
                    INSERT INTO trade_events (trade_id, event_type, price, message, data)
                    VALUES (:trade_id, 'TP1', :price,
                            'TP1 reached; stop moved to breakeven', CAST(:data AS JSONB))
                    """
                ),
                {
                    "trade_id": trade["trade_id"],
                    "price": tp1,
                    "data": json.dumps({"new_stop": entry}),
                },
            )
            await db.execute(
                text("UPDATE signals SET state = 'PROTECT', updated_at = NOW() WHERE id = :signal_id"),
                {"signal_id": trade["signal_id"]},
            )
            actions.append({"symbol": trade["symbol"], "action": "PROTECT", "new_stop": entry})
        else:
            await db.execute(
                text("UPDATE signals SET state = 'HOLD', updated_at = NOW() WHERE id = :signal_id AND state <> 'PROTECT'"),
                {"signal_id": trade["signal_id"]},
            )
            actions.append({"symbol": trade["symbol"], "action": "HOLD", "price": live_price})

    await db.commit()
    return {"managed": len(trades), "actions": actions}


async def paper_performance(db: AsyncSession) -> dict[str, Any]:
    paper_cfg = await _get_setting(db, "paper_account", DEFAULT_PAPER_ACCOUNT)
    starting_equity = float(paper_cfg["starting_equity_usdt"])

    result = await db.execute(
        text(
            """
            SELECT pnl_usdt, r_multiple, closed_at
            FROM trades
            WHERE mode = 'PAPER' AND status IN ('CLOSED','STOPPED') AND pnl_usdt IS NOT NULL
            ORDER BY closed_at ASC
            """
        )
    )
    rows = [dict(row) for row in result.mappings().all()]
    pnls = [float(row["pnl_usdt"] or 0) for row in rows]
    rs = [float(row["r_multiple"]) for row in rows if row["r_multiple"] is not None]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (None if gross_profit == 0 else 999.0)

    equity = starting_equity
    peak = equity
    max_drawdown_pct = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            drawdown = ((peak - equity) / peak) * 100
            max_drawdown_pct = max(max_drawdown_pct, drawdown)

    total = len(pnls)
    return {
        "closed_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / total) * 100, 2) if total else None,
        "gross_profit_usdt": round(gross_profit, 4),
        "gross_loss_usdt": round(gross_loss, 4),
        "net_pnl_usdt": round(sum(pnls), 4),
        "expectancy_usdt_per_trade": round(sum(pnls) / total, 4) if total else None,
        "average_r": round(sum(rs) / len(rs), 4) if rs else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "starting_equity_usdt": starting_equity,
        "current_equity_usdt": round(equity, 4),
        "ready_for_real_money": bool(
            total >= 100
            and profit_factor is not None
            and profit_factor >= 1.5
            and max_drawdown_pct <= 12
            and sum(pnls) > 0
        ),
    }
