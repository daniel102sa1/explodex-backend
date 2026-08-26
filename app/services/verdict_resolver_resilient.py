from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _interval_for_age(age: timedelta) -> str:
    if age <= timedelta(hours=24):
        return "5m"
    if age <= timedelta(hours=72):
        return "15m"
    return "1h"


async def resolve_verdict_outcomes_resilient(db: AsyncSession, limit: int = 60) -> dict[str, Any]:
    """Resolve verdict outcomes without allowing one provider/symbol failure to abort the cycle."""
    result = await db.execute(
        text(
            """
            SELECT id::text, symbol, observed_at, direction, entry_price, stop_loss, tp1
            FROM verdict_memory
            WHERE outcome = 'UNRESOLVED'
              AND observed_at <= NOW() - INTERVAL '5 minutes'
              AND observed_at >= NOW() - INTERVAL '7 days'
            ORDER BY observed_at ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows = [dict(r) for r in result.mappings().all()]
    if not rows:
        return {"checked": 0, "resolved": 0, "ambiguous": 0, "market_errors": []}

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["symbol"]].append(row)

    resolved = 0
    ambiguous = 0
    market_errors: list[dict[str, str]] = []
    now = datetime.now(timezone.utc)

    for symbol, group in grouped.items():
        oldest = min(r["observed_at"] for r in group)
        interval = _interval_for_age(now - oldest)
        try:
            candles = await binance_client.klines(symbol, interval=interval, limit=300)
        except Exception as exc:
            market_errors.append({
                "symbol": symbol,
                "interval": interval,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
            continue

        parsed = []
        for candle in candles or []:
            if len(candle) < 5:
                continue
            parsed.append({"time": int(candle[0]), "high": _f(candle[2]), "low": _f(candle[3])})
        if not parsed:
            market_errors.append({"symbol": symbol, "interval": interval, "error": "empty_candle_payload"})
            continue

        for row in group:
            direction = str(row["direction"])
            start_ms = _ms(row["observed_at"])
            stop = _f(row["stop_loss"])
            tp1 = _f(row["tp1"])
            entry = _f(row["entry_price"])
            if not entry or not stop or not tp1:
                continue

            best = entry
            worst = entry
            outcome = None
            outcome_at = None
            mfe_pct = 0.0
            mae_pct = 0.0

            for candle in parsed:
                # Conservative: do not include a candle that opened before observation,
                # because its high/low may contain pre-decision information.
                if candle["time"] < start_ms:
                    continue
                high, low = candle["high"], candle["low"]
                hit_tp = high >= tp1 if direction == "LONG" else low <= tp1
                hit_stop = low <= stop if direction == "LONG" else high >= stop

                if direction == "LONG":
                    best = max(best, high)
                    worst = min(worst, low)
                    mfe_pct = (best - entry) / entry * 100.0
                    mae_pct = (entry - worst) / entry * 100.0
                else:
                    best = min(best, low)
                    worst = max(worst, high)
                    mfe_pct = (entry - best) / entry * 100.0
                    mae_pct = (worst - entry) / entry * 100.0

                if hit_tp and hit_stop:
                    outcome = "AMBIGUOUS"
                    ambiguous += 1
                elif hit_tp:
                    outcome = "TP1_FIRST"
                elif hit_stop:
                    outcome = "STOP_FIRST"
                if outcome:
                    outcome_at = datetime.fromtimestamp(candle["time"] / 1000, tz=timezone.utc)
                    break

            if not outcome:
                continue

            minutes = max(0.0, (outcome_at - row["observed_at"]).total_seconds() / 60.0) if outcome_at else None
            await db.execute(
                text(
                    """
                    UPDATE verdict_memory
                    SET outcome = :outcome,
                        outcome_at = :outcome_at,
                        minutes_to_outcome = :minutes,
                        mfe_pct = :mfe_pct,
                        mae_pct = :mae_pct,
                        evaluated_at = NOW()
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {
                    "id": row["id"],
                    "outcome": outcome,
                    "outcome_at": outcome_at,
                    "minutes": minutes,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                },
            )
            resolved += 1

    if resolved:
        await db.commit()
    return {
        "checked": len(rows),
        "resolved": resolved,
        "ambiguous": ambiguous,
        "market_errors": market_errors[:20],
        "symbols_failed": len(market_errors),
    }
