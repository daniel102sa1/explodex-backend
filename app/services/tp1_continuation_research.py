from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client

HORIZONS_MINUTES = (30, 60, 120, 240)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dt_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _horizon_metrics(
    *,
    direction: str,
    entry: float,
    stop: float,
    tp1: float,
    outcome_at: datetime,
    candles: list[dict[str, float]],
    horizon_minutes: int,
) -> dict[str, Any] | None:
    risk = abs(entry - stop)
    if risk <= 0 or entry <= 0 or tp1 <= 0:
        return None

    start_ms = _dt_ms(outcome_at)
    end_ms = start_ms + horizon_minutes * 60_000
    rows = [row for row in candles if start_ms <= row["time"] <= end_ms]
    if not rows:
        return None

    if direction == "LONG":
        best = max(row["high"] for row in rows)
        worst = min(row["low"] for row in rows)
        total_r = (best - entry) / risk
        tp1_r = (tp1 - entry) / risk
        extra_r = max(0.0, (best - tp1) / risk)
        pullback_from_tp1_r = max(0.0, (tp1 - worst) / risk)
        held_above_entry = worst > entry
    else:
        best = min(row["low"] for row in rows)
        worst = max(row["high"] for row in rows)
        total_r = (entry - best) / risk
        tp1_r = (entry - tp1) / risk
        extra_r = max(0.0, (tp1 - best) / risk)
        pullback_from_tp1_r = max(0.0, (worst - tp1) / risk)
        held_above_entry = worst < entry

    return {
        "horizon_minutes": horizon_minutes,
        "sampled_candles": len(rows),
        "tp1_r": round(tp1_r, 4),
        "max_total_r": round(total_r, 4),
        "extra_r_after_tp1": round(extra_r, 4),
        "pullback_from_tp1_r": round(pullback_from_tp1_r, 4),
        "held_beyond_entry": held_above_entry,
        "reached_2r": total_r >= 2.0,
        "reached_3r": total_r >= 3.0,
        "reached_4r": total_r >= 4.0,
    }


async def update_tp1_continuation_memory(db: AsyncSession, limit: int = 120) -> dict[str, Any]:
    """Observe post-TP1 continuation while recent 5m candles are still fetchable.

    This is research-only. It never changes an active trade, stop, target, verdict,
    or leverage. Horizons are only written after enough wall-clock time has elapsed.
    """
    result = await db.execute(
        text(
            """
            SELECT id::text, symbol, direction, entry_price, stop_loss, tp1,
                   outcome_at, metadata
            FROM verdict_memory
            WHERE outcome = 'TP1_FIRST'
              AND outcome_at IS NOT NULL
              AND outcome_at <= NOW() - INTERVAL '30 minutes'
              AND outcome_at >= NOW() - INTERVAL '20 hours'
              AND COALESCE(metadata->'tp1_continuation'->>'complete','false') <> 'true'
            ORDER BY outcome_at ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows = [dict(row) for row in result.mappings().all()]
    if not rows:
        return {"seen": 0, "updated": 0, "symbols_failed": 0, "market_errors": []}

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(row)

    updated = 0
    market_errors: list[dict[str, str]] = []
    now = datetime.now(timezone.utc)

    for symbol, group in grouped.items():
        try:
            raw = await binance_client.klines(symbol, interval="5m", limit=300)
        except Exception as exc:
            market_errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            continue

        candles: list[dict[str, float]] = []
        for candle in raw or []:
            if not isinstance(candle, list) or len(candle) < 5:
                continue
            candles.append({
                "time": int(candle[0]),
                "high": _f(candle[2]),
                "low": _f(candle[3]),
            })
        if not candles:
            market_errors.append({"symbol": symbol, "error": "empty_candle_payload"})
            continue

        for row in group:
            outcome_at = row.get("outcome_at")
            if not isinstance(outcome_at, datetime):
                continue
            age_minutes = max(0.0, (now - outcome_at).total_seconds() / 60.0)
            metadata = _json_obj(row.get("metadata"))
            continuation = dict(metadata.get("tp1_continuation") or {})
            horizons = dict(continuation.get("horizons") or {})
            changed = False

            for horizon in HORIZONS_MINUTES:
                key = str(horizon)
                if age_minutes < horizon or key in horizons:
                    continue
                metrics = _horizon_metrics(
                    direction=str(row.get("direction") or "LONG"),
                    entry=_f(row.get("entry_price")),
                    stop=_f(row.get("stop_loss")),
                    tp1=_f(row.get("tp1")),
                    outcome_at=outcome_at,
                    candles=candles,
                    horizon_minutes=horizon,
                )
                if metrics:
                    horizons[key] = metrics
                    changed = True

            continuation.update({
                "version": "tp1_continuation_v1",
                "horizons": horizons,
                "complete": all(str(h) in horizons for h in HORIZONS_MINUTES),
                "research_only": True,
            })

            if not changed and not continuation.get("complete"):
                continue

            metadata["tp1_continuation"] = continuation
            await db.execute(
                text(
                    """
                    UPDATE verdict_memory
                    SET metadata = CAST(:metadata AS JSONB)
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": row["id"], "metadata": json.dumps(metadata, separators=(",", ":"))},
            )
            updated += 1

    if updated:
        await db.commit()
    return {
        "seen": len(rows),
        "updated": updated,
        "symbols_failed": len(market_errors),
        "market_errors": market_errors[:20],
    }


def _summary(values: list[dict[str, Any]]) -> dict[str, Any]:
    if not values:
        return {"sample": 0, "status": "CALIBRATING"}
    extras = [_f(v.get("extra_r_after_tp1")) for v in values]
    totals = [_f(v.get("max_total_r")) for v in values]
    pullbacks = [_f(v.get("pullback_from_tp1_r")) for v in values]
    sample = len(values)
    return {
        "sample": sample,
        "avg_extra_r_after_tp1": round(mean(extras), 4),
        "avg_max_total_r": round(mean(totals), 4),
        "avg_pullback_from_tp1_r": round(mean(pullbacks), 4),
        "reached_2r_pct": round(sum(1 for v in values if v.get("reached_2r")) / sample * 100.0, 2),
        "reached_3r_pct": round(sum(1 for v in values if v.get("reached_3r")) / sample * 100.0, 2),
        "reached_4r_pct": round(sum(1 for v in values if v.get("reached_4r")) / sample * 100.0, 2),
        "held_beyond_entry_pct": round(sum(1 for v in values if v.get("held_beyond_entry")) / sample * 100.0, 2),
        "status": "USABLE" if sample >= 30 else "CALIBRATING",
    }


async def build_tp1_continuation_report(db: AsyncSession) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT direction, metadata
            FROM verdict_memory
            WHERE outcome = 'TP1_FIRST'
              AND metadata ? 'tp1_continuation'
            ORDER BY outcome_at DESC NULLS LAST
            LIMIT 1200
            """
        )
    )

    global_horizons: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_track: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))

    for source in result.mappings().all():
        metadata = _json_obj(source.get("metadata"))
        continuation = dict(metadata.get("tp1_continuation") or {})
        horizons = dict(continuation.get("horizons") or {})
        track = "FAST_TRACK" if metadata.get("fast_track") is True else "NORMAL"
        burst = "BURST" if metadata.get("burst_detected") is True else "NO_BURST"
        lock_count = str(metadata.get("lock_count") or "N/D")
        cohort = f"{track}|{burst}|LOCK_{lock_count}"
        for horizon, values in horizons.items():
            if not isinstance(values, dict):
                continue
            global_horizons[horizon].append(values)
            by_track[cohort][horizon].append(values)

    cohorts = []
    for cohort, horizons in by_track.items():
        for horizon, values in horizons.items():
            summary = _summary(values)
            if summary["sample"] <= 0:
                continue
            cohorts.append({"cohort": cohort, "horizon_minutes": int(horizon), **summary})
    cohorts.sort(key=lambda row: (-int(row.get("sample") or 0), int(row.get("horizon_minutes") or 0)))

    return {
        "mode": "SHADOW_RESEARCH",
        "version": "tp1_continuation_v1",
        "horizons": {
            horizon: _summary(values)
            for horizon, values in sorted(global_horizons.items(), key=lambda item: int(item[0]))
        },
        "cohorts": cohorts[:80],
        "rule": "Post-TP1 continuation is descriptive below 30 comparable samples per horizon/cohort and cannot change trade management yet.",
        "probability_note": "Historical continuation rates are not a probability guarantee for the next trade.",
    }
