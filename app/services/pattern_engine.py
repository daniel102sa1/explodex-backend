"""Terra Pattern Engine V2.

PAPER sensor only. Detects simple structural evidence across all Terra
timeframes. A pattern is never an automatic entry.
"""
from __future__ import annotations

from typing import Any


def _ohlc(candle: Any) -> tuple[float, float, float, float]:
    try:
        if isinstance(candle, dict):
            return (
                float(candle.get("open", candle.get("o", 0)) or 0),
                float(candle.get("high", candle.get("h", 0)) or 0),
                float(candle.get("low", candle.get("l", 0)) or 0),
                float(candle.get("close", candle.get("c", 0)) or 0),
            )
        return float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])
    except Exception:
        return 0.0, 0.0, 0.0, 0.0


def detect_patterns(candles: list[Any]) -> dict[str, Any]:
    rows = [_ohlc(c) for c in list(candles or [])[-80:]]
    rows = [r for r in rows if r[3] > 0]
    if len(rows) < 20:
        return {"patterns": [], "score": 0, "signals": [], "engine": "Terra Pattern Engine V2"}

    closes = [r[3] for r in rows]
    highs = [r[1] for r in rows]
    lows = [r[2] for r in rows]
    recent_c = closes[-20:]
    recent_h = highs[-20:]
    recent_l = lows[-20:]
    first_c, second_c = recent_c[:10], recent_c[10:]
    first_h, second_h = recent_h[:10], recent_h[10:]
    first_l, second_l = recent_l[:10], recent_l[10:]

    patterns: list[str] = []
    signals: list[str] = []
    score = 0

    higher_lows = min(second_l) >= min(first_l) * 0.995
    lower_highs = max(second_h) <= max(first_h) * 1.005
    old_range = max(first_h) - min(first_l)
    new_range = max(second_h) - min(second_l)
    compressed = old_range > 0 and new_range < old_range * 0.78

    if higher_lows:
        patterns.append("HIGHER_LOWS")
        signals.append("buyers_defending_higher_levels")
        score += 10
    if lower_highs:
        patterns.append("LOWER_HIGHS")
        signals.append("sellers_defending_lower_levels")
        score += 10
    if compressed:
        patterns.append("PRICE_COMPRESSION")
        signals.append("volatility_contracting")
        score += 10

    # Approximate triangle families from converging structure.
    flat_high = (max(second_h) - min(second_h)) / max(second_c[-1], 1e-12) < 0.012
    flat_low = (max(second_l) - min(second_l)) / max(second_c[-1], 1e-12) < 0.012
    if higher_lows and flat_high and compressed:
        patterns.append("ASCENDING_TRIANGLE")
        signals.append("rising_support_below_flat_resistance")
        score += 20
    if lower_highs and flat_low and compressed:
        patterns.append("DESCENDING_TRIANGLE")
        signals.append("falling_resistance_above_flat_support")
        score += 20
    if higher_lows and lower_highs and compressed:
        patterns.append("SYMMETRIC_TRIANGLE")
        signals.append("converging_range")
        score += 15

    if second_c[-1] > second_c[0]:
        patterns.append("SHORT_TERM_UPTREND")
        signals.append("short_term_momentum_positive")
        score += 8
    elif second_c[-1] < second_c[0]:
        patterns.append("SHORT_TERM_DOWNTREND")
        signals.append("short_term_momentum_negative")
        score += 8

    # Breakout / failed-breakout sensor relative to recent pre-break range.
    prior_high = max(recent_h[:-3])
    prior_low = min(recent_l[:-3])
    last_close = recent_c[-1]
    if last_close > prior_high:
        patterns.append("BULLISH_BREAKOUT")
        signals.append("close_above_recent_resistance")
        score += 12
    elif last_close < prior_low:
        patterns.append("BEARISH_BREAKDOWN")
        signals.append("close_below_recent_support")
        score += 12

    return {
        "patterns": patterns,
        "score": min(score, 100),
        "signals": signals,
        "range_compression": round(new_range / old_range, 4) if old_range > 0 else None,
        "engine": "Terra Pattern Engine V2",
    }


def analyze_pattern_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "1m": detect_patterns(snapshot.get("klines_1m", [])),
        "5m": detect_patterns(snapshot.get("klines", [])),
        "15m": detect_patterns(snapshot.get("klines_15m", [])),
        "1h": detect_patterns(snapshot.get("klines_1h", [])),
        "4h": detect_patterns(snapshot.get("klines_4h", [])),
        "1d": detect_patterns(snapshot.get("klines_1d", [])),
        "1w": detect_patterns(snapshot.get("klines_1w", [])),
        "1M": detect_patterns(snapshot.get("klines_1M", [])),
    }
