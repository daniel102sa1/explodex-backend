from __future__ import annotations

import asyncio
import math
import time
from statistics import mean, median, pstdev
from typing import Any

from app.services.binance import binance_client

VERSION = "higher_timeframe_context_v3_robust_stats"
CACHE_SECONDS = 300.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _percentile(values: list[float], q: float) -> float:
    clean = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not clean:
        return 0.0
    if len(clean) == 1:
        return clean[0]
    q = max(0.0, min(1.0, q))
    pos = (len(clean) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return clean[lo]
    weight = pos - lo
    return clean[lo] * (1.0 - weight) + clean[hi] * weight


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _true_ranges_pct(rows: list[list[Any]]) -> list[float]:
    values: list[float] = []
    for idx in range(1, len(rows)):
        high = _f(rows[idx][2])
        low = _f(rows[idx][3])
        prev_close = _f(rows[idx - 1][4])
        if high <= 0 or low <= 0 or prev_close <= 0:
            continue
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        values.append(tr / prev_close * 100.0)
    return values


def _atr_pct(rows: list[list[Any]], period: int = 14) -> float:
    trs = _true_ranges_pct(rows)
    return mean(trs[-period:]) if trs else 0.0


def _realized_vol_pct(rows: list[list[Any]], period: int = 30) -> float:
    closes = [_f(row[4]) for row in rows if len(row) > 4 and _f(row[4]) > 0]
    if len(closes) < 3:
        return 0.0
    recent = closes[-min(period + 1, len(closes)) :]
    returns = [math.log(b / a) * 100.0 for a, b in zip(recent[:-1], recent[1:]) if a > 0 and b > 0]
    return pstdev(returns) if len(returns) >= 2 else 0.0


def _efficiency_ratio(closes: list[float], period: int = 14) -> float:
    if len(closes) < 3:
        return 0.0
    recent = closes[-min(period + 1, len(closes)) :]
    net = abs(recent[-1] - recent[0])
    path = sum(abs(b - a) for a, b in zip(recent[:-1], recent[1:]))
    return net / path if path > 0 else 0.0


def _frame(rows: list[list[Any]], candles_back: int, interval_hours: float) -> dict[str, Any]:
    valid = [row for row in rows if len(row) > 4 and _f(row[4]) > 0]
    closes = [_f(row[4]) for row in valid]
    if len(closes) < 22:
        return {
            "trend": "UNKNOWN",
            "change_pct": 0.0,
            "ema_gap_pct": 0.0,
            "atr_pct": 0.0,
            "available": False,
            "interval_hours": interval_hours,
        }

    current = closes[-1]
    lookback_index = max(0, len(closes) - max(2, candles_back) - 1)
    reference = closes[lookback_index]
    change = ((current - reference) / reference * 100.0) if reference else 0.0
    ema9 = _ema(closes[-40:], 9)
    ema21 = _ema(closes[-60:], 21)
    gap = ((ema9 - ema21) / current * 100.0) if current else 0.0

    trs = _true_ranges_pct(valid)
    atr = _atr_pct(valid, 14)
    tr_median = median(trs[-30:]) if trs else 0.0
    tr_p75 = _percentile(trs[-40:], 0.75)
    tr_p90 = _percentile(trs[-40:], 0.90)
    realized_vol = _realized_vol_pct(valid, 30)
    efficiency = _efficiency_ratio(closes, 14)

    inner = valid[-12:]
    outer = valid[-24:]
    swing_high = max((_f(row[2]) for row in inner), default=current)
    swing_low = min((_f(row[3]) for row in inner), default=current)
    swing_high_outer = max((_f(row[2]) for row in outer), default=current)
    swing_low_outer = min((_f(row[3]) for row in outer), default=current)

    atr_floor = max(atr, 0.05)
    normalized_gap = _clip(gap / atr_floor)
    normalized_change = _clip(change / max(atr_floor * math.sqrt(max(1.0, float(candles_back))), 0.10))
    signed_efficiency = efficiency * (1.0 if change > 0 else -1.0 if change < 0 else 0.0)
    trend_strength_signed = _clip(normalized_gap * 0.45 + normalized_change * 0.40 + signed_efficiency * 0.15)

    if trend_strength_signed >= 0.18:
        trend = "BULLISH"
    elif trend_strength_signed <= -0.18:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    robust_bar_range = max(atr, tr_p75, realized_vol * 1.25)
    return {
        "available": True,
        "interval_hours": interval_hours,
        "trend": trend,
        "trend_strength_signed": round(trend_strength_signed, 5),
        "trend_efficiency": round(efficiency, 5),
        "change_pct": round(change, 4),
        "ema_gap_pct": round(gap, 4),
        "atr_pct": round(atr, 4),
        "true_range_median_pct": round(tr_median, 4),
        "true_range_p75_pct": round(tr_p75, 4),
        "true_range_p90_pct": round(tr_p90, 4),
        "realized_vol_pct_per_bar": round(realized_vol, 4),
        "robust_bar_range_pct": round(robust_bar_range, 4),
        "swing_high": round(swing_high, 12),
        "swing_low": round(swing_low, 12),
        "swing_high_outer": round(swing_high_outer, 12),
        "swing_low_outer": round(swing_low_outer, 12),
        "last": round(current, 12),
    }


async def higher_timeframe_context(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    now = time.monotonic()
    cached = _cache.get(symbol)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]

    results = await asyncio.gather(
        binance_client.klines(symbol, interval="4h", limit=90),
        binance_client.klines(symbol, interval="6h", limit=90),
        binance_client.klines(symbol, interval="1d", limit=90),
        return_exceptions=True,
    )
    values: list[list[list[Any]]] = []
    errors: list[str] = []
    for name, result in zip(("4h", "6h", "1d"), results):
        if isinstance(result, Exception):
            values.append([])
            errors.append(f"{name}:{type(result).__name__}:{str(result)[:120]}")
        else:
            values.append(result)

    rows4, rows6, rows1d = values
    frames = {
        "4h": _frame(rows4, 3, 4.0),
        "6h": _frame(rows6, 3, 6.0),
        "1d": _frame(rows1d, 3, 24.0),
    }
    bull = sum(1 for item in frames.values() if item.get("trend") == "BULLISH")
    bear = sum(1 for item in frames.values() if item.get("trend") == "BEARISH")
    if bull >= 2:
        bias = "BULLISH"
    elif bear >= 2:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    available = [item for item in frames.values() if item.get("available")]
    average_strength = mean(_f(item.get("trend_strength_signed")) for item in available) if available else 0.0
    payload = {
        "version": VERSION,
        "symbol": symbol,
        "frames": frames,
        "bias": bias,
        "bullish_frames": bull,
        "bearish_frames": bear,
        "average_trend_strength_signed": round(average_strength, 5),
        "errors": errors,
        "role": "directional context, robust volatility, structural levels and continuation evidence; never an automatic entry by itself",
    }
    _cache[symbol] = (now, payload)
    return payload


async def batch_higher_timeframe_context(symbols: list[str], concurrency: int = 4) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(str(s).upper() for s in symbols if s))
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 8)))
    output: dict[str, dict[str, Any]] = {}

    async def one(symbol: str) -> None:
        async with semaphore:
            try:
                output[symbol] = await higher_timeframe_context(symbol)
            except Exception as exc:
                output[symbol] = {
                    "version": VERSION,
                    "symbol": symbol,
                    "bias": "UNKNOWN",
                    "frames": {},
                    "errors": [f"{type(exc).__name__}:{str(exc)[:160]}"],
                }

    await asyncio.gather(*(one(symbol) for symbol in unique))
    return output


def alignment(direction: str, context: dict[str, Any]) -> dict[str, Any]:
    direction = str(direction or "").upper()
    frames = context.get("frames") if isinstance(context.get("frames"), dict) else {}
    wanted = "BULLISH" if direction == "LONG" else "BEARISH"
    opposite = "BEARISH" if direction == "LONG" else "BULLISH"
    aligned = sum(1 for item in frames.values() if isinstance(item, dict) and item.get("trend") == wanted)
    conflicting = sum(1 for item in frames.values() if isinstance(item, dict) and item.get("trend") == opposite)
    signed_strengths = [_f(item.get("trend_strength_signed")) for item in frames.values() if isinstance(item, dict) and item.get("available")]
    signed = mean(signed_strengths) if signed_strengths else 0.0
    if direction == "SHORT":
        signed *= -1.0
    return {
        "direction": direction,
        "aligned_frames": aligned,
        "conflicting_frames": conflicting,
        "strong_alignment": aligned >= 2,
        "strong_conflict": conflicting == 3,
        "weighted_alignment_strength": round(signed, 5),
        "bias": context.get("bias"),
    }
