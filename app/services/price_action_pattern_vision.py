from __future__ import annotations

from statistics import mean
from typing import Any

VERSION = "price_action_pattern_vision_v1_shadow"

POLICY = {
    "paper_only": True,
    "shadow_only": True,
    "can_create_entry": False,
    "can_change_direction": False,
    "can_raise_leverage": False,
    "requires_external_confirmation": True,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _bars(rows: list[list[Any]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        out.append({
            "time": _f(row[0]),
            "open": _f(row[1]),
            "high": _f(row[2]),
            "low": _f(row[3]),
            "close": _f(row[4]),
            "volume": max(0.0, _f(row[5])),
        })
    return [b for b in out if min(b["open"], b["high"], b["low"], b["close"]) > 0]


def _atr(bars: list[dict[str, float]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    tr: list[float] = []
    prev = bars[0]["close"]
    for bar in bars[1:]:
        tr.append(max(bar["high"] - bar["low"], abs(bar["high"] - prev), abs(bar["low"] - prev)))
        prev = bar["close"]
    sample = tr[-period:]
    return mean(sample) if sample else 0.0


def _trend(closes: list[float], lookback: int = 20) -> str:
    sample = closes[-lookback:]
    if len(sample) < 8 or sample[0] <= 0:
        return "NEUTRAL"
    change = (sample[-1] - sample[0]) / sample[0] * 100.0
    halves = max(2, len(sample) // 2)
    first = mean(sample[:halves])
    second = mean(sample[-halves:])
    if change >= 1.0 and second > first:
        return "UP"
    if change <= -1.0 and second < first:
        return "DOWN"
    return "SIDEWAYS"


def _pivots(bars: list[dict[str, float]], *, window: int = 2, atr: float = 0.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if len(bars) < window * 2 + 1:
        return out
    prominence = max(atr * 0.20, bars[-1]["close"] * 0.0008)
    for i in range(window, len(bars) - window):
        bar = bars[i]
        left = bars[i-window:i]
        right = bars[i+1:i+window+1]
        is_high = all(bar["high"] >= x["high"] for x in left + right)
        is_low = all(bar["low"] <= x["low"] for x in left + right)
        if is_high:
            surrounding = max([x["high"] for x in left + right] or [bar["high"]])
            if bar["high"] - surrounding >= -prominence:
                out.append({"index": i, "time": bar["time"], "type": "H", "price": bar["high"]})
        if is_low:
            surrounding = min([x["low"] for x in left + right] or [bar["low"]])
            if surrounding - bar["low"] >= -prominence:
                out.append({"index": i, "time": bar["time"], "type": "L", "price": bar["low"]})
    out.sort(key=lambda p: p["index"])

    # Compress adjacent same-type pivots to the more extreme one.
    compressed: list[dict[str, Any]] = []
    for pivot in out:
        if compressed and compressed[-1]["type"] == pivot["type"]:
            prev = compressed[-1]
            if pivot["type"] == "H" and pivot["price"] >= prev["price"]:
                compressed[-1] = pivot
            elif pivot["type"] == "L" and pivot["price"] <= prev["price"]:
                compressed[-1] = pivot
        else:
            compressed.append(pivot)
    return compressed


def _slope(points: list[dict[str, Any]]) -> float:
    if len(points) < 2:
        return 0.0
    a, b = points[0], points[-1]
    dx = max(1.0, float(b["index"] - a["index"]))
    return (float(b["price"]) - float(a["price"])) / dx


def _flat(points: list[dict[str, Any]], atr: float) -> bool:
    if len(points) < 2:
        return False
    prices = [float(p["price"]) for p in points[-3:]]
    return max(prices) - min(prices) <= max(atr * 0.55, mean(prices) * 0.0025)


def _chart_patterns(bars: list[dict[str, float]], pivots: list[dict[str, Any]], atr: float) -> list[dict[str, Any]]:
    highs = [p for p in pivots if p["type"] == "H"][-4:]
    lows = [p for p in pivots if p["type"] == "L"][-4:]
    patterns: list[dict[str, Any]] = []
    if len(highs) >= 2 and len(lows) >= 2:
        hs = _slope(highs[-3:])
        ls = _slope(lows[-3:])
        scale = max(atr, bars[-1]["close"] * 0.001, 1e-12)
        hs_n, ls_n = hs / scale, ls / scale

        if _flat(highs) and ls_n > 0.08:
            patterns.append({"name": "ASCENDING_TRIANGLE", "bias": "LONG", "quality": 72.0})
        if _flat(lows) and hs_n < -0.08:
            patterns.append({"name": "DESCENDING_TRIANGLE", "bias": "SHORT", "quality": 72.0})
        if hs_n < -0.06 and ls_n > 0.06:
            patterns.append({"name": "SYMMETRICAL_TRIANGLE", "bias": "NEUTRAL", "quality": 64.0})
        if hs_n > 0.04 and ls_n > 0.04 and ls_n > hs_n * 1.10:
            patterns.append({"name": "RISING_WEDGE", "bias": "SHORT", "quality": 66.0})
        if hs_n < -0.04 and ls_n < -0.04 and abs(hs_n) > abs(ls_n) * 1.10:
            patterns.append({"name": "FALLING_WEDGE", "bias": "LONG", "quality": 66.0})

    # Double top / bottom with separation and ATR-normalized similarity.
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if b["index"] - a["index"] >= 4 and abs(b["price"] - a["price"]) <= max(atr * 0.55, b["price"] * 0.003):
            between_lows = [p for p in pivots if p["type"] == "L" and a["index"] < p["index"] < b["index"]]
            if between_lows:
                neckline = min(p["price"] for p in between_lows)
                patterns.append({"name": "DOUBLE_TOP", "bias": "SHORT", "quality": 70.0, "neckline": neckline})
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if b["index"] - a["index"] >= 4 and abs(b["price"] - a["price"]) <= max(atr * 0.55, b["price"] * 0.003):
            between_highs = [p for p in pivots if p["type"] == "H" and a["index"] < p["index"] < b["index"]]
            if between_highs:
                neckline = max(p["price"] for p in between_highs)
                patterns.append({"name": "DOUBLE_BOTTOM", "bias": "LONG", "quality": 70.0, "neckline": neckline})

    # Head and shoulders / inverse H&S from five alternating pivots.
    recent = pivots[-7:]
    for i in range(max(0, len(recent)-5), len(recent)-4):
        seq = recent[i:i+5]
        types = "".join(p["type"] for p in seq)
        if types == "HLHLH":
            lsh, neck1, head, neck2, rsh = seq
            shoulders_close = abs(lsh["price"] - rsh["price"]) <= max(atr * 0.75, head["price"] * 0.005)
            head_clear = head["price"] >= max(lsh["price"], rsh["price"]) + atr * 0.55
            if shoulders_close and head_clear:
                patterns.append({"name": "HEAD_AND_SHOULDERS", "bias": "SHORT", "quality": 74.0, "neckline": (neck1["price"]+neck2["price"])/2})
        elif types == "LHLHL":
            lsh, neck1, head, neck2, rsh = seq
            shoulders_close = abs(lsh["price"] - rsh["price"]) <= max(atr * 0.75, max(lsh["price"], rsh["price"]) * 0.005)
            head_clear = head["price"] <= min(lsh["price"], rsh["price"]) - atr * 0.55
            if shoulders_close and head_clear:
                patterns.append({"name": "INVERSE_HEAD_AND_SHOULDERS", "bias": "LONG", "quality": 74.0, "neckline": (neck1["price"]+neck2["price"])/2})

    # Flag heuristic: large impulse followed by shallow opposing drift.
    if len(bars) >= 18:
        impulse = bars[-18:-8]
        flag = bars[-8:]
        imp_start, imp_end = impulse[0]["close"], impulse[-1]["close"]
        imp_move = imp_end - imp_start
        imp_atr = abs(imp_move) / max(atr, 1e-12)
        flag_move = flag[-1]["close"] - flag[0]["close"]
        retrace = abs(flag_move) / max(abs(imp_move), 1e-12)
        if imp_move > 0 and imp_atr >= 2.2 and flag_move < 0 and retrace <= 0.55:
            patterns.append({"name": "BULL_FLAG", "bias": "LONG", "quality": 68.0})
        elif imp_move < 0 and imp_atr >= 2.2 and flag_move > 0 and retrace <= 0.55:
            patterns.append({"name": "BEAR_FLAG", "bias": "SHORT", "quality": 68.0})
    return patterns


def _candles(bars: list[dict[str, float]], atr: float) -> list[dict[str, Any]]:
    if len(bars) < 4:
        return []
    closes = [b["close"] for b in bars]
    trend = _trend(closes, 20)
    out: list[dict[str, Any]] = []
    b = bars[-2]  # last fully closed candle
    p = bars[-3]
    p2 = bars[-4]

    body = abs(b["close"] - b["open"])
    span = max(b["high"] - b["low"], 1e-12)
    upper = b["high"] - max(b["open"], b["close"])
    lower = min(b["open"], b["close"]) - b["low"]
    body_ratio = body / span

    if body_ratio <= 0.10:
        out.append({"name": "DOJI", "bias": "NEUTRAL", "quality": 52.0})
    if lower >= body * 2.0 and upper <= max(body * 0.8, atr * 0.08):
        name = "HAMMER" if trend == "DOWN" else "HANGING_MAN" if trend == "UP" else "LONG_LOWER_WICK"
        bias = "LONG" if name == "HAMMER" else "SHORT" if name == "HANGING_MAN" else "NEUTRAL"
        out.append({"name": name, "bias": bias, "quality": 64.0})
    if upper >= body * 2.0 and lower <= max(body * 0.8, atr * 0.08):
        name = "SHOOTING_STAR" if trend == "UP" else "INVERTED_HAMMER" if trend == "DOWN" else "LONG_UPPER_WICK"
        bias = "SHORT" if name == "SHOOTING_STAR" else "LONG" if name == "INVERTED_HAMMER" else "NEUTRAL"
        out.append({"name": name, "bias": bias, "quality": 64.0})

    bullish_engulf = p["close"] < p["open"] and b["close"] > b["open"] and b["open"] <= p["close"] and b["close"] >= p["open"]
    bearish_engulf = p["close"] > p["open"] and b["close"] < b["open"] and b["open"] >= p["close"] and b["close"] <= p["open"]
    if bullish_engulf:
        out.append({"name": "BULLISH_ENGULFING", "bias": "LONG", "quality": 70.0})
    if bearish_engulf:
        out.append({"name": "BEARISH_ENGULFING", "bias": "SHORT", "quality": 70.0})

    p_body_hi, p_body_lo = max(p["open"], p["close"]), min(p["open"], p["close"])
    b_body_hi, b_body_lo = max(b["open"], b["close"]), min(b["open"], b["close"])
    if p["close"] < p["open"] and b["close"] > b["open"] and b_body_hi <= p_body_hi and b_body_lo >= p_body_lo:
        out.append({"name": "BULLISH_HARAMI", "bias": "LONG", "quality": 60.0})
    if p["close"] > p["open"] and b["close"] < b["open"] and b_body_hi <= p_body_hi and b_body_lo >= p_body_lo:
        out.append({"name": "BEARISH_HARAMI", "bias": "SHORT", "quality": 60.0})

    p2_body = abs(p2["close"] - p2["open"])
    p_body = abs(p["close"] - p["open"])
    if p2_body > atr * 0.35 and p_body <= p2_body * 0.45:
        if p2["close"] < p2["open"] and b["close"] > b["open"] and b["close"] > (p2["open"] + p2["close"]) / 2:
            out.append({"name": "MORNING_STAR", "bias": "LONG", "quality": 72.0})
        if p2["close"] > p2["open"] and b["close"] < b["open"] and b["close"] < (p2["open"] + p2["close"]) / 2:
            out.append({"name": "EVENING_STAR", "bias": "SHORT", "quality": 72.0})

    last3 = bars[-4:-1]
    if all(x["close"] > x["open"] for x in last3) and last3[0]["close"] < last3[1]["close"] < last3[2]["close"]:
        out.append({"name": "THREE_WHITE_SOLDIERS", "bias": "LONG", "quality": 68.0})
    if all(x["close"] < x["open"] for x in last3) and last3[0]["close"] > last3[1]["close"] > last3[2]["close"]:
        out.append({"name": "THREE_BLACK_CROWS", "bias": "SHORT", "quality": 68.0})
    return out


def _ratio(value: float, base: float) -> float | None:
    if abs(base) <= 1e-12:
        return None
    return abs(value / base)


def _near(value: float | None, target: float, tol: float = 0.14) -> bool:
    if value is None:
        return False
    return abs(value - target) <= target * tol


def _between(value: float | None, low: float, high: float, pad: float = 0.08) -> bool:
    if value is None:
        return False
    return low * (1-pad) <= value <= high * (1+pad)


def _harmonics(pivots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(pivots) < 5:
        return []
    out: list[dict[str, Any]] = []
    # Evaluate a few recent alternating XABCD candidates.
    for start in range(max(0, len(pivots)-9), len(pivots)-4):
        seq = pivots[start:start+5]
        if len(seq) < 5 or any(seq[i]["type"] == seq[i+1]["type"] for i in range(4)):
            continue
        x, a, b, c, d = [float(p["price"]) for p in seq]
        xa = a - x
        ab = b - a
        bc = c - b
        cd = d - c
        ad = d - a
        ab_xa = _ratio(ab, xa)
        bc_ab = _ratio(bc, ab)
        cd_bc = _ratio(cd, bc)
        ad_xa = _ratio(ad, xa)
        bias = "LONG" if seq[-1]["type"] == "L" else "SHORT"

        candidates: list[tuple[str, bool, float]] = [
            ("GARTLEY", _near(ab_xa, 0.618) and _between(bc_ab, 0.382, 0.886) and _between(cd_bc, 1.272, 1.618) and _near(ad_xa, 0.786, 0.16), 78.0),
            ("BAT", _between(ab_xa, 0.382, 0.50) and _between(bc_ab, 0.382, 0.886) and _between(cd_bc, 1.618, 2.618) and _near(ad_xa, 0.886, 0.14), 76.0),
            ("BUTTERFLY", _near(ab_xa, 0.786, 0.14) and _between(bc_ab, 0.382, 0.886) and _between(cd_bc, 1.618, 2.618) and _between(ad_xa, 1.27, 1.618, 0.10), 76.0),
            ("CRAB", _between(ab_xa, 0.382, 0.618) and _between(bc_ab, 0.382, 0.886) and _between(cd_bc, 2.24, 3.618) and _near(ad_xa, 1.618, 0.14), 74.0),
        ]
        ratios = {
            "AB_XA": round(ab_xa, 4) if ab_xa is not None else None,
            "BC_AB": round(bc_ab, 4) if bc_ab is not None else None,
            "CD_BC": round(cd_bc, 4) if cd_bc is not None else None,
            "AD_XA": round(ad_xa, 4) if ad_xa is not None else None,
        }
        for name, matched, quality in candidates:
            if matched:
                out.append({
                    "name": name,
                    "bias": bias,
                    "quality": quality,
                    "ratios": ratios,
                    "points": [{"label": label, "index": seq[i]["index"], "price": seq[i]["price"]} for i, label in enumerate("XABCD")],
                    "completion_point": d,
                })
    return out[-3:]


def _levels(pivots: list[dict[str, Any]], atr: float, current: float) -> dict[str, Any]:
    threshold = max(atr * 0.45, current * 0.002)
    clusters: list[list[float]] = []
    for p in pivots[-16:]:
        price = float(p["price"])
        placed = False
        for cluster in clusters:
            if abs(price - mean(cluster)) <= threshold:
                cluster.append(price)
                placed = True
                break
        if not placed:
            clusters.append([price])
    levels = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        level = mean(cluster)
        kind = "SUPPORT" if level < current else "RESISTANCE"
        levels.append({
            "level": round(level, 12),
            "type": kind,
            "touches": len(cluster),
            "distance_pct": round((level-current)/current*100.0, 4) if current > 0 else None,
        })
    levels.sort(key=lambda x: abs(float(x.get("distance_pct") or 999)))
    return {"levels": levels[:8], "cluster_threshold": round(threshold, 12)}


def _market_cycle(bars: list[dict[str, float]], levels: dict[str, Any], atr: float) -> dict[str, Any]:
    if len(bars) < 50:
        return {"state": "UNAVAILABLE", "confidence": 0.0}
    recent = bars[-32:]
    prior = bars[-64:-32] if len(bars) >= 64 else bars[:-32]
    recent_range = max(b["high"] for b in recent) - min(b["low"] for b in recent)
    prior_range = (max(b["high"] for b in prior) - min(b["low"] for b in prior)) if prior else recent_range
    closes = [b["close"] for b in bars]
    prior_trend = _trend(closes[:-12], 30)
    current_trend = _trend(closes, 20)
    range_atr = recent_range / max(atr, 1e-12)
    vols = [b["volume"] for b in recent if b["volume"] > 0]
    vol_ratio = (mean(vols[-8:]) / mean(vols[:-8])) if len(vols) >= 16 and mean(vols[:-8]) > 0 else 1.0

    state = "MARKUP" if current_trend == "UP" else "MARKDOWN" if current_trend == "DOWN" else "RANGE"
    confidence = 55.0
    if current_trend == "SIDEWAYS" and range_atr <= 6.0:
        if prior_trend == "DOWN":
            state = "ACCUMULATION_CANDIDATE"
            confidence = 62.0
        elif prior_trend == "UP":
            state = "DISTRIBUTION_CANDIDATE"
            confidence = 62.0
    if prior_range > 0 and recent_range < prior_range * 0.72:
        confidence += 6.0
    if vol_ratio >= 1.25 and state in {"ACCUMULATION_CANDIDATE", "DISTRIBUTION_CANDIDATE"}:
        confidence += 4.0
    return {
        "state": state,
        "confidence": round(_clip(confidence), 1),
        "range_atr": round(range_atr, 3),
        "recent_vs_prior_range_ratio": round(recent_range/prior_range, 4) if prior_range > 0 else None,
        "recent_volume_ratio": round(vol_ratio, 4),
        "note": "Accumulation/distribution is a structural hypothesis, not proof of smart-money activity.",
    }


def detect_price_action_patterns(rows: list[list[Any]]) -> dict[str, Any]:
    bars = _bars(rows)
    if len(bars) < 40:
        return {
            "version": VERSION,
            "available": False,
            "reason": "insufficient_klines",
            "policy": POLICY,
        }
    # Ignore currently forming candle when possible.
    closed = bars[:-1] if len(bars) > 45 else bars
    atr = _atr(closed)
    pivots = _pivots(closed, window=2, atr=atr)
    current = closed[-1]["close"]
    chart = _chart_patterns(closed, pivots, atr)
    candles = _candles(closed, atr)
    harmonics = _harmonics(pivots)
    levels = _levels(pivots, atr, current)
    cycle = _market_cycle(closed, levels, atr)

    combined = chart + candles + harmonics
    combined.sort(key=lambda x: float(x.get("quality") or 0), reverse=True)
    long_score = sum(float(x.get("quality") or 0) for x in combined if x.get("bias") == "LONG")
    short_score = sum(float(x.get("quality") or 0) for x in combined if x.get("bias") == "SHORT")
    aggregate = "LONG" if long_score > short_score * 1.15 and long_score >= 60 else "SHORT" if short_score > long_score * 1.15 and short_score >= 60 else "NEUTRAL"

    return {
        "version": VERSION,
        "available": True,
        "policy": POLICY,
        "atr": round(atr, 12),
        "pivot_count": len(pivots),
        "pivots": pivots[-14:],
        "support_resistance": levels,
        "chart_patterns": chart,
        "candlestick_patterns": candles,
        "harmonic_patterns": harmonics,
        "market_cycle": cycle,
        "aggregate_bias": aggregate,
        "evidence_score": round(_clip(max(long_score, short_score) / max(1, len(combined))), 1) if combined else 0.0,
        "top_patterns": combined[:8],
        "note": (
            "Geometric price-action detector. Pattern presence is evidence only; "
            "breakout/retest, volume, order flow, derivatives and risk geometry must confirm execution."
        ),
    }
