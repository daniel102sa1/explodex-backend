from __future__ import annotations

import asyncio
import json
import math
import time
from bisect import bisect_right
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.binance import binance_client
from app.services.impulse_pullback_confirmation import build_impulse_pullback_confirmation
from app.services.price_action_pattern_vision import detect_price_action_patterns

VERSION = "historical_market_brain_v1_ohlcv_replay"
FEATURE_VERSION = "hist_ohlcv_features_v1"
HORIZON_BARS = {"15m": 3, "1h": 12, "4h": 48, "12h": 144, "24h": 288}
POLICY = {
    "paper_only": True,
    "shadow_only": True,
    "point_in_time_features_only": True,
    "can_create_entry": False,
    "can_change_direction": False,
    "can_raise_leverage": False,
    "derivatives_history_included": "BEST_EFFORT_FUNDING_AND_OI",
    "score_is_probability": False,
}

_failed_until: dict[str, float] = {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(a: float, b: float) -> float:
    return ((b - a) / a * 100.0) if a else 0.0


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    out = values[0]
    for value in values[1:]:
        out = alpha * value + (1.0 - alpha) * out
    return out


def _normalize(rows: list[list[Any]]) -> list[list[float]]:
    out: list[list[float]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            ts = int(row[0])
            o, h, l, c = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
            # Binance quote volume is index 7. Fall back to base volume * close.
            qv = float(row[7]) if len(row) > 7 and row[7] not in (None, "") else float(row[5]) * c
        except (TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0:
            continue
        out.append([float(ts), o, h, l, c, max(0.0, qv)])
    out.sort(key=lambda row: row[0])
    return out


def _atr_abs(rows: list[list[float]], idx: int, period: int = 14) -> float:
    if idx < 1:
        return 0.0
    start = max(1, idx - period + 1)
    trs: list[float] = []
    for i in range(start, idx + 1):
        high, low, prev = rows[i][2], rows[i][3], rows[i - 1][4]
        trs.append(max(high - low, abs(high - prev), abs(low - prev)))
    return mean(trs) if trs else 0.0


def _change(rows: list[list[float]], idx: int, bars_back: int) -> float:
    prior = max(0, idx - bars_back)
    return _pct(rows[prior][4], rows[idx][4])


def _btc_features(btc_rows: list[list[float]], btc_times: list[int], ts: int) -> dict[str, Any]:
    pos = bisect_right(btc_times, ts) - 1
    if pos < 48:
        return {"btc_1h_pct": None, "btc_4h_pct": None, "btc_regime": "UNKNOWN"}
    ch1 = _change(btc_rows, pos, 12)
    ch4 = _change(btc_rows, pos, 48)
    regime = "BULLISH" if ch1 > 0.35 and ch4 > 0.8 else "BEARISH" if ch1 < -0.35 and ch4 < -0.8 else "NEUTRAL"
    return {"btc_1h_pct": round(ch1, 5), "btc_4h_pct": round(ch4, 5), "btc_regime": regime}


def _derivative_features(
    *,
    ts: int,
    funding_rows: list[dict[str, Any]] | None = None,
    funding_times: list[int] | None = None,
    oi_rows: list[dict[str, Any]] | None = None,
    oi_times: list[int] | None = None,
) -> dict[str, Any]:
    funding_rows = funding_rows or []
    funding_times = funding_times or []
    oi_rows = oi_rows or []
    oi_times = oi_times or []

    funding_rate = None
    if funding_rows and funding_times:
        pos = bisect_right(funding_times, ts) - 1
        if pos >= 0:
            funding_rate = _f(funding_rows[pos].get("fundingRate"), None)

    oi_change_1h = None
    if oi_rows and oi_times:
        pos = bisect_right(oi_times, ts) - 1
        prev = bisect_right(oi_times, ts - 3_600_000) - 1
        if pos >= 0 and prev >= 0 and prev < pos:
            now_row = oi_rows[pos]
            prev_row = oi_rows[prev]
            now_val = _f(now_row.get("sumOpenInterestValue"), _f(now_row.get("sumOpenInterest")))
            prev_val = _f(prev_row.get("sumOpenInterestValue"), _f(prev_row.get("sumOpenInterest")))
            if prev_val > 0:
                oi_change_1h = (now_val - prev_val) / prev_val * 100.0

    return {
        "funding_rate": round(funding_rate, 8) if funding_rate is not None else None,
        "oi_change_1h_pct": round(oi_change_1h, 6) if oi_change_1h is not None else None,
    }


def _feature_vector(
    rows: list[list[float]],
    idx: int,
    *,
    btc_rows: list[list[float]],
    btc_times: list[int],
    funding_rows: list[dict[str, Any]] | None = None,
    funding_times: list[int] | None = None,
    oi_rows: list[dict[str, Any]] | None = None,
    oi_times: list[int] | None = None,
) -> dict[str, Any]:
    current = rows[idx][4]
    atr = _atr_abs(rows, idx)
    atr_pct = atr / current * 100.0 if current > 0 else 0.0

    closes = [row[4] for row in rows[max(0, idx - 80):idx + 1]]
    vols = [row[5] for row in rows[max(0, idx - 32):idx + 1]]
    ema9 = _ema(closes[-40:], 9)
    ema21 = _ema(closes[-60:], 21)
    ema_spread = ((ema9 - ema21) / current * 100.0) if current else 0.0

    previous_vols = vols[-21:-1]
    rvol = rows[idx][5] / mean(previous_vols) if previous_vols and mean(previous_vols) > 0 else 1.0
    recent_vol = mean(vols[-3:]) if len(vols) >= 3 else rows[idx][5]
    baseline_vols = vols[-15:-3]
    vacc = recent_vol / mean(baseline_vols) if baseline_vols and mean(baseline_vols) > 0 else 1.0

    range12 = rows[max(0, idx - 11):idx + 1]
    range48 = rows[max(0, idx - 47):idx + 1]
    high12, low12 = max(r[2] for r in range12), min(r[3] for r in range12)
    high48, low48 = max(r[2] for r in range48), min(r[3] for r in range48)
    compression = (high12 - low12) / (high48 - low48) if high48 > low48 else 1.0
    range_pos = (current - low48) / (high48 - low48) if high48 > low48 else 0.5

    bar = rows[idx]
    body = abs(bar[4] - bar[1])
    upper = bar[2] - max(bar[1], bar[4])
    lower = min(bar[1], bar[4]) - bar[3]

    prior_high = max(r[2] for r in rows[max(0, idx - 48):idx]) if idx > 0 else current
    prior_low = min(r[3] for r in rows[max(0, idx - 48):idx]) if idx > 0 else current
    dist_high_atr = (prior_high - current) / atr if atr > 0 else 0.0
    dist_low_atr = (current - prior_low) / atr if atr > 0 else 0.0

    trend = "BULLISH" if ema_spread > 0.12 and _change(rows, idx, 12) > -0.2 else "BEARISH" if ema_spread < -0.12 and _change(rows, idx, 12) < 0.2 else "NEUTRAL"

    point_window = rows[max(0, idx - 119):idx + 1]
    price_action: dict[str, Any]
    impulse: dict[str, Any]
    try:
        price_action = detect_price_action_patterns(point_window)
    except Exception:
        price_action = {"available": False}
    try:
        impulse = build_impulse_pullback_confirmation(point_window)
    except Exception:
        impulse = {"available": False}

    top = list(price_action.get("top_patterns") or [])
    top_pattern = top[0] if top and isinstance(top[0], dict) else {}
    cycle = price_action.get("market_cycle") if isinstance(price_action.get("market_cycle"), dict) else {}
    ts = int(rows[idx][0])
    btc = _btc_features(btc_rows, btc_times, ts)
    derivatives = _derivative_features(
        ts=ts,
        funding_rows=funding_rows,
        funding_times=funding_times,
        oi_rows=oi_rows,
        oi_times=oi_times,
    )

    return {
        "change_5m_pct": round(_change(rows, idx, 1), 6),
        "change_15m_pct": round(_change(rows, idx, 3), 6),
        "change_1h_pct": round(_change(rows, idx, 12), 6),
        "change_4h_pct": round(_change(rows, idx, 48), 6),
        "atr_pct": round(atr_pct, 6),
        "relative_volume": round(rvol, 6),
        "volume_acceleration": round(vacc, 6),
        "compression_ratio": round(compression, 6),
        "ema_spread_pct": round(ema_spread, 6),
        "range_position_48": round(range_pos, 6),
        "body_atr": round(body / atr, 6) if atr > 0 else 0.0,
        "upper_wick_atr": round(upper / atr, 6) if atr > 0 else 0.0,
        "lower_wick_atr": round(lower / atr, 6) if atr > 0 else 0.0,
        "distance_prior_high_atr": round(dist_high_atr, 6),
        "distance_prior_low_atr": round(dist_low_atr, 6),
        "trend": trend,
        "pattern_name": top_pattern.get("name"),
        "pattern_bias": top_pattern.get("bias"),
        "market_cycle": cycle.get("state"),
        "impulse_phase": impulse.get("phase"),
        "impulse_direction": impulse.get("direction"),
        "impulse_quality": impulse.get("quality_score"),
        **btc,
        **derivatives,
    }


def _barrier_outcome(
    future: list[list[float]],
    *,
    entry: float,
    atr: float,
    direction: str,
) -> dict[str, Any]:
    if not future or entry <= 0 or atr <= 0:
        return {"outcome": "UNAVAILABLE", "bars": None}
    if direction == "LONG":
        target, stop = entry + 1.5 * atr, entry - atr
    else:
        target, stop = entry - 1.5 * atr, entry + atr
    for n, bar in enumerate(future, start=1):
        hit_target = bar[2] >= target if direction == "LONG" else bar[3] <= target
        hit_stop = bar[3] <= stop if direction == "LONG" else bar[2] >= stop
        if hit_target and hit_stop:
            return {"outcome": "AMBIGUOUS_SAME_BAR", "bars": n}
        if hit_target:
            return {"outcome": "TARGET_FIRST", "bars": n}
        if hit_stop:
            return {"outcome": "STOP_FIRST", "bars": n}
    return {"outcome": "NONE", "bars": None}


def _outcomes(rows: list[list[float]], idx: int) -> dict[str, Any]:
    entry = rows[idx][4]
    atr = _atr_abs(rows, idx)
    result: dict[str, Any] = {}
    for label, count in HORIZON_BARS.items():
        future = rows[idx + 1:idx + 1 + count]
        if len(future) < count:
            continue
        high = max(r[2] for r in future)
        low = min(r[3] for r in future)
        result[label] = {
            "return_pct": round(_pct(entry, future[-1][4]), 6),
            "up_mfe_pct": round((high - entry) / entry * 100.0, 6),
            "down_mfe_pct": round((entry - low) / entry * 100.0, 6),
            "long_barrier_1p5atr_vs_1atr": _barrier_outcome(future, entry=entry, atr=atr, direction="LONG"),
            "short_barrier_1p5atr_vs_1atr": _barrier_outcome(future, entry=entry, atr=atr, direction="SHORT"),
        }
    return result


async def backfill_symbol(
    db: AsyncSession,
    symbol: str,
    *,
    days: int | None = None,
    stride_bars: int | None = None,
) -> dict[str, Any]:
    symbol = str(symbol or "").upper().strip()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    safe_days = max(7, min(int(days or settings.historical_market_backfill_days), 365))
    stride = max(1, min(int(stride_bars or settings.historical_market_stride_bars), 72))

    symbol_payload = await binance_client.historical_spot_klines(
        symbol, settings.historical_market_interval, safe_days
    )
    btc_payload = (
        symbol_payload
        if symbol == "BTCUSDT"
        else await binance_client.historical_spot_klines(
            "BTCUSDT", settings.historical_market_interval, safe_days
        )
    )
    funding_payload, oi_payload = await asyncio.gather(
        binance_client.historical_funding_rates(symbol, safe_days),
        binance_client.historical_open_interest(symbol, min(safe_days, 30), "5m"),
    )

    rows = _normalize(list(symbol_payload.get("rows") or []))
    btc_rows = _normalize(list(btc_payload.get("rows") or []))
    if len(rows) < 500 or len(btc_rows) < 500:
        raise RuntimeError(f"insufficient historical rows for {symbol}: symbol={len(rows)} btc={len(btc_rows)}")

    btc_times = [int(row[0]) for row in btc_rows]
    funding_rows = [dict(row) for row in list(funding_payload.get("rows") or []) if isinstance(row, dict)]
    funding_times = [int(row.get("fundingTime") or 0) for row in funding_rows]
    oi_rows = [dict(row) for row in list(oi_payload.get("rows") or []) if isinstance(row, dict)]
    oi_times = [int(row.get("timestamp") or 0) for row in oi_rows]
    max_horizon = max(HORIZON_BARS.values())
    start = 120
    stop = len(rows) - max_horizon - 1
    if stop <= start:
        raise RuntimeError(f"history too short after horizon reserve for {symbol}")

    records: list[dict[str, Any]] = []
    for idx in range(start, stop, stride):
        features = _feature_vector(
            rows,
            idx,
            btc_rows=btc_rows,
            btc_times=btc_times,
            funding_rows=funding_rows,
            funding_times=funding_times,
            oi_rows=oi_rows,
            oi_times=oi_times,
        )
        outcomes = _outcomes(rows, idx)
        if not outcomes:
            continue
        records.append({
            "symbol": symbol,
            "observed_at": datetime.fromtimestamp(rows[idx][0] / 1000.0, tz=timezone.utc),
            "interval": settings.historical_market_interval,
            "source": str(symbol_payload.get("source") or "BINANCE_SPOT_HISTORY"),
            "feature_version": FEATURE_VERSION,
            "features": json.dumps(features),
            "outcomes": json.dumps(outcomes),
            "sample_stride": stride,
        })

    written = 0
    statement = text("""
        INSERT INTO historical_market_replay (
            symbol, observed_at, interval, source, feature_version,
            features, outcomes, sample_stride
        ) VALUES (
            :symbol, :observed_at, :interval, :source, :feature_version,
            CAST(:features AS JSONB), CAST(:outcomes AS JSONB), :sample_stride
        )
        ON CONFLICT (symbol, observed_at, interval, feature_version)
        DO UPDATE SET
            source=EXCLUDED.source,
            features=EXCLUDED.features,
            outcomes=EXCLUDED.outcomes,
            sample_stride=EXCLUDED.sample_stride
    """)
    for offset in range(0, len(records), 250):
        batch = records[offset:offset + 250]
        if batch:
            await db.execute(statement, batch)
            written += len(batch)
    await db.commit()

    return {
        "version": VERSION,
        "symbol": symbol,
        "source": symbol_payload.get("source"),
        "days": safe_days,
        "interval": settings.historical_market_interval,
        "stride_bars": stride,
        "raw_rows": len(rows),
        "derivatives_history": {
            "funding_available": bool(funding_payload.get("available")),
            "funding_rows": len(funding_rows),
            "oi_available": bool(oi_payload.get("available")),
            "oi_rows": len(oi_rows),
            "oi_window_days_max": 30,
        },
        "replay_rows_written": written,
        "first_observation": records[0]["observed_at"].isoformat() if records else None,
        "last_observation": records[-1]["observed_at"].isoformat() if records else None,
        "policy": POLICY,
    }


def _current_features(scored: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    metrics = scored.get("metrics") if isinstance(scored.get("metrics"), dict) else {}
    seq = prediction.get("sequence") if isinstance(prediction.get("sequence"), dict) else {}
    pro = prediction.get("professional_arsenal") if isinstance(prediction.get("professional_arsenal"), dict) else {}
    vision = pro.get("price_action_pattern_vision") if isinstance(pro.get("price_action_pattern_vision"), dict) else {}
    top = list(vision.get("top_patterns") or [])
    pattern = top[0] if top and isinstance(top[0], dict) else {}
    impulse = prediction.get("impulse_pullback_confirmation") if isinstance(prediction.get("impulse_pullback_confirmation"), dict) else {}
    if not impulse:
        impulse = pro.get("impulse_pullback_confirmation") if isinstance(pro.get("impulse_pullback_confirmation"), dict) else {}
    cycle = vision.get("market_cycle") if isinstance(vision.get("market_cycle"), dict) else {}

    current = _f(scored.get("current_price"))
    high48 = _f(seq.get("range_high_48"))
    low48 = _f(seq.get("range_low_48"))
    range_pos = (current - low48) / (high48 - low48) if current > 0 and high48 > low48 > 0 else None
    ema9 = _f(metrics.get("ema9"))
    ema21 = _f(metrics.get("ema21"))
    ema_spread = (ema9 - ema21) / current * 100.0 if current > 0 and (ema9 or ema21) else None

    trend = "BULLISH" if ema_spread is not None and ema_spread > 0.12 else "BEARISH" if ema_spread is not None and ema_spread < -0.12 else "NEUTRAL"
    return {
        "change_5m_pct": metrics.get("change_5m_pct"),
        "change_15m_pct": metrics.get("change_15m_pct"),
        "change_1h_pct": metrics.get("change_1h_pct"),
        "atr_pct": metrics.get("atr_pct"),
        "relative_volume": metrics.get("relative_volume"),
        "volume_acceleration": metrics.get("volume_acceleration"),
        "compression_ratio": metrics.get("compression_ratio"),
        "ema_spread_pct": ema_spread,
        "range_position_48": range_pos,
        "distance_prior_high_atr": seq.get("dist_high_atr"),
        "distance_prior_low_atr": seq.get("dist_low_atr"),
        "trend": trend,
        "pattern_name": pattern.get("name"),
        "pattern_bias": pattern.get("bias"),
        "market_cycle": cycle.get("state"),
        "impulse_phase": impulse.get("phase"),
        "impulse_direction": impulse.get("direction"),
        "btc_1h_pct": metrics.get("btc_change_1h_pct"),
        "btc_regime": metrics.get("btc_trend"),
        "funding_rate": metrics.get("funding_rate"),
        "oi_change_1h_pct": metrics.get("oi_change_pct"),
    }


_NUMERIC_SCALES = {
    "change_5m_pct": 0.8,
    "change_15m_pct": 1.5,
    "change_1h_pct": 3.0,
    "atr_pct": 1.2,
    "relative_volume": 0.8,
    "volume_acceleration": 0.8,
    "compression_ratio": 0.45,
    "ema_spread_pct": 0.6,
    "range_position_48": 0.28,
    "distance_prior_high_atr": 1.4,
    "distance_prior_low_atr": 1.4,
    "btc_1h_pct": 2.5,
    "funding_rate": 0.0008,
    "oi_change_1h_pct": 2.0,
}


def _distance(current: dict[str, Any], historical: dict[str, Any]) -> float:
    terms: list[float] = []
    for key, scale in _NUMERIC_SCALES.items():
        if current.get(key) is None or historical.get(key) is None:
            continue
        a, b = _f(current.get(key)), _f(historical.get(key))
        if key in {"relative_volume", "volume_acceleration"}:
            a, b = math.log(max(a, 0.05)), math.log(max(b, 0.05))
            scale = max(scale, 0.35)
        terms.append(((a - b) / max(scale, 1e-6)) ** 2)

    for key, penalty in (
        ("trend", 0.55),
        ("btc_regime", 0.45),
        ("market_cycle", 0.35),
        ("pattern_bias", 0.30),
        ("impulse_direction", 0.35),
    ):
        a, b = str(current.get(key) or ""), str(historical.get(key) or "")
        if a and b and a != b:
            terms.append(penalty ** 2)

    cpat, hpat = str(current.get("pattern_name") or ""), str(historical.get("pattern_name") or "")
    if cpat and hpat and cpat != hpat:
        terms.append(0.50 ** 2)

    if not terms:
        return 999.0
    return math.sqrt(sum(terms) / len(terms))


def _similarity(distance: float) -> float:
    if not math.isfinite(distance):
        return 0.0
    return max(0.0, min(100.0, 100.0 * math.exp(-0.5 * distance * distance)))


def _horizon_summary(rows: list[dict[str, Any]], direction: str, horizon: str) -> dict[str, Any]:
    signed_returns: list[float] = []
    favorable: list[float] = []
    adverse: list[float] = []
    similarities: list[float] = []
    target_first = stop_first = ambiguous = 0

    key = "long_barrier_1p5atr_vs_1atr" if direction == "LONG" else "short_barrier_1p5atr_vs_1atr"
    for row in rows:
        outcomes = row.get("outcomes") if isinstance(row.get("outcomes"), dict) else {}
        outcome = outcomes.get(horizon) if isinstance(outcomes.get(horizon), dict) else {}
        if not outcome:
            continue
        ret = _f(outcome.get("return_pct"))
        signed_returns.append(ret if direction == "LONG" else -ret)
        favorable.append(_f(outcome.get("up_mfe_pct")) if direction == "LONG" else _f(outcome.get("down_mfe_pct")))
        adverse.append(_f(outcome.get("down_mfe_pct")) if direction == "LONG" else _f(outcome.get("up_mfe_pct")))
        similarities.append(_f(row.get("similarity")))
        barrier = outcome.get(key) if isinstance(outcome.get(key), dict) else {}
        state = str(barrier.get("outcome") or "")
        target_first += int(state == "TARGET_FIRST")
        stop_first += int(state == "STOP_FIRST")
        ambiguous += int(state == "AMBIGUOUS_SAME_BAR")

    decided = target_first + stop_first
    positives = sum(1 for value in signed_returns if value > 0)
    return {
        "sample": len(signed_returns),
        "median_similarity": round(median(similarities), 2) if similarities else None,
        "positive_close_rate_pct": round(positives / len(signed_returns) * 100.0, 2) if signed_returns else None,
        "median_signed_return_pct": round(median(signed_returns), 4) if signed_returns else None,
        "mean_signed_return_pct": round(mean(signed_returns), 4) if signed_returns else None,
        "median_favorable_excursion_pct": round(median(favorable), 4) if favorable else None,
        "median_adverse_excursion_pct": round(median(adverse), 4) if adverse else None,
        "generic_1p5atr_before_1atr_rate_pct": round(target_first / decided * 100.0, 2) if decided else None,
        "generic_barrier_decided_sample": decided,
        "generic_barrier_ambiguous_sample": ambiguous,
    }


def _oos_status(rows: list[dict[str, Any]], direction: str) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row["observed_at"])
    if len(ordered) < 60:
        return {
            "status": "LEARNING",
            "sample": len(ordered),
            "minimum": 60,
            "rule": "Need >=60 matched analogs before chronological holdout is considered.",
        }
    split = max(42, int(len(ordered) * 0.70))
    split = min(split, len(ordered) - 18)
    train, test = ordered[:split], ordered[split:]
    train_summary = _horizon_summary(train, direction, "1h")
    test_summary = _horizon_summary(test, direction, "1h")
    train_ret = _f(train_summary.get("mean_signed_return_pct"))
    test_ret = _f(test_summary.get("mean_signed_return_pct"))
    if train_ret > 0 and test_ret > 0:
        status = "HOLDS_OUT_OF_SAMPLE"
    elif train_ret > 0 and test_ret <= 0:
        status = "FAILED_OUT_OF_SAMPLE"
    else:
        status = "NO_TRAIN_EDGE"
    return {
        "status": status,
        "split": "chronological_70_30",
        "train": train_summary,
        "test": test_summary,
        "can_create_entry": False,
    }


async def analog_context(
    db: AsyncSession,
    *,
    symbol: str,
    scored: dict[str, Any],
    prediction: dict[str, Any],
    limit: int = 120,
) -> dict[str, Any]:
    current = _current_features(scored, prediction)
    raw = (await db.execute(text("""
        SELECT symbol, observed_at, features, outcomes
        FROM historical_market_replay
        WHERE feature_version=:version
        ORDER BY observed_at DESC
        LIMIT :limit
    """), {
        "version": FEATURE_VERSION,
        "limit": max(500, min(int(settings.historical_market_max_analog_rows), 30000)),
    })).mappings().all()

    ranked: list[dict[str, Any]] = []
    for item in raw:
        row = dict(item)
        features = row.get("features") if isinstance(row.get("features"), dict) else {}
        outcomes = row.get("outcomes") if isinstance(row.get("outcomes"), dict) else {}
        distance = _distance(current, features)
        similarity = _similarity(distance)
        if similarity < 45.0:
            continue
        ranked.append({
            "symbol": row.get("symbol"),
            "observed_at": row.get("observed_at"),
            "similarity": similarity,
            "features": features,
            "outcomes": outcomes,
        })
    ranked.sort(key=lambda row: row["similarity"], reverse=True)
    selected = ranked[:max(20, min(int(limit), 250))]
    direction = str(scored.get("direction") or prediction.get("direction") or "LONG").upper()
    if direction not in {"LONG", "SHORT"}:
        direction = "LONG"

    sample = len(selected)
    result = {
        "version": VERSION,
        "available": sample > 0,
        "symbol": symbol,
        "direction": direction,
        "sample": sample,
        "minimum_sample": int(settings.historical_market_min_analog_sample),
        "status": "USABLE" if sample >= int(settings.historical_market_min_analog_sample) else "CALIBRATING",
        "top_similarity": round(_f(selected[0].get("similarity")), 2) if selected else None,
        "median_similarity": round(median([_f(row.get("similarity")) for row in selected]), 2) if selected else None,
        "same_symbol_matches": sum(1 for row in selected if str(row.get("symbol")) == symbol),
        "cross_symbol_matches": sum(1 for row in selected if str(row.get("symbol")) != symbol),
        "current_features": current,
        "horizons": {label: _horizon_summary(selected, direction, label) for label in HORIZON_BARS},
        "out_of_sample": _oos_status(selected, direction),
        "top_analogs": [
            {
                "symbol": row.get("symbol"),
                "observed_at": row.get("observed_at").isoformat() if row.get("observed_at") else None,
                "similarity": round(_f(row.get("similarity")), 2),
                "pattern_name": (row.get("features") or {}).get("pattern_name"),
                "market_cycle": (row.get("features") or {}).get("market_cycle"),
                "btc_regime": (row.get("features") or {}).get("btc_regime"),
            }
            for row in selected[:8]
        ],
        "policy": POLICY,
        "note": (
            "Historical analog statistics are descriptive. The generic barrier rate uses a fixed "
            "1.5 ATR target versus 1 ATR stop and is not the current trade's TP/SL probability."
        ),
    }
    return result


async def coverage(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT symbol, COUNT(*) AS samples, MIN(observed_at) AS first_at, MAX(observed_at) AS last_at
        FROM historical_market_replay
        WHERE feature_version=:version
        GROUP BY symbol
        ORDER BY samples DESC, symbol ASC
    """), {"version": FEATURE_VERSION})).mappings().all()
    items = []
    for raw in rows:
        row = dict(raw)
        items.append({
            "symbol": row["symbol"],
            "samples": int(row.get("samples") or 0),
            "first_at": row.get("first_at").isoformat() if row.get("first_at") else None,
            "last_at": row.get("last_at").isoformat() if row.get("last_at") else None,
        })
    return {
        "version": VERSION,
        "feature_version": FEATURE_VERSION,
        "symbols": items,
        "symbols_covered": len(items),
        "total_samples": sum(item["samples"] for item in items),
        "policy": POLICY,
    }


async def backfill_next_liquid_symbol(db: AsyncSession) -> dict[str, Any]:
    if not settings.historical_market_enabled:
        return {"version": VERSION, "status": "DISABLED"}

    counts = {
        str(row["symbol"]): int(row["samples"] or 0)
        for row in (await db.execute(text("""
            SELECT symbol, COUNT(*) AS samples
            FROM historical_market_replay
            WHERE feature_version=:version
            GROUP BY symbol
        """), {"version": FEATURE_VERSION})).mappings().all()
    }
    target_samples = max(300, int(settings.historical_market_backfill_days * 288 / max(1, settings.historical_market_stride_bars) * 0.70))

    tickers = await binance_client.ticker_24h()
    ranked = sorted(
        (
            (str(row.get("symbol") or "").upper(), _f(row.get("quoteVolume")))
            for row in tickers
            if str(row.get("symbol") or "").upper().endswith("USDT")
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    preferred = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT"]
    candidates = list(dict.fromkeys(preferred + [symbol for symbol, _ in ranked]))[:max(5, int(settings.historical_market_max_symbols))]

    errors: list[str] = []
    now = time.monotonic()
    for symbol in candidates:
        if counts.get(symbol, 0) >= target_samples:
            continue
        if _failed_until.get(symbol, 0.0) > now:
            continue
        try:
            result = await backfill_symbol(db, symbol)
            result["status"] = "BACKFILLED"
            result["target_samples"] = target_samples
            return result
        except Exception as exc:
            await db.rollback()
            _failed_until[symbol] = time.monotonic() + 3600.0
            errors.append(f"{symbol}:{type(exc).__name__}:{str(exc)[:160]}")
            if len(errors) >= 4:
                break
    return {
        "version": VERSION,
        "status": "UP_TO_DATE" if not errors else "NO_SYMBOL_BACKFILLED",
        "target_samples": target_samples,
        "errors": errors,
        "covered_symbols": len(counts),
    }
