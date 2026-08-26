from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import sqrt
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


def _wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = wins / total
    z2 = z * z
    center = p + z2 / (2 * total)
    margin = z * sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    denominator = 1 + z2 / total
    return max(0.0, (center - margin) / denominator) * 100.0


def _interval_for_age(age: timedelta) -> str:
    """Choose enough candle coverage to resolve observations up to seven days old."""
    if age <= timedelta(hours=24):
        return "5m"       # 300 candles ~= 25h
    if age <= timedelta(hours=72):
        return "15m"      # 300 candles ~= 75h
    return "1h"           # 300 candles ~= 12.5d


async def capture_enter_verdicts(db: AsyncSession, limit: int = 200) -> dict[str, int]:
    """Persist READY signals as immutable learning observations.

    This intentionally does not create new trades or change execution logic. It only
    records what ExplodeX already decided so outcomes can be measured server-side.
    """
    result = await db.execute(
        text(
            """
            SELECT s.id::text AS signal_id, s.symbol_id::text AS symbol_id, sy.symbol,
                   s.created_at, s.direction, s.setup_score, s.risk_score,
                   s.current_price, s.entry_low, s.entry_high, s.stop_loss,
                   s.tp1, s.tp2, s.tp3, s.reason
            FROM signals s
            JOIN symbols sy ON sy.id = s.symbol_id
            LEFT JOIN verdict_memory vm ON vm.signal_id = s.id
            WHERE s.state = 'READY' AND vm.signal_id IS NULL
            ORDER BY s.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows = result.mappings().all()
    inserted = 0
    for row in rows:
        await db.execute(
            text(
                """
                INSERT INTO verdict_memory (
                    signal_id, symbol_id, symbol, observed_at, direction,
                    setup_score, risk_score, entry_price, entry_low, entry_high,
                    stop_loss, tp1, tp2, tp3, reason, outcome
                ) VALUES (
                    CAST(:signal_id AS uuid), CAST(:symbol_id AS uuid), :symbol, :observed_at, :direction,
                    :setup_score, :risk_score, :entry_price, :entry_low, :entry_high,
                    :stop_loss, :tp1, :tp2, :tp3, :reason, 'UNRESOLVED'
                )
                ON CONFLICT (signal_id) DO NOTHING
                """
            ),
            dict(row),
        )
        inserted += 1
    await db.commit()
    return {"seen": len(rows), "inserted": inserted}


async def resolve_verdict_outcomes(db: AsyncSession, limit: int = 60) -> dict[str, int]:
    """Resolve TP1_FIRST / STOP_FIRST from market candles while the UI is closed."""
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
        return {"checked": 0, "resolved": 0, "ambiguous": 0}

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["symbol"]].append(row)

    resolved = 0
    ambiguous = 0
    now = datetime.now(timezone.utc)

    for symbol, group in grouped.items():
        oldest = min(r["observed_at"] for r in group)
        interval = _interval_for_age(now - oldest)
        candles = await binance_client.klines(symbol, interval=interval, limit=300)
        parsed = []
        for c in candles:
            if len(c) < 5:
                continue
            parsed.append({"time": int(c[0]), "high": _f(c[2]), "low": _f(c[3])})

        for row in group:
            direction = row["direction"]
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

            for c in parsed:
                if c["time"] < start_ms:
                    continue
                high, low = c["high"], c["low"]
                hit_tp = high >= tp1 if direction == "LONG" else low <= tp1
                hit_stop = low <= stop if direction == "LONG" else high >= stop

                if direction == "LONG":
                    best = max(best, high)
                    worst = min(worst, low)
                    mfe_pct = (best - entry) / entry * 100
                    mae_pct = (entry - worst) / entry * 100
                else:
                    best = min(best, low)
                    worst = max(worst, high)
                    mfe_pct = (entry - best) / entry * 100
                    mae_pct = (worst - entry) / entry * 100

                if hit_tp and hit_stop:
                    outcome = "AMBIGUOUS"
                    ambiguous += 1
                elif hit_tp:
                    outcome = "TP1_FIRST"
                elif hit_stop:
                    outcome = "STOP_FIRST"
                if outcome:
                    outcome_at = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
                    break

            if not outcome:
                continue

            minutes = max(0.0, (outcome_at - row["observed_at"]).total_seconds() / 60) if outcome_at else None
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

    await db.commit()
    return {"checked": len(rows), "resolved": resolved, "ambiguous": ambiguous}


async def verdict_memory_stats(db: AsyncSession) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS decided,
                COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                COUNT(*) FILTER (WHERE outcome = 'AMBIGUOUS') AS ambiguous,
                COUNT(*) FILTER (WHERE outcome = 'UNRESOLVED') AS unresolved,
                AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct,
                AVG(minutes_to_outcome) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_minutes
            FROM verdict_memory
            """
        )
    )
    row = dict(result.mappings().one())
    decided = int(row.get("decided") or 0)
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    row["decided"] = decided
    row["wins"] = wins
    row["losses"] = losses
    row["ambiguous"] = int(row.get("ambiguous") or 0)
    row["unresolved"] = int(row.get("unresolved") or 0)
    row["win_rate_pct"] = (wins / decided * 100.0) if decided else None
    row["wilson_low_pct"] = _wilson_lower_bound(wins, decided)
    row["sample_status"] = "CALIBRATING" if decided < 30 else "USABLE"
    row["can_influence_veto"] = decided >= 30
    return row
