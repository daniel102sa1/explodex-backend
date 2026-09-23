from __future__ import annotations

from statistics import mean
from typing import Any

VERSION = "technical_arsenal_shadow_v3_strat_amd_market_story"
MIN_SHADOW_SAMPLE = 30


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _true_ranges(rows: list[list[Any]]) -> list[float]:
    if len(rows) < 2:
        return []
    out: list[float] = []
    prev_close = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return out


def _atr(rows: list[list[Any]], period: int = 14) -> float:
    trs = _true_ranges(rows)
    if not trs:
        return 0.0
    sample = trs[-min(period, len(trs)):]
    return sum(sample) / len(sample)


def _pivot_points(
    values: list[float],
    *,
    mode: str,
    left: int = 2,
    right: int = 2,
) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for i in range(left, len(values) - right):
        window = values[i - left:i + right + 1]
        value = values[i]
        if mode == "high" and value >= max(window):
            out.append((i, value))
        elif mode == "low" and value <= min(window):
            out.append((i, value))
    return out


def _linear_slope(points: list[tuple[int, float]], scale: float) -> float:
    if len(points) < 2 or scale <= 1e-12:
        return 0.0
    pts = points[-4:]
    xs = [float(x) for x, _ in pts]
    ys = [float(y) for _, y in pts]
    xbar, ybar = mean(xs), mean(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    if denom <= 1e-12:
        return 0.0
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
    return slope / scale


def _candle_features(row: list[Any]) -> dict[str, float]:
    o, h, l, c = _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4])
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "body": body,
        "body_ratio": body / rng,
        "upper_wick": max(0.0, h - max(o, c)),
        "lower_wick": max(0.0, min(o, c) - l),
        "range": rng,
        "bullish": 1.0 if c > o else 0.0,
        "bearish": 1.0 if c < o else 0.0,
    }


def _candlestick_context(rows: list[list[Any]], atr: float) -> dict[str, Any]:
    if len(rows) < 3:
        return {"available": False}
    prev = _candle_features(rows[-2])
    cur = _candle_features(rows[-1])
    patterns: list[dict[str, Any]] = []
    scale = max(atr, cur["range"] * 0.5, 1e-12)

    bullish_engulfing = (
        cur["close"] > cur["open"]
        and prev["close"] < prev["open"]
        and cur["open"] <= prev["close"]
        and cur["close"] >= prev["open"]
        and cur["body"] >= prev["body"] * 1.05
    )
    bearish_engulfing = (
        cur["close"] < cur["open"]
        and prev["close"] > prev["open"]
        and cur["open"] >= prev["close"]
        and cur["close"] <= prev["open"]
        and cur["body"] >= prev["body"] * 1.05
    )
    doji = cur["body_ratio"] <= 0.12
    hammer = (
        cur["lower_wick"] >= max(cur["body"] * 2.0, scale * 0.20)
        and cur["upper_wick"] <= max(cur["body"] * 0.8, scale * 0.12)
    )
    shooting_star = (
        cur["upper_wick"] >= max(cur["body"] * 2.0, scale * 0.20)
        and cur["lower_wick"] <= max(cur["body"] * 0.8, scale * 0.12)
    )

    if bullish_engulfing:
        patterns.append({"name": "BULLISH_ENGULFING", "bias": "LONG", "strength": 78.0})
    if bearish_engulfing:
        patterns.append({"name": "BEARISH_ENGULFING", "bias": "SHORT", "strength": 78.0})
    if hammer:
        patterns.append({"name": "HAMMER_REJECTION", "bias": "LONG", "strength": 66.0})
    if shooting_star:
        patterns.append({"name": "SHOOTING_STAR_REJECTION", "bias": "SHORT", "strength": 66.0})
    if doji:
        patterns.append({"name": "DOJI_INDECISION", "bias": "NEUTRAL", "strength": 55.0})

    return {
        "available": True,
        "patterns": patterns,
        "current_body_ratio": round(cur["body_ratio"], 4),
        "current_upper_wick_atr": round(cur["upper_wick"] / scale, 4),
        "current_lower_wick_atr": round(cur["lower_wick"] / scale, 4),
    }


def _strat_bar(prev: list[Any], cur: list[Any]) -> dict[str, Any]:
    """Classify one candle with the 1/2/3 price-action taxonomy.

    1 = inside candle, 2 = directional break of one side, 3 = outside candle.
    The directional suffix for a 2 candle is derived from the side that broke.
    """
    ph, pl = _f(prev[2]), _f(prev[3])
    ch, cl = _f(cur[2]), _f(cur[3])
    co, cc = _f(cur[1]), _f(cur[4])

    if ch <= ph and cl >= pl:
        kind, direction = "1", "INSIDE"
    elif ch > ph and cl < pl:
        kind, direction = "3", "OUTSIDE"
    elif ch > ph:
        kind, direction = "2", "UP"
    elif cl < pl:
        kind, direction = "2", "DOWN"
    else:
        kind, direction = "1", "INSIDE"

    token = kind
    if kind == "2":
        token = "2U" if direction == "UP" else "2D"
    return {
        "type": kind,
        "direction": direction,
        "token": token,
        "bullish_close": cc > co,
        "bearish_close": cc < co,
    }


def _strat_context(rows: list[list[Any]]) -> dict[str, Any]:
    """Detect the strategic 1/2/3 candle combinations shared by the user.

    This is a transparent price-action heuristic, not a probability model and
    not a standalone entry trigger.
    """
    work = rows[-12:]
    if len(work) < 4:
        return {"available": False}

    bars = [_strat_bar(work[i - 1], work[i]) for i in range(1, len(work))]
    tokens = [str(x["token"]) for x in bars]
    patterns: list[dict[str, Any]] = []

    def add(name: str, bias: str, family: str, strength: float) -> None:
        patterns.append({
            "name": name,
            "bias": bias,
            "family": family,
            "strength": strength,
            "score_is_probability": False,
        })

    last3 = tokens[-3:] if len(tokens) >= 3 else []
    last2 = tokens[-2:] if len(tokens) >= 2 else []
    last4 = tokens[-4:] if len(tokens) >= 4 else []

    mapping3 = {
        ("2U", "1", "2U"): ("2-1-2_CONTINUATION_BULLISH", "LONG", "2-1-2", 74.0),
        ("2D", "1", "2U"): ("2-1-2_REVERSAL_BULLISH", "LONG", "2-1-2", 76.0),
        ("3", "1", "2U"): ("3-1-2_REVERSAL_BULLISH", "LONG", "3-1-2", 72.0),
        ("2D", "1", "2D"): ("2-1-2_CONTINUATION_BEARISH", "SHORT", "2-1-2", 74.0),
        ("2U", "1", "2D"): ("2-1-2_REVERSAL_BEARISH", "SHORT", "2-1-2", 76.0),
        ("3", "1", "2D"): ("3-1-2_REVERSAL_BEARISH", "SHORT", "3-1-2", 72.0),
        ("1", "2D", "2U"): ("1-2-2_RETURN_BULLISH", "LONG", "1-2-2", 70.0),
        ("1", "2U", "2D"): ("1-2-2_RETURN_BEARISH", "SHORT", "1-2-2", 70.0),
        ("3", "2D", "2U"): ("3-2-2_REVERSAL_BULLISH", "LONG", "3-2-2", 70.0),
        ("3", "2U", "2D"): ("3-2-2_REVERSAL_BEARISH", "SHORT", "3-2-2", 70.0),
    }
    if tuple(last3) in mapping3:
        add(*mapping3[tuple(last3)])

    mapping2 = {
        ("2U", "2U"): ("2-2_CONTINUATION_BULLISH", "LONG", "2-2", 68.0),
        ("2D", "2U"): ("2-2_REVERSAL_BULLISH", "LONG", "2-2", 70.0),
        ("2D", "2D"): ("2-2_CONTINUATION_BEARISH", "SHORT", "2-2", 68.0),
        ("2U", "2D"): ("2-2_REVERSAL_BEARISH", "SHORT", "2-2", 70.0),
    }
    if tuple(last2) in mapping2:
        add(*mapping2[tuple(last2)])

    # The notebook also shows a 1-3-1-3 "return" idea. It is exposed only as
    # neutral research context because the screenshot does not define a
    # directional entry rule for it.
    if last4 == ["1", "3", "1", "3"]:
        add("1-3-1-3_RETURN_CONTEXT", "NEUTRAL", "1-3-1-3", 55.0)

    directional = [p for p in patterns if p["bias"] in {"LONG", "SHORT"}]
    long_strength = max([_f(p.get("strength")) for p in directional if p["bias"] == "LONG"] or [0.0])
    short_strength = max([_f(p.get("strength")) for p in directional if p["bias"] == "SHORT"] or [0.0])
    bias = "LONG" if long_strength > short_strength else "SHORT" if short_strength > long_strength else "NEUTRAL"

    return {
        "available": True,
        "taxonomy": {
            "1": "inside_consolidation",
            "2": "directional_break_one_side",
            "3": "outside_break_both_sides",
        },
        "recent_tokens": tokens[-6:],
        "recent_bars": bars[-6:],
        "patterns": patterns,
        "aggregate_bias": bias,
        "score_is_probability": False,
        "can_create_entry": False,
    }


def _liquidity_sweep_context(rows: list[list[Any]], current: float, atr: float) -> dict[str, Any]:
    """OHLC-only stop-run/reclaim context around recent visible extremes."""
    work = rows[-32:]
    if len(work) < 12:
        return {"available": False}
    prior = work[:-1]
    cur = _candle_features(work[-1])
    prior_high = max(_f(r[2]) for r in prior)
    prior_low = min(_f(r[3]) for r in prior)
    scale = max(atr, current * 0.001, 1e-12)
    buffer = max(scale * 0.05, current * 0.00015)

    swept_high = cur["high"] > prior_high + buffer
    swept_low = cur["low"] < prior_low - buffer
    high_reclaimed = swept_high and cur["close"] < prior_high
    low_reclaimed = swept_low and cur["close"] > prior_low

    if high_reclaimed and not low_reclaimed:
        bias = "SHORT"
        event = "HIGH_LIQUIDITY_SWEEP_REJECTED"
    elif low_reclaimed and not high_reclaimed:
        bias = "LONG"
        event = "LOW_LIQUIDITY_SWEEP_RECLAIMED"
    elif high_reclaimed and low_reclaimed:
        bias = "NEUTRAL"
        event = "TWO_SIDED_LIQUIDITY_SWEEP"
    else:
        bias = "NEUTRAL"
        event = "NO_CONFIRMED_SWEEP"

    return {
        "available": True,
        "event": event,
        "bias": bias,
        "prior_high": round(prior_high, 12),
        "prior_low": round(prior_low, 12),
        "swept_high": swept_high,
        "swept_low": swept_low,
        "high_reclaimed": high_reclaimed,
        "low_reclaimed": low_reclaimed,
        "distance_above_high_atr": round(max(0.0, cur["high"] - prior_high) / scale, 4),
        "distance_below_low_atr": round(max(0.0, prior_low - cur["low"]) / scale, 4),
        "ohlc_proxy_only": True,
        "can_create_entry": False,
    }


def _amd_context(rows: list[list[Any]], current: float, atr: float) -> dict[str, Any]:
    """Approximate Accumulation -> Manipulation -> Distribution as market story."""
    work = rows[-36:]
    if len(work) < 20:
        return {"available": False}

    base = work[-18:-4]
    recent = work[-4:]
    base_high = max(_f(r[2]) for r in base)
    base_low = min(_f(r[3]) for r in base)
    base_closes = [_f(r[4]) for r in base]
    base_range = max(base_high - base_low, 1e-12)
    scale = max(atr, current * 0.001, 1e-12)
    base_range_atr = base_range / scale
    close_band_ratio = (max(base_closes) - min(base_closes)) / base_range if base_range > 0 else 1.0
    accumulation = base_range_atr <= 6.0 and close_band_ratio <= 0.78

    buffer = max(scale * 0.05, current * 0.00015)
    recent_high = max(_f(r[2]) for r in recent)
    recent_low = min(_f(r[3]) for r in recent)
    last_close = _f(recent[-1][4])
    midpoint = (base_high + base_low) / 2.0
    swept_high = recent_high > base_high + buffer
    swept_low = recent_low < base_low - buffer
    high_reclaimed = swept_high and last_close < base_high
    low_reclaimed = swept_low and last_close > base_low

    phase = "NO_CLEAR_AMD"
    bias = "NEUTRAL"
    if low_reclaimed and last_close > midpoint:
        phase = "DISTRIBUTION_UP_AFTER_LOW_MANIPULATION"
        bias = "LONG"
    elif high_reclaimed and last_close < midpoint:
        phase = "DISTRIBUTION_DOWN_AFTER_HIGH_MANIPULATION"
        bias = "SHORT"
    elif low_reclaimed:
        phase = "MANIPULATION_BELOW_RANGE"
        bias = "LONG"
    elif high_reclaimed:
        phase = "MANIPULATION_ABOVE_RANGE"
        bias = "SHORT"
    elif accumulation:
        phase = "ACCUMULATION"

    return {
        "available": True,
        "phase": phase,
        "bias": bias,
        "accumulation_detected": accumulation,
        "base_high": round(base_high, 12),
        "base_low": round(base_low, 12),
        "base_range_atr": round(base_range_atr, 4),
        "close_band_ratio": round(close_band_ratio, 4),
        "swept_high": swept_high,
        "swept_low": swept_low,
        "high_reclaimed": high_reclaimed,
        "low_reclaimed": low_reclaimed,
        "heuristic_not_institutional_intent_claim": True,
        "score_is_probability": False,
        "can_create_entry": False,
    }


def _market_structure(rows: list[list[Any]], atr: float) -> dict[str, Any]:
    work = rows[-100:]
    highs = [_f(r[2]) for r in work]
    lows = [_f(r[3]) for r in work]
    closes = [_f(r[4]) for r in work]
    prior_highs = _pivot_points(highs[:-1], mode="high")
    prior_lows = _pivot_points(lows[:-1], mode="low")
    last_close = closes[-1]
    buffer = max(atr * 0.08, last_close * 0.0002)

    structure = "MIXED"
    if len(prior_highs) >= 2 and len(prior_lows) >= 2:
        hh = prior_highs[-1][1] > prior_highs[-2][1]
        hl = prior_lows[-1][1] > prior_lows[-2][1]
        lh = prior_highs[-1][1] < prior_highs[-2][1]
        ll = prior_lows[-1][1] < prior_lows[-2][1]
        if hh and hl:
            structure = "BULLISH_HH_HL"
        elif lh and ll:
            structure = "BEARISH_LH_LL"

    last_swing_high = prior_highs[-1][1] if prior_highs else None
    last_swing_low = prior_lows[-1][1] if prior_lows else None
    bos = None
    choch = None
    if last_swing_high and last_close > last_swing_high + buffer:
        bos = "BULLISH_BOS"
        if structure == "BEARISH_LH_LL":
            choch = "BULLISH_CHOCH"
    if last_swing_low and last_close < last_swing_low - buffer:
        bos = "BEARISH_BOS"
        if structure == "BULLISH_HH_HL":
            choch = "BEARISH_CHOCH"

    low_slope = _linear_slope(prior_lows, max(atr, last_close * 0.001))
    high_slope = _linear_slope(prior_highs, max(atr, last_close * 0.001))
    if low_slope > 0.03 and high_slope > -0.02:
        trendline_bias = "ASCENDING"
    elif high_slope < -0.03 and low_slope < 0.02:
        trendline_bias = "DESCENDING"
    else:
        trendline_bias = "MIXED"

    return {
        "structure": structure,
        "bos": bos,
        "choch": choch,
        "last_swing_high": round(last_swing_high, 12) if last_swing_high else None,
        "last_swing_low": round(last_swing_low, 12) if last_swing_low else None,
        "swing_high_count": len(prior_highs),
        "swing_low_count": len(prior_lows),
        "trendline_context": {
            "bias": trendline_bias,
            "swing_low_slope_atr_per_bar": round(low_slope, 4),
            "swing_high_slope_atr_per_bar": round(high_slope, 4),
        },
    }


def _support_resistance(rows: list[list[Any]], current: float, atr: float) -> dict[str, Any]:
    work = rows[-120:]
    highs = [_f(r[2]) for r in work]
    lows = [_f(r[3]) for r in work]
    piv_high = _pivot_points(highs, mode="high")
    piv_low = _pivot_points(lows, mode="low")
    resistances = sorted({round(v, 12) for _, v in piv_high if v > current})
    supports = sorted({round(v, 12) for _, v in piv_low if v < current}, reverse=True)
    nearest_resistance = resistances[0] if resistances else max(highs)
    nearest_support = supports[0] if supports else min(lows)
    scale = max(atr, current * 0.001)
    return {
        "nearest_support": round(nearest_support, 12),
        "nearest_resistance": round(nearest_resistance, 12),
        "distance_to_support_atr": round((current - nearest_support) / scale, 4),
        "distance_to_resistance_atr": round((nearest_resistance - current) / scale, 4),
        "support_cluster": supports[:4],
        "resistance_cluster": resistances[:4],
    }


def _fibonacci_context(rows: list[list[Any]], current: float, atr: float) -> dict[str, Any]:
    work = rows[-96:]
    highs = [_f(r[2]) for r in work]
    lows = [_f(r[3]) for r in work]
    high = max(highs)
    low = min(lows)
    hi_idx = highs.index(high)
    lo_idx = lows.index(low)
    span = high - low
    if span <= 1e-12:
        return {"available": False}
    if lo_idx < hi_idx:
        impulse = "UP"
        levels = {str(r): high - span * r for r in (0.236, 0.382, 0.5, 0.618, 0.786)}
    else:
        impulse = "DOWN"
        levels = {str(r): low + span * r for r in (0.236, 0.382, 0.5, 0.618, 0.786)}
    scale = max(atr, current * 0.001)
    nearest_ratio, nearest_price = min(levels.items(), key=lambda kv: abs(current - kv[1]))
    return {
        "available": True,
        "impulse": impulse,
        "swing_low": round(low, 12),
        "swing_high": round(high, 12),
        "levels": {k: round(v, 12) for k, v in levels.items()},
        "nearest_level": nearest_ratio,
        "nearest_level_price": round(nearest_price, 12),
        "distance_to_nearest_atr": round(abs(current - nearest_price) / scale, 4),
        "near_retracement": abs(current - nearest_price) <= scale * 0.40,
    }


def _fair_value_gaps(rows: list[list[Any]], current: float, atr: float) -> dict[str, Any]:
    work = rows[-60:]
    gaps: list[dict[str, Any]] = []
    scale = max(atr, current * 0.001)
    for i in range(2, len(work)):
        h0 = _f(work[i - 2][2])
        l0 = _f(work[i - 2][3])
        hi = _f(work[i][2])
        li = _f(work[i][3])
        if li > h0:
            lower, upper = h0, li
            later = work[i + 1:]
            fully_filled = any(_f(r[3]) <= lower for r in later)
            if not fully_filled:
                gaps.append({
                    "type": "BULLISH_FVG",
                    "bias": "LONG",
                    "lower": lower,
                    "upper": upper,
                    "width_atr": (upper - lower) / scale,
                })
        elif hi < l0:
            lower, upper = hi, l0
            later = work[i + 1:]
            fully_filled = any(_f(r[2]) >= upper for r in later)
            if not fully_filled:
                gaps.append({
                    "type": "BEARISH_FVG",
                    "bias": "SHORT",
                    "lower": lower,
                    "upper": upper,
                    "width_atr": (upper - lower) / scale,
                })
    for gap in gaps:
        gap["contains_current"] = gap["lower"] <= current <= gap["upper"]
        gap["distance_atr"] = 0.0 if gap["contains_current"] else min(
            abs(current - gap["lower"]),
            abs(current - gap["upper"]),
        ) / scale
        gap["lower"] = round(gap["lower"], 12)
        gap["upper"] = round(gap["upper"], 12)
        gap["width_atr"] = round(gap["width_atr"], 4)
        gap["distance_atr"] = round(gap["distance_atr"], 4)
    gaps.sort(key=lambda x: (x["distance_atr"], -x["width_atr"]))
    return {
        "open_gap_count": len(gaps),
        "nearest_open_gaps": gaps[:5],
        "near_open_gap": bool(gaps and gaps[0]["distance_atr"] <= 0.50),
    }


def _order_block_context(rows: list[list[Any]], current: float, atr: float) -> dict[str, Any]:
    work = rows[-80:]
    zones: list[dict[str, Any]] = []
    scale = max(atr, current * 0.001)
    for i in range(12, len(work)):
        prev = work[i - 1]
        cur = work[i]
        po, pc = _f(prev[1]), _f(prev[4])
        co, ch, cl, cc = _f(cur[1]), _f(cur[2]), _f(cur[3]), _f(cur[4])
        prior_high = max(_f(r[2]) for r in work[max(0, i - 12):i])
        prior_low = min(_f(r[3]) for r in work[max(0, i - 12):i])
        bullish_displacement = cc > co and (cc - co) >= scale * 1.15 and cc > prior_high + scale * 0.08
        bearish_displacement = cc < co and (co - cc) >= scale * 1.15 and cc < prior_low - scale * 0.08
        if bullish_displacement and pc < po:
            zones.append({
                "type": "DEMAND_ORDER_BLOCK_APPROX",
                "bias": "LONG",
                "lower": _f(prev[3]),
                "upper": _f(prev[2]),
            })
        elif bearish_displacement and pc > po:
            zones.append({
                "type": "SUPPLY_ORDER_BLOCK_APPROX",
                "bias": "SHORT",
                "lower": _f(prev[3]),
                "upper": _f(prev[2]),
            })
    for zone in zones:
        zone["contains_current"] = zone["lower"] <= current <= zone["upper"]
        zone["distance_atr"] = 0.0 if zone["contains_current"] else min(
            abs(current - zone["lower"]), abs(current - zone["upper"])
        ) / scale
        zone["lower"] = round(zone["lower"], 12)
        zone["upper"] = round(zone["upper"], 12)
        zone["distance_atr"] = round(zone["distance_atr"], 4)
    zones.sort(key=lambda x: x["distance_atr"])
    return {
        "approximation": True,
        "zone_count": len(zones),
        "nearest_zones": zones[:4],
        "near_zone": bool(zones and zones[0]["distance_atr"] <= 0.65),
    }


def _stochastic(rows: list[list[Any]], period: int = 14) -> dict[str, Any]:
    if len(rows) < period + 2:
        return {"k": None, "d": None, "state": "NO_DATA"}
    ks: list[float] = []
    for end in range(len(rows) - 2, len(rows) + 1):
        window = rows[max(0, end - period):end]
        if len(window) < period:
            continue
        high = max(_f(r[2]) for r in window)
        low = min(_f(r[3]) for r in window)
        close = _f(window[-1][4])
        k = (close - low) / (high - low) * 100.0 if high > low else 50.0
        ks.append(k)
    if not ks:
        return {"k": None, "d": None, "state": "NO_DATA"}
    k = ks[-1]
    d = mean(ks[-3:])
    state = "OVERSOLD" if k <= 20 and d <= 20 else "OVERBOUGHT" if k >= 80 and d >= 80 else "MID"
    return {"k": round(k, 4), "d": round(d, 4), "state": state}


def _parabolic_sar(rows: list[list[Any]]) -> dict[str, Any]:
    work = rows[-80:]
    if len(work) < 8:
        return {"available": False}
    highs = [_f(r[2]) for r in work]
    lows = [_f(r[3]) for r in work]
    closes = [_f(r[4]) for r in work]
    up = closes[1] >= closes[0]
    sar = lows[0] if up else highs[0]
    ep = highs[0] if up else lows[0]
    af = 0.02
    for i in range(1, len(work)):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if lows[i] < sar:
                up = False
                sar = ep
                ep = lows[i]
                af = 0.02
            elif highs[i] > ep:
                ep = highs[i]
                af = min(0.20, af + 0.02)
        else:
            sar = max(sar, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if highs[i] > sar:
                up = True
                sar = ep
                ep = highs[i]
                af = 0.02
            elif lows[i] < ep:
                ep = lows[i]
                af = min(0.20, af + 0.02)
    return {
        "available": True,
        "sar": round(sar, 12),
        "bias": "LONG" if up else "SHORT",
        "price_above_sar": closes[-1] > sar,
    }


def _supertrend(rows: list[list[Any]], period: int = 10, multiplier: float = 3.0) -> dict[str, Any]:
    work = rows[-100:]
    if len(work) < period + 3:
        return {"available": False}
    trs = _true_ranges(work)
    if len(trs) < period:
        return {"available": False}
    atr_series: list[float] = []
    for i in range(len(work)):
        if i == 0:
            atr_series.append(0.0)
            continue
        sub = trs[max(0, i - period):i]
        atr_series.append(mean(sub) if sub else 0.0)

    final_upper = final_lower = 0.0
    direction = 1
    supertrend = 0.0
    prev_close = _f(work[0][4])
    for i, row in enumerate(work):
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        hl2 = (high + low) / 2.0
        atr = atr_series[i]
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr
        if i == 0:
            final_upper, final_lower = basic_upper, basic_lower
            supertrend = final_lower
            prev_close = close
            continue
        final_upper = basic_upper if basic_upper < final_upper or prev_close > final_upper else final_upper
        final_lower = basic_lower if basic_lower > final_lower or prev_close < final_lower else final_lower
        if direction == 1 and close < final_lower:
            direction = -1
        elif direction == -1 and close > final_upper:
            direction = 1
        supertrend = final_lower if direction == 1 else final_upper
        prev_close = close
    return {
        "available": True,
        "bias": "LONG" if direction == 1 else "SHORT",
        "line": round(supertrend, 12),
        "multiplier": multiplier,
        "period": period,
    }


def _approx_volume_profile(rows: list[list[Any]], bins: int = 24) -> dict[str, Any]:
    work = rows[-96:]
    if not work:
        return {"available": False}
    low = min(_f(r[3]) for r in work)
    high = max(_f(r[2]) for r in work)
    width = high - low
    if width <= 1e-12:
        return {"available": False}
    vols = [0.0] * bins
    for row in work:
        typical = (_f(row[2]) + _f(row[3]) + _f(row[4])) / 3.0
        vol = _f(row[5])
        idx = min(bins - 1, max(0, int((typical - low) / width * bins)))
        vols[idx] += max(0.0, vol)
    total = sum(vols)
    if total <= 1e-12:
        return {"available": False}
    poc_idx = max(range(bins), key=lambda i: vols[i])
    bin_width = width / bins
    centers = [low + (i + 0.5) * bin_width for i in range(bins)]
    ranked = sorted(range(bins), key=lambda i: vols[i], reverse=True)
    acc = 0.0
    va_indices: list[int] = []
    for idx in ranked:
        va_indices.append(idx)
        acc += vols[idx]
        if acc / total >= 0.70:
            break
    positive = [i for i, volume in enumerate(vols) if volume > 0]
    hvn_idx = sorted(positive, key=lambda i: vols[i], reverse=True)[:3]
    lvn_idx = sorted(positive, key=lambda i: vols[i])[:3]
    return {
        "available": True,
        "approximation": True,
        "method": "TYPICAL_PRICE_BINNED_OHLCV_NOT_TICK_VOLUME_PROFILE",
        "poc": round(centers[poc_idx], 12),
        "value_area_low": round(min(centers[i] for i in va_indices), 12),
        "value_area_high": round(max(centers[i] for i in va_indices), 12),
        "high_volume_nodes": [
            {"price": round(centers[i], 12), "volume_share_pct": round(vols[i] / total * 100.0, 4)}
            for i in hvn_idx
        ],
        "low_volume_nodes": [
            {"price": round(centers[i], 12), "volume_share_pct": round(vols[i] / total * 100.0, 4)}
            for i in lvn_idx
        ],
        "bins": bins,
    }



def _rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    if not closes:
        return []
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for a, b in zip(closes, closes[1:]):
        delta = b - a
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))
    avg_gain = mean(gains[:period])
    avg_loss = mean(losses[:period])
    def rsi_value(g: float, l: float) -> float:
        if l <= 1e-12:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)
    out[period] = rsi_value(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = ((period - 1.0) * avg_gain + gains[i]) / period
        avg_loss = ((period - 1.0) * avg_loss + losses[i]) / period
        out[i + 1] = rsi_value(avg_gain, avg_loss)
    return out


def _macd_line_series(closes: list[float]) -> list[float | None]:
    if not closes:
        return []
    e12 = _ema(closes, 12)
    e26 = _ema(closes, 26)
    out: list[float | None] = []
    for i in range(len(closes)):
        if i < 25:
            out.append(None)
        else:
            out.append(e12[i] - e26[i])
    return out


def _stochastic_k_series(rows: list[list[Any]], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(rows)
    for i in range(period - 1, len(rows)):
        window = rows[i - period + 1:i + 1]
        high = max(_f(r[2]) for r in window)
        low = min(_f(r[3]) for r in window)
        close = _f(rows[i][4])
        out[i] = (close - low) / (high - low) * 100.0 if high > low else 50.0
    return out


def _divergence_context(rows: list[list[Any]]) -> dict[str, Any]:
    work = rows[-120:]
    closes = [_f(r[4]) for r in work]
    highs = [_f(r[2]) for r in work]
    lows = [_f(r[3]) for r in work]
    rsi = _rsi_series(closes)
    macd = _macd_line_series(closes)
    stoch = _stochastic_k_series(work)
    piv_high = _pivot_points(highs, mode="high")
    piv_low = _pivot_points(lows, mode="low")
    findings: list[dict[str, Any]] = []

    def compare(points: list[tuple[int, float]], series: list[float | None], name: str, kind: str) -> None:
        if len(points) < 2:
            return
        (i1, p1), (i2, p2) = points[-2], points[-1]
        if i1 >= len(series) or i2 >= len(series):
            return
        v1, v2 = series[i1], series[i2]
        if v1 is None or v2 is None:
            return
        if kind == "HIGH" and p2 > p1 and float(v2) < float(v1):
            findings.append({
                "indicator": name,
                "type": "BEARISH_DIVERGENCE",
                "bias": "SHORT",
                "price_first": round(p1, 12),
                "price_second": round(p2, 12),
                "indicator_first": round(float(v1), 6),
                "indicator_second": round(float(v2), 6),
            })
        elif kind == "LOW" and p2 < p1 and float(v2) > float(v1):
            findings.append({
                "indicator": name,
                "type": "BULLISH_DIVERGENCE",
                "bias": "LONG",
                "price_first": round(p1, 12),
                "price_second": round(p2, 12),
                "indicator_first": round(float(v1), 6),
                "indicator_second": round(float(v2), 6),
            })

    for name, series in (("RSI", rsi), ("MACD", macd), ("STOCHASTIC", stoch)):
        compare(piv_high, series, name, "HIGH")
        compare(piv_low, series, name, "LOW")

    long_count = sum(1 for x in findings if x["bias"] == "LONG")
    short_count = sum(1 for x in findings if x["bias"] == "SHORT")
    bias = "LONG" if long_count > short_count else "SHORT" if short_count > long_count else "NEUTRAL"
    return {
        "available": True,
        "findings": findings[-6:],
        "aggregate_bias": bias,
        "long_count": long_count,
        "short_count": short_count,
        "note": "Classical price-vs-indicator divergence is treated as an early warning, not a standalone reversal trigger.",
    }


def _dynamic_support_resistance(rows: list[list[Any]], current: float, atr: float) -> dict[str, Any]:
    closes = [_f(r[4]) for r in rows]
    ema20 = _ema(closes, 20)[-1]
    ema50 = _ema(closes, 50)[-1]
    scale = max(atr, current * 0.001)
    def relation(value: float) -> str:
        if current > value:
            return "PRICE_ABOVE"
        if current < value:
            return "PRICE_BELOW"
        return "AT_LEVEL"
    return {
        "ema20": round(ema20, 12),
        "ema50": round(ema50, 12),
        "ema20_relation": relation(ema20),
        "ema50_relation": relation(ema50),
        "distance_to_ema20_atr": round(abs(current - ema20) / scale, 4),
        "distance_to_ema50_atr": round(abs(current - ema50) / scale, 4),
        "trend_bias": "LONG" if ema20 > ema50 and current > ema20 else "SHORT" if ema20 < ema50 and current < ema20 else "MIXED",
        "near_dynamic_level": min(abs(current - ema20), abs(current - ema50)) <= scale * 0.35,
    }


def _heikin_ashi_context(rows: list[list[Any]]) -> dict[str, Any]:
    work = rows[-80:]
    if len(work) < 3:
        return {"available": False}
    ha: list[dict[str, float]] = []
    prev_open = (_f(work[0][1]) + _f(work[0][4])) / 2.0
    prev_close = (_f(work[0][1]) + _f(work[0][2]) + _f(work[0][3]) + _f(work[0][4])) / 4.0
    for row in work:
        o, h, l, c = _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4])
        close = (o + h + l + c) / 4.0
        open_ = (prev_open + prev_close) / 2.0
        high = max(h, open_, close)
        low = min(l, open_, close)
        body = abs(close - open_)
        rng = max(high - low, 1e-12)
        ha.append({"open": open_, "high": high, "low": low, "close": close, "body_ratio": body / rng})
        prev_open, prev_close = open_, close

    last = ha[-1]
    direction = "LONG" if last["close"] > last["open"] else "SHORT" if last["close"] < last["open"] else "NEUTRAL"
    run = 0
    for row in reversed(ha):
        row_dir = "LONG" if row["close"] > row["open"] else "SHORT" if row["close"] < row["open"] else "NEUTRAL"
        if row_dir != direction:
            break
        run += 1
    avg_body = mean(x["body_ratio"] for x in ha[-5:])
    return {
        "available": True,
        "bias": direction,
        "same_color_run": run,
        "last_body_ratio": round(last["body_ratio"], 4),
        "avg_body_ratio_5": round(avg_body, 4),
        "trend_strength": "STRONG" if run >= 4 and avg_body >= 0.55 else "MODERATE" if run >= 2 else "WEAK",
        "analysis_only_not_execution_price": True,
    }


def _renko_context(rows: list[list[Any]], brick_pct: float = 1.0) -> dict[str, Any]:
    closes = [_f(r[4]) for r in rows[-180:] if _f(r[4]) > 0]
    if len(closes) < 2:
        return {"available": False}
    brick_rate = max(0.001, brick_pct / 100.0)
    anchor = closes[0]
    bricks: list[str] = []
    for close in closes[1:]:
        while close >= anchor * (1.0 + brick_rate):
            anchor *= 1.0 + brick_rate
            bricks.append("UP")
        while close <= anchor * (1.0 - brick_rate):
            anchor *= 1.0 - brick_rate
            bricks.append("DOWN")
    if not bricks:
        return {
            "available": True,
            "brick_pct": brick_pct,
            "brick_count": 0,
            "bias": "NEUTRAL",
            "current_run": 0,
            "approximation": True,
        }
    last_dir = bricks[-1]
    run = 0
    for direction in reversed(bricks):
        if direction != last_dir:
            break
        run += 1
    return {
        "available": True,
        "brick_pct": brick_pct,
        "brick_count": len(bricks),
        "bias": "LONG" if last_dir == "UP" else "SHORT",
        "current_run": run,
        "approximation": True,
        "method": "CLOSE_BASED_PERCENT_RENKO_NOT_NATIVE_TICK_RENKO",
        "analysis_only_not_execution_price": True,
    }


def _harmonic_source_constraints(rows: list[list[Any]]) -> dict[str, Any]:
    highs = [_f(r[2]) for r in rows[-120:]]
    lows = [_f(r[3]) for r in rows[-120:]]
    pivots = sorted(
        [{"i": i, "type": "H", "price": v} for i, v in _pivot_points(highs, mode="high")]
        + [{"i": i, "type": "L", "price": v} for i, v in _pivot_points(lows, mode="low")],
        key=lambda x: x["i"],
    )
    return {
        "available": len(pivots) >= 5,
        "recent_pivots": pivots[-7:],
        "source_rules_captured": {
            "named_patterns": ["BAT", "BUTTERFLY", "CRAB"],
            "bat_xb_range_stated": [0.382, 0.5],
            "ac_range_stated": [0.382, 0.886],
        },
        "confirmation_status": "INCOMPLETE_SOURCE_RULESET",
        "can_confirm_harmonic_pattern": False,
        "reason": "The shared transcript names additional point-ratio requirements but does not provide the full XA/AB/BC/CD/D completion ratios. ExplodeX will not invent them.",
    }


def _gann_source_context() -> dict[str, Any]:
    return {
        "available": False,
        "research_only": True,
        "reason": "A 45-degree Gann Fan depends on chart price/time scaling (bar ratio). Raw OHLC alone does not preserve the visual chart ratio described in the source.",
        "can_create_entry": False,
    }


def _lunar_phase_policy() -> dict[str, Any]:
    return {
        "implemented_as_signal": False,
        "reason": "The shared source presents lunar phases as an optional confirmation idea, but no market-tested rule or causal evidence was supplied. ExplodeX will not use it as trading evidence.",
    }

def technical_arsenal_registry() -> dict[str, Any]:
    return {
        "version": VERSION,
        "source_scope": "USER_SHARED_PROFESSIONAL_TRADING_ARSENAL_TRANSCRIPT",
        "implemented": [
            "candlestick_engulfing_hammer_shooting_star_doji",
            "strat_1_2_3_candle_sequences_212_312_122_22_322",
            "amd_accumulation_manipulation_distribution_context",
            "ohlc_liquidity_sweep_reclaim_context",
            "support_resistance",
            "trendline_context",
            "stochastic",
            "parabolic_sar",
            "supertrend",
            "approx_volume_profile_poc_value_area_hvn_lvn",
            "fibonacci_retracements",
            "market_structure_hh_hl_lh_ll",
            "break_of_structure",
            "change_of_character",
            "approx_supply_demand_order_blocks",
            "fair_value_gaps",
            "price_indicator_divergence_rsi_macd_stochastic",
            "dynamic_support_resistance_ema20_ema50",
            "heikin_ashi_trend_context",
            "renko_1pct_close_based_approximation",
        ],
        "already_covered_elsewhere": [
            "macd",
            "moving_averages",
            "rsi",
            "professional_confluence_vwap_rvol_cvd_adx_mfi_stochrsi_bollinger_basis",
            "volume",
            "breakout_chart_patterns",
            "reversal_chart_patterns",
            "elliott_wave_structure",
            "liquidity_target_engine",
            "order_flow_spot_futures_delta_orderbook",
            "open_interest_funding_liquidation_context",
            "structural_stop_position_sizing",
            "risk_reward_execution_math",
        ],
        "mentioned_but_not_rule_defined_in_source": ["harmonic_patterns"],
        "partially_defined_in_source": ["harmonic_patterns_bat_butterfly_crab"],
        "not_directly_translated": ["gann_fan_chart_scale_dependent"],
        "excluded_from_signal_logic": ["lunar_phases"],
        "policy": {
            "research_only": True,
            "can_create_entry": False,
            "can_veto_entry": False,
            "can_raise_leverage": False,
            "can_move_live_stop": False,
            "minimum_shadow_sample_before_promotion": MIN_SHADOW_SAMPLE,
            "avoid_double_counting_existing_formula_murphy_elliott_evidence": True,
        },
    }


def build_technical_arsenal_context(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    prediction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [r for r in (snapshot.get("klines") or []) if isinstance(r, list) and len(r) >= 6][-180:]
    if len(rows) < 40:
        return {
            "version": VERSION,
            "available": False,
            "research_only": True,
            "reason": "insufficient_klines",
            "registry": technical_arsenal_registry(),
        }

    current = _f(rows[-1][4])
    atr = _atr(rows)
    structure = _market_structure(rows, atr)
    candle = _candlestick_context(rows, atr)
    strat = _strat_context(rows)
    liquidity_sweep = _liquidity_sweep_context(rows, current, atr)
    amd = _amd_context(rows, current, atr)
    sr = _support_resistance(rows, current, atr)
    fib = _fibonacci_context(rows, current, atr)
    fvg = _fair_value_gaps(rows, current, atr)
    order_blocks = _order_block_context(rows, current, atr)
    stochastic = _stochastic(rows)
    psar = _parabolic_sar(rows)
    supertrend = _supertrend(rows)
    volume_profile = _approx_volume_profile(rows)
    divergence = _divergence_context(rows)
    dynamic_sr = _dynamic_support_resistance(rows, current, atr)
    heikin_ashi = _heikin_ashi_context(rows)
    renko = _renko_context(rows, brick_pct=1.0)
    harmonics = _harmonic_source_constraints(rows)
    gann = _gann_source_context()
    lunar = _lunar_phase_policy()

    long_points = 0.0
    short_points = 0.0
    evidence: list[str] = []

    if structure.get("structure") == "BULLISH_HH_HL":
        long_points += 2.0; evidence.append("bullish_hh_hl")
    elif structure.get("structure") == "BEARISH_LH_LL":
        short_points += 2.0; evidence.append("bearish_lh_ll")
    if structure.get("bos") == "BULLISH_BOS":
        long_points += 1.5; evidence.append("bullish_bos")
    elif structure.get("bos") == "BEARISH_BOS":
        short_points += 1.5; evidence.append("bearish_bos")
    if structure.get("choch") == "BULLISH_CHOCH":
        long_points += 2.0; evidence.append("bullish_choch")
    elif structure.get("choch") == "BEARISH_CHOCH":
        short_points += 2.0; evidence.append("bearish_choch")
    trendline_bias = str((structure.get("trendline_context") or {}).get("bias") or "")
    if trendline_bias == "ASCENDING":
        long_points += 0.75
    elif trendline_bias == "DESCENDING":
        short_points += 0.75

    for pattern in candle.get("patterns") or []:
        if pattern.get("bias") == "LONG":
            long_points += 0.75
        elif pattern.get("bias") == "SHORT":
            short_points += 0.75

    strat_bias = str(strat.get("aggregate_bias") or "NEUTRAL")
    if strat_bias == "LONG":
        long_points += 0.55
        evidence.append("strat_price_action_long")
    elif strat_bias == "SHORT":
        short_points += 0.55
        evidence.append("strat_price_action_short")

    amd_phase = str(amd.get("phase") or "")
    if amd_phase == "DISTRIBUTION_UP_AFTER_LOW_MANIPULATION":
        long_points += 0.65
        evidence.append("amd_distribution_up")
    elif amd_phase == "DISTRIBUTION_DOWN_AFTER_HIGH_MANIPULATION":
        short_points += 0.65
        evidence.append("amd_distribution_down")

    # Liquidity sweeps are exposed for context but intentionally not added to
    # the aggregate score because ExplodeX already has a dedicated sweep and
    # liquidity-target stack elsewhere.
    if stochastic.get("state") == "OVERSOLD":
        long_points += 0.40
    elif stochastic.get("state") == "OVERBOUGHT":
        short_points += 0.40
    if psar.get("bias") == "LONG":
        long_points += 0.50
    elif psar.get("bias") == "SHORT":
        short_points += 0.50
    if supertrend.get("bias") == "LONG":
        long_points += 0.75
    elif supertrend.get("bias") == "SHORT":
        short_points += 0.75

    if divergence.get("aggregate_bias") == "LONG":
        long_points += 0.60; evidence.append("bullish_indicator_divergence")
    elif divergence.get("aggregate_bias") == "SHORT":
        short_points += 0.60; evidence.append("bearish_indicator_divergence")
    if dynamic_sr.get("trend_bias") == "LONG":
        long_points += 0.45
    elif dynamic_sr.get("trend_bias") == "SHORT":
        short_points += 0.45
    if heikin_ashi.get("bias") == "LONG" and heikin_ashi.get("trend_strength") in {"MODERATE", "STRONG"}:
        long_points += 0.55
    elif heikin_ashi.get("bias") == "SHORT" and heikin_ashi.get("trend_strength") in {"MODERATE", "STRONG"}:
        short_points += 0.55
    if renko.get("bias") == "LONG" and int(renko.get("current_run") or 0) >= 2:
        long_points += 0.45
    elif renko.get("bias") == "SHORT" and int(renko.get("current_run") or 0) >= 2:
        short_points += 0.45

    for gap in fvg.get("nearest_open_gaps") or []:
        if gap.get("distance_atr", 99) <= 0.50:
            if gap.get("bias") == "LONG":
                long_points += 0.45
            elif gap.get("bias") == "SHORT":
                short_points += 0.45
            break
    for zone in order_blocks.get("nearest_zones") or []:
        if zone.get("distance_atr", 99) <= 0.65:
            if zone.get("bias") == "LONG":
                long_points += 0.45
            elif zone.get("bias") == "SHORT":
                short_points += 0.45
            break

    edge = long_points - short_points
    if edge >= 1.50:
        bias = "LONG"
    elif edge <= -1.50:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"
    strength = _clip(50.0 + min(50.0, abs(edge) * 8.0))

    return {
        "version": VERSION,
        "available": True,
        "research_only": True,
        "aggregate_bias": bias,
        "evidence_strength_score": round(strength, 2),
        "score_is_probability": False,
        "long_points": round(long_points, 3),
        "short_points": round(short_points, 3),
        "evidence": evidence,
        "candlesticks": candle,
        "strat_price_action": strat,
        "liquidity_sweep": liquidity_sweep,
        "amd_market_story": amd,
        "support_resistance": sr,
        "market_structure": structure,
        "fibonacci": fib,
        "fair_value_gaps": fvg,
        "order_blocks": order_blocks,
        "stochastic": stochastic,
        "parabolic_sar": psar,
        "supertrend": supertrend,
        "volume_profile": volume_profile,
        "divergence": divergence,
        "dynamic_support_resistance": dynamic_sr,
        "heikin_ashi": heikin_ashi,
        "renko": renko,
        "harmonics": harmonics,
        "gann_fan": gann,
        "lunar_phases": lunar,
        "registry": technical_arsenal_registry(),
        "policy": technical_arsenal_registry()["policy"],
        "note": (
            "Shadow-only translation of the shared technical-analysis arsenal into transparent heuristics. "
            "The newly shared 1/2/3 candle combinations and AMD market-story context are included, while liquidity/order-flow/risk logic already covered elsewhere is not double-counted. "
            "It deliberately avoids double-counting MACD/RSI/VWAP, Murphy chart patterns and Elliott, which ExplodeX already computes elsewhere."
        ),
    }
