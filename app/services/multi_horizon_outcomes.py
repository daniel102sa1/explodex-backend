from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client

VERSION = "multi_horizon_outcomes_v1"

# Short horizons evaluate entry timing. Long horizons evaluate whether the
# directional thesis eventually expanded/continued. They are deliberately kept
# separate so a 24h directional success cannot justify a bad immediate entry.
HORIZONS_MINUTES: tuple[int, ...] = (1, 3, 5, 10, 15, 30, 60, 240, 360, 1440)
HORIZON_LABELS = {
    1: "1m",
    3: "3m",
    5: "5m",
    10: "10m",
    15: "15m",
    30: "30m",
    60: "1h",
    240: "4h",
    360: "6h",
    1440: "24h",
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _interval_for_horizon(minutes: int) -> tuple[str, int]:
    if minutes <= 60:
        return "1m", 1
    if minutes <= 360:
        return "5m", 5
    return "15m", 15


def _parse_candles(rows: list[list[Any]]) -> list[dict[str, float]]:
    parsed: list[dict[str, float]] = []
    for row in rows:
        if len(row) < 5:
            continue
        parsed.append({
            "time": float(row[0]),
            "open": _f(row[1]),
            "high": _f(row[2]),
            "low": _f(row[3]),
            "close": _f(row[4]),
        })
    parsed.sort(key=lambda item: item["time"])
    return parsed


def evaluate_horizon(
    *,
    candles: list[dict[str, float]],
    observed_ms: int,
    horizon_minutes: int,
    direction: str,
    entry: float,
    stop: float,
    tp1: float,
    resolution_minutes: int,
) -> dict[str, Any] | None:
    """Evaluate one fixed horizon without using future data beyond that horizon."""
    if entry <= 0 or not candles:
        return None

    target_ms = observed_ms + horizon_minutes * 60_000
    # Use bars whose opening timestamp is at/after the decision and no later
    # than the target. With 5m/15m bars this is intentionally approximate and
    # the resolution is stored alongside the result.
    window = [c for c in candles if observed_ms <= int(c["time"]) <= target_ms]
    if not window:
        # A decision can happen in the middle of a candle. Accept the first bar
        # after the decision only if it still belongs to the requested horizon.
        window = [c for c in candles if observed_ms <= int(c["time"]) <= target_ms + resolution_minutes * 60_000]
        if not window:
            return None

    # Price at horizon = close of the latest bar whose opening time is closest
    # to the target without intentionally looking into later bars.
    last = max(window, key=lambda item: item["time"])
    high = max(c["high"] for c in window)
    low = min(c["low"] for c in window)
    close = last["close"]
    side = str(direction or "").upper()

    if side == "SHORT":
        directional_return = (entry - close) / entry * 100.0
        mfe = max(0.0, (entry - low) / entry * 100.0)
        mae = max(0.0, (high - entry) / entry * 100.0)
        tp1_hit = tp1 > 0 and low <= tp1
        stop_hit = stop > 0 and high >= stop
    else:
        directional_return = (close - entry) / entry * 100.0
        mfe = max(0.0, (high - entry) / entry * 100.0)
        mae = max(0.0, (entry - low) / entry * 100.0)
        tp1_hit = tp1 > 0 and high >= tp1
        stop_hit = stop > 0 and low <= stop

    stop_distance_pct = abs(entry - stop) / entry * 100.0 if stop > 0 else 0.0
    favorable_r = mfe / stop_distance_pct if stop_distance_pct > 0 else None
    adverse_r = mae / stop_distance_pct if stop_distance_pct > 0 else None

    # Expansion labels are descriptive research labels, not probabilities.
    if favorable_r is not None and favorable_r >= 2.0:
        expansion = "STRONG_EXPANSION"
    elif favorable_r is not None and favorable_r >= 1.0:
        expansion = "EXPANSION"
    elif directional_return > 0:
        expansion = "DIRECTION_CORRECT_SMALL"
    elif directional_return < 0:
        expansion = "DIRECTION_WRONG"
    else:
        expansion = "FLAT"

    return {
        "horizon_minutes": horizon_minutes,
        "label": HORIZON_LABELS[horizon_minutes],
        "resolution_minutes": resolution_minutes,
        "price_at_horizon": round(close, 12),
        "directional_return_pct": round(directional_return, 5),
        "mfe_pct": round(mfe, 5),
        "mae_pct": round(mae, 5),
        "favorable_r": round(favorable_r, 4) if favorable_r is not None else None,
        "adverse_r": round(adverse_r, 4) if adverse_r is not None else None,
        "tp1_hit_by_horizon": bool(tp1_hit),
        "stop_hit_by_horizon": bool(stop_hit),
        "expansion_class": expansion,
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }


async def update_multi_horizon_outcomes(db: AsyncSession, limit: int = 80) -> dict[str, Any]:
    """Incrementally fill due outcome horizons in verdict_memory metadata."""
    now = datetime.now(timezone.utc)
    result = await db.execute(text("""
        SELECT id::text, symbol, observed_at, direction, entry_price,
               stop_loss, tp1, metadata
        FROM verdict_memory
        WHERE observed_at <= NOW() - INTERVAL '1 minute'
          AND observed_at >= NOW() - INTERVAL '72 hours'
        ORDER BY observed_at DESC
        LIMIT :limit
    """), {"limit": max(10, min(int(limit), 250))})
    rows = [dict(row) for row in result.mappings().all()]
    if not rows:
        return {"version": VERSION, "checked": 0, "updated": 0, "measurements": 0}

    due_by_row: dict[str, list[int]] = {}
    row_by_id: dict[str, dict[str, Any]] = {}
    requests: set[tuple[str, str]] = set()

    for row in rows:
        metadata = _meta(row.get("metadata"))
        outcomes = metadata.get("horizon_outcomes") if isinstance(metadata.get("horizon_outcomes"), dict) else {}
        age_minutes = max(0.0, (now - row["observed_at"]).total_seconds() / 60.0)
        due: list[int] = []
        for horizon in HORIZONS_MINUTES:
            label = HORIZON_LABELS[horizon]
            if age_minutes >= horizon and label not in outcomes:
                due.append(horizon)
                interval, _ = _interval_for_horizon(horizon)
                requests.add((str(row["symbol"]), interval))
        if due:
            row_id = str(row["id"])
            due_by_row[row_id] = due
            row_by_id[row_id] = row

    if not due_by_row:
        return {"version": VERSION, "checked": len(rows), "updated": 0, "measurements": 0, "due": 0}

    semaphore = asyncio.Semaphore(6)
    market_data: dict[tuple[str, str], list[dict[str, float]]] = {}
    errors: list[str] = []

    async def fetch_one(symbol: str, interval: str) -> None:
        async with semaphore:
            try:
                raw = await binance_client.klines(symbol, interval=interval, limit=300)
                market_data[(symbol, interval)] = _parse_candles(raw)
            except Exception as exc:
                errors.append(f"{symbol}:{interval}:{type(exc).__name__}:{str(exc)[:120]}")

    await asyncio.gather(*(fetch_one(symbol, interval) for symbol, interval in sorted(requests)))

    updated = 0
    measurements = 0
    measured_by_horizon: defaultdict[str, int] = defaultdict(int)

    for row_id, due in due_by_row.items():
        row = row_by_id[row_id]
        metadata = _meta(row.get("metadata"))
        outcomes = metadata.get("horizon_outcomes") if isinstance(metadata.get("horizon_outcomes"), dict) else {}
        observed_ms = int(row["observed_at"].timestamp() * 1000)
        changed = False

        for horizon in due:
            interval, resolution = _interval_for_horizon(horizon)
            candles = market_data.get((str(row["symbol"]), interval), [])
            evaluated = evaluate_horizon(
                candles=candles,
                observed_ms=observed_ms,
                horizon_minutes=horizon,
                direction=str(row.get("direction") or "LONG"),
                entry=_f(row.get("entry_price")),
                stop=_f(row.get("stop_loss")),
                tp1=_f(row.get("tp1")),
                resolution_minutes=resolution,
            )
            if evaluated is None:
                continue
            label = HORIZON_LABELS[horizon]
            outcomes[label] = evaluated
            measured_by_horizon[label] += 1
            measurements += 1
            changed = True

        if not changed:
            continue
        metadata["horizon_outcomes"] = outcomes
        metadata["horizon_tracker_version"] = VERSION
        metadata["horizon_roles"] = {
            "entry_timing": ["1m", "3m", "5m", "10m", "15m", "30m", "1h"],
            "directional_continuation": ["4h", "6h", "24h"],
        }
        await db.execute(text("""
            UPDATE verdict_memory
            SET metadata=CAST(:metadata AS JSONB), evaluated_at=NOW()
            WHERE id=CAST(:id AS UUID)
        """), {"id": row_id, "metadata": json.dumps(metadata)})
        updated += 1

    await db.commit()
    return {
        "version": VERSION,
        "checked": len(rows),
        "rows_due": len(due_by_row),
        "updated": updated,
        "measurements": measurements,
        "measured_by_horizon": dict(measured_by_horizon),
        "market_requests": len(requests),
        "errors": errors[:10],
        "note": "4h/6h/24h measure directional continuation; they do not retroactively validate a poor entry timing.",
    }
