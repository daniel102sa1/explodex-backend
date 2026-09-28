from __future__ import annotations

"""Terra Pattern Engine V2.

Deterministic technical-pattern sensor for Terra. It never opens a trade by
itself; it converts multi-timeframe OHLCV into structured evidence.
"""

from typing import Any

VERSION = "terra_pattern_engine_v2"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bars(candles: list[Any]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for row in candles or []:
        try:
            if isinstance(row, dict):
                out.append({
                    "open": _f(row.get("open", row.get("o"))),
                    "high": _f(row.get("high", row.get("h"))),
                    "low": _f(row.get("low", row.get("l"))),
                    "close": _f(row.get("close", row.get("c"))),
                    "volume": _f(row.get("volume", row.get("v"))),
                })
            elif isinstance(row, (list, tuple)) and len(row) >= 6:
                out.append({
                    "open": _f(row[1]),
                    "high": _f(row[2]),
                    "low": _f(row[3]),
                    "close": _f(row[4]),
                    "volume": _f(row[5]),
                })
        except Exception:
            continue
    return [b for b in out if min(b["high"], b["low"], b["close"]) > 0]


def _slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den <= 0 or abs(mean_y) <= 1e-12:
        return 0.0
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    return (num / den) / abs(mean_y)


def _atr_pct(bars: list[dict[str, float]], length: int = 14) -> float:
    sample = bars[-max(2, length):]
    if len(sample) < 2:
        return 0.0
    trs: list[float] = []
    prev = sample[0]["close"]
    for b in sample[1:]:
        trs.append(max(
            b["high"] - b["low"],
            abs(b["high"] - prev),
            abs(b["low"] - prev),
        ))
        prev = b["close"]
    close = sample[-1]["close"]
    return (sum(trs) / max(1, len(trs))) / close * 100.0 if close > 0 else 0.0


def _volume_ratio(bars: list[dict[str, float]]) -> float:
    if len(bars) < 20:
        return 1.0
    recent = [b["volume"] for b in bars[-5:]]
    prior = [b["volume"] for b in bars[-20:-5]]
    prior_avg = sum(prior) / max(1, len(prior))
    return (sum(recent) / max(1, len(recent))) / prior_avg if prior_avg > 0 else 1.0


def detect_patterns(candles: list[Any]) -> dict[str, Any]:
    bars = _bars(candles)[-100:]
    if len(bars) < 24:
        return {
            "version": VERSION,
            "patterns": [],
            "signals": [],
            "long_score": 0.0,
            "short_score": 0.0,
            "net_score": 0.0,
            "quality": 0.0,
            "atr_pct": 0.0,
            "volume_ratio": 1.0,
        }

    recent = bars[-24:]
    highs = [b["high"] for b in recent]
    lows = [b["low"] for b in recent]
    closes = [b["close"] for b in recent]
    high_slope = _slope(highs)
    low_slope = _slope(lows)
    close_slope = _slope(closes)
    current = closes[-1]
    old_range = max(highs[:12]) - min(lows[:12])
    new_range = max(highs[12:]) - min(lows[12:])
    contraction = old_range > 0 and new_range <= old_range * 0.82
    atr_pct = _atr_pct(bars)
    volume_ratio = _volume_ratio(bars)

    patterns: list[dict[str, Any]] = []
    signals: list[str] = []
    long_score = 0.0
    short_score = 0.0

    def add(name: str, direction: str, weight: float, reason: str) -> None:
        nonlocal long_score, short_score
        patterns.append({"name": name, "direction": direction, "weight": weight, "reason": reason})
        signals.append(reason)
        if direction == "LONG":
            long_score += weight
        elif direction == "SHORT":
            short_score += weight

    flat_threshold = 0.00035
    trend_threshold = 0.00035

    if low_slope > trend_threshold and abs(high_slope) <= flat_threshold and contraction:
        add("ASCENDING_TRIANGLE", "LONG", 18, "rising_lows_flat_resistance_compression")
    if high_slope < -trend_threshold and abs(low_slope) <= flat_threshold and contraction:
        add("DESCENDING_TRIANGLE", "SHORT", 18, "falling_highs_flat_support_compression")
    if high_slope < -trend_threshold and low_slope > trend_threshold and contraction:
        bias = "LONG" if close_slope > 0 else "SHORT" if close_slope < 0 else "NEUTRAL"
        if bias == "LONG":
            add("SYMMETRICAL_TRIANGLE", "LONG", 10, "converging_range_with_positive_close_slope")
        elif bias == "SHORT":
            add("SYMMETRICAL_TRIANGLE", "SHORT", 10, "converging_range_with_negative_close_slope")
        else:
            patterns.append({"name": "SYMMETRICAL_TRIANGLE", "direction": "NEUTRAL", "weight": 6, "reason": "converging_range_neutral"})

    if high_slope > trend_threshold and low_slope > trend_threshold:
        add("HIGHER_HIGHS_HIGHER_LOWS", "LONG", 12, "market_structure_uptrend")
    if high_slope < -trend_threshold and low_slope < -trend_threshold:
        add("LOWER_HIGHS_LOWER_LOWS", "SHORT", 12, "market_structure_downtrend")

    if contraction and high_slope > 0 and low_slope > 0 and high_slope < low_slope:
        add("RISING_WEDGE", "SHORT", 9, "rising_wedge_contraction")
    if contraction and high_slope < 0 and low_slope < 0 and high_slope < low_slope:
        add("FALLING_WEDGE", "LONG", 9, "falling_wedge_contraction")

    first = bars[-24:-12]
    second = bars[-12:]
    first_move = (first[-1]["close"] - first[0]["close"]) / first[0]["close"] if first[0]["close"] else 0.0
    second_move = (second[-1]["close"] - second[0]["close"]) / second[0]["close"] if second[0]["close"] else 0.0
    if first_move > max(0.015, atr_pct / 100.0 * 2.0) and second_move < 0 and abs(second_move) < abs(first_move) * 0.55:
        add("BULL_FLAG", "LONG", 12, "impulse_then_controlled_pullback")
    if first_move < -max(0.015, atr_pct / 100.0 * 2.0) and second_move > 0 and abs(second_move) < abs(first_move) * 0.55:
        add("BEAR_FLAG", "SHORT", 12, "selloff_then_controlled_bounce")

    a = bars[-36:-18] if len(bars) >= 36 else bars[-24:-12]
    b = bars[-18:] if len(bars) >= 36 else bars[-12:]
    if a and b:
        top1, top2 = max(x["high"] for x in a), max(x["high"] for x in b)
        bot1, bot2 = min(x["low"] for x in a), min(x["low"] for x in b)
        tol = max(atr_pct / 100.0 * 1.5, 0.006)
        if max(top1, top2) > 0 and abs(top1 - top2) / max(top1, top2) <= tol and current < min(top1, top2) * (1 - tol * 0.25):
            add("DOUBLE_TOP", "SHORT", 10, "repeated_resistance_with_rejection")
        if max(bot1, bot2) > 0 and abs(bot1 - bot2) / max(bot1, bot2) <= tol and current > max(bot1, bot2) * (1 + tol * 0.25):
            add("DOUBLE_BOTTOM", "LONG", 10, "repeated_support_with_recovery")

    if len(bars) >= 26:
        prior = bars[-26:-3]
        resistance = max(x["high"] for x in prior)
        support = min(x["low"] for x in prior)
        penultimate = bars[-2]
        last = bars[-1]
        buffer = max(0.0015, atr_pct / 100.0 * 0.25)
        if penultimate["close"] > resistance * (1 + buffer) and last["low"] <= resistance * (1 + buffer * 1.5) and last["close"] > resistance:
            add("BREAKOUT_RETEST_LONG", "LONG", 16, "breakout_then_retest_holds")
        elif penultimate["high"] > resistance * (1 + buffer) and last["close"] < resistance:
            add("FALSE_BREAKOUT_UP", "SHORT", 14, "failed_breakout_above_resistance")
        if penultimate["close"] < support * (1 - buffer) and last["high"] >= support * (1 - buffer * 1.5) and last["close"] < support:
            add("BREAKDOWN_RETEST_SHORT", "SHORT", 16, "breakdown_then_retest_fails")
        elif penultimate["low"] < support * (1 - buffer) and last["close"] > support:
            add("FALSE_BREAKDOWN", "LONG", 14, "failed_breakdown_below_support")

    if volume_ratio >= 1.35:
        if close_slope > 0:
            long_score += 6
            signals.append("expanding_volume_supports_upside")
        elif close_slope < 0:
            short_score += 6
            signals.append("expanding_volume_supports_downside")

    quality = min(100.0, max(long_score, short_score))
    return {
        "version": VERSION,
        "patterns": patterns,
        "signals": signals,
        "long_score": round(min(100.0, long_score), 2),
        "short_score": round(min(100.0, short_score), 2),
        "net_score": round(long_score - short_score, 2),
        "quality": round(quality, 2),
        "atr_pct": round(atr_pct, 4),
        "volume_ratio": round(volume_ratio, 4),
        "structure": {
            "high_slope": round(high_slope, 7),
            "low_slope": round(low_slope, 7),
            "close_slope": round(close_slope, 7),
            "range_contraction": bool(contraction),
        },
    }


def analyze_pattern_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "1m": snapshot.get("klines_1m", []),
        "5m": snapshot.get("klines", []),
        "15m": snapshot.get("klines_15m", []),
        "1h": snapshot.get("klines_1h", []),
        "4h": snapshot.get("klines_4h", []),
        "1d": snapshot.get("klines_1d", []),
        "1w": snapshot.get("klines_1w", []),
        "1M": snapshot.get("klines_1M", []),
    }
    weights = {"1m": 0.6, "5m": 0.8, "15m": 1.0, "1h": 1.3, "4h": 1.7, "1d": 2.0, "1w": 2.3, "1M": 2.5}
    frames: dict[str, Any] = {}
    weighted_long = 0.0
    weighted_short = 0.0
    total_weight = 0.0
    directional_frames = 0
    aligned_long = 0
    aligned_short = 0
    top_patterns: list[dict[str, Any]] = []

    for tf, candles in mapping.items():
        analysis = detect_patterns(candles)
        frames[tf] = analysis
        w = weights[tf]
        weighted_long += analysis["long_score"] * w
        weighted_short += analysis["short_score"] * w
        total_weight += w
        if analysis["long_score"] > analysis["short_score"] and analysis["long_score"] >= 8:
            aligned_long += 1
            directional_frames += 1
        elif analysis["short_score"] > analysis["long_score"] and analysis["short_score"] >= 8:
            aligned_short += 1
            directional_frames += 1
        for p in analysis["patterns"]:
            top_patterns.append({"timeframe": tf, **p})

    denom = max(1e-9, total_weight)
    long_score = min(100.0, weighted_long / denom)
    short_score = min(100.0, weighted_short / denom)
    if long_score > short_score + 5:
        bias = "LONG"
        aligned = aligned_long
    elif short_score > long_score + 5:
        bias = "SHORT"
        aligned = aligned_short
    else:
        bias = "NEUTRAL"
        aligned = max(aligned_long, aligned_short)

    agreement_pct = (aligned / directional_frames * 100.0) if directional_frames else 0.0
    top_patterns.sort(key=lambda x: float(x.get("weight") or 0), reverse=True)

    return {
        "version": VERSION,
        "bias": bias,
        "long_score": round(long_score, 2),
        "short_score": round(short_score, 2),
        "score": round(max(long_score, short_score), 2),
        "agreement_pct": round(agreement_pct, 2),
        "directional_timeframes": directional_frames,
        "aligned_long_timeframes": aligned_long,
        "aligned_short_timeframes": aligned_short,
        "top_patterns": top_patterns[:16],
        "timeframes": frames,
        "policy": "pattern_is_evidence_not_entry_trigger",
    }
