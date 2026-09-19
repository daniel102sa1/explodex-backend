from __future__ import annotations

from statistics import median
from typing import Any

VERSION = "structure_retest_strategy_v1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _bars(klines: list[list[Any]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for row in klines:
        if len(row) < 6:
            continue
        volume = _f(row[7]) if len(row) > 7 else _f(row[5])
        out.append({
            "time": _f(row[0]),
            "open": _f(row[1]),
            "high": _f(row[2]),
            "low": _f(row[3]),
            "close": _f(row[4]),
            "volume": max(0.0, volume),
        })
    return [bar for bar in out if min(bar["open"], bar["high"], bar["low"], bar["close"]) > 0]


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _atr(bars: list[dict[str, float]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = bars[0]["close"]
    for bar in bars[1:]:
        tr = max(
            bar["high"] - bar["low"],
            abs(bar["high"] - prev_close),
            abs(bar["low"] - prev_close),
        )
        trs.append(tr)
        prev_close = bar["close"]
    sample = trs[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def _aligned_ema(direction: str, bars: list[dict[str, float]]) -> bool:
    closes = [bar["close"] for bar in bars]
    if len(closes) < 20:
        return False
    ema20 = _ema(closes[-60:], 20)
    ema50 = _ema(closes[-80:], 50)
    if direction == "LONG":
        return ema20 > ema50
    if direction == "SHORT":
        return ema20 < ema50
    return False


def _nearest_htf_target(direction: str, entry: float, bars_1h: list[dict[str, float]]) -> float:
    if not bars_1h or entry <= 0:
        return 0.0
    recent = bars_1h[-60:]
    if direction == "LONG":
        levels = sorted({bar["high"] for bar in recent if bar["high"] > entry * 1.001})
    else:
        levels = sorted({bar["low"] for bar in recent if bar["low"] < entry * 0.999}, reverse=True)
    return levels[0] if levels else 0.0


def detect_structure_retest(
    *,
    direction: str,
    current_price: float,
    klines_15m: list[list[Any]],
    klines_1h: list[list[Any]] | None = None,
) -> dict[str, Any]:
    direction = str(direction or "").upper()
    bars15 = _bars(klines_15m)
    bars1h = _bars(klines_1h or [])
    current_price = _f(current_price)

    if direction not in {"LONG", "SHORT"} or len(bars15) < 40 or current_price <= 0:
        return {
            "version": VERSION,
            "phase": "NO_SETUP",
            "direction": direction,
            "pattern_score": 0.0,
            "paper_candidate": False,
            "reason": "insufficient_structure_data",
        }

    # Ignore the newest bar when possible because it can still be forming.
    closed = bars15[:-1] if len(bars15) >= 45 else bars15
    atr = _atr(closed, 14)
    if atr <= 0:
        return {
            "version": VERSION,
            "phase": "NO_SETUP",
            "direction": direction,
            "pattern_score": 0.0,
            "paper_candidate": False,
            "reason": "atr_unavailable",
        }

    start = max(22, len(closed) - 10)
    breakout: dict[str, Any] | None = None

    for i in range(start, len(closed) - 1):
        prior = closed[max(0, i - 20):i]
        if len(prior) < 12:
            continue
        prior_high = max(bar["high"] for bar in prior)
        prior_low = min(bar["low"] for bar in prior)
        prior_volumes = [bar["volume"] for bar in prior if bar["volume"] > 0]
        volume_median = median(prior_volumes) if prior_volumes else 0.0
        bar = closed[i]
        volume_ratio = bar["volume"] / volume_median if volume_median > 0 else 1.0

        if direction == "LONG":
            crossed = bar["close"] > prior_high + atr * 0.10
            level = prior_high
        else:
            crossed = bar["close"] < prior_low - atr * 0.10
            level = prior_low

        if crossed:
            breakout = {
                "index": i,
                "level": level,
                "bar": bar,
                "prior_high": prior_high,
                "prior_low": prior_low,
                "volume_ratio": volume_ratio,
            }

    if not breakout:
        return {
            "version": VERSION,
            "phase": "NO_SETUP",
            "direction": direction,
            "pattern_score": 0.0,
            "paper_candidate": False,
            "atr": atr,
            "reason": "no_recent_breakout",
        }

    level = _f(breakout["level"])
    breakout_idx = int(breakout["index"])
    after = closed[breakout_idx + 1:]
    retest: dict[str, Any] | None = None

    for offset, bar in enumerate(after, start=breakout_idx + 1):
        if direction == "LONG":
            touched = bar["low"] <= level + atr * 0.30 and bar["high"] >= level - atr * 0.10
            recovered = bar["close"] >= level - atr * 0.08
        else:
            touched = bar["high"] >= level - atr * 0.30 and bar["low"] <= level + atr * 0.10
            recovered = bar["close"] <= level + atr * 0.08
        if touched and recovered:
            retest = {"index": offset, "bar": bar}
            break

    latest = closed[-1]
    ema15_ok = _aligned_ema(direction, closed)
    ema1h_ok = _aligned_ema(direction, bars1h) if len(bars1h) >= 20 else False

    phase = "BREAKOUT_ONLY"
    continuation = False
    higher_low_or_lower_high = False
    structural_level = 0.0
    structural_stop = 0.0
    retest_price = 0.0

    if retest:
        retest_bar = retest["bar"]
        retest_price = retest_bar["low"] if direction == "LONG" else retest_bar["high"]
        subsequent = closed[int(retest["index"]):]
        if direction == "LONG":
            continuation = latest["close"] >= level + atr * 0.18 and latest["close"] > retest_bar["close"]
            recent_lows = [bar["low"] for bar in subsequent[-4:]]
            higher_low_or_lower_high = bool(recent_lows) and min(recent_lows) >= retest_bar["low"] - atr * 0.08
            structural_level = min(recent_lows + [retest_bar["low"]])
            structural_buffer = max(atr * 0.45, current_price * 0.0015)
            structural_stop = structural_level - structural_buffer
        else:
            continuation = latest["close"] <= level - atr * 0.18 and latest["close"] < retest_bar["close"]
            recent_highs = [bar["high"] for bar in subsequent[-4:]]
            higher_low_or_lower_high = bool(recent_highs) and max(recent_highs) <= retest_bar["high"] + atr * 0.08
            structural_level = max(recent_highs + [retest_bar["high"]])
            structural_buffer = max(atr * 0.45, current_price * 0.0015)
            structural_stop = structural_level + structural_buffer

        phase = "RETEST_CONFIRMED" if continuation else "RETESTING"

    score = 20.0
    volume_ratio = _f(breakout.get("volume_ratio"), 1.0)
    score += min(15.0, max(0.0, (volume_ratio - 1.0) * 20.0))
    if retest:
        score += 25.0
    if continuation:
        score += 15.0
    if higher_low_or_lower_high:
        score += 10.0
    if ema15_ok:
        score += 8.0
    if ema1h_ok:
        score += 7.0
    score = min(100.0, score)

    if direction == "LONG":
        entry_low = level - atr * 0.12
        entry_high = level + atr * 0.65
        chase_limit = level + atr * 1.20
    else:
        entry_low = level - atr * 0.65
        entry_high = level + atr * 0.12
        chase_limit = level - atr * 1.20

    lo, hi = sorted((entry_low, entry_high))
    inside_entry = lo <= current_price <= hi
    chased = current_price > chase_limit if direction == "LONG" else current_price < chase_limit

    # Reference entry is the live price only when still inside the original
    # post-retest band. Otherwise use the band midpoint for diagnostic math.
    reference_entry = current_price if inside_entry else (lo + hi) / 2.0
    risk_distance = abs(reference_entry - structural_stop) if structural_stop > 0 else 0.0
    stop_distance_atr = risk_distance / atr if atr > 0 else 999.0

    if risk_distance > 0:
        sign = 1.0 if direction == "LONG" else -1.0
        r25 = reference_entry + sign * risk_distance * 2.5
        r35 = reference_entry + sign * risk_distance * 3.5
        r50 = reference_entry + sign * risk_distance * 5.0
    else:
        r25 = r35 = r50 = 0.0

    htf_target = _nearest_htf_target(direction, reference_entry, bars1h)

    paper_candidate = bool(
        phase == "RETEST_CONFIRMED"
        and score >= 72.0
        and structural_stop > 0
        and stop_distance_atr <= 3.5
        and not chased
    )

    return {
        "version": VERSION,
        "direction": direction,
        "phase": phase,
        "pattern_score": round(score, 2),
        "paper_candidate": paper_candidate,
        "breakout_level": round(level, 12),
        "breakout_time": breakout["bar"]["time"],
        "breakout_volume_ratio": round(volume_ratio, 3),
        "retest_price": round(retest_price, 12) if retest_price else None,
        "retest_confirmed": bool(retest),
        "continuation_confirmed": continuation,
        "higher_low_or_lower_high": higher_low_or_lower_high,
        "ema15_aligned": ema15_ok,
        "ema1h_aligned": ema1h_ok,
        "atr": round(atr, 12),
        "atr_pct": round(atr / current_price * 100.0, 4),
        "entry_low": round(lo, 12),
        "entry_high": round(hi, 12),
        "inside_entry_zone": inside_entry,
        "chase_limit": round(chase_limit, 12),
        "chased": chased,
        "structural_level": round(structural_level, 12) if structural_level else None,
        "structural_stop": round(structural_stop, 12) if structural_stop else None,
        "stop_distance_atr": round(stop_distance_atr, 3),
        "targets": {
            "htf_liquidity": round(htf_target, 12) if htf_target else None,
            "r2_5": round(r25, 12) if r25 else None,
            "r3_5": round(r35, 12) if r35 else None,
            "r5_0": round(r50, 12) if r50 else None,
        },
        "stop_policy": {
            "basis": "retest_swing_plus_atr_buffer",
            "money_loss_does_not_place_stop": True,
            "position_size_must_adapt_to_stop": True,
            "stop_fixed_before_entry": True,
            "stop_never_widens_after_entry": True,
        },
        "reason": "breakout_retest_structure" if paper_candidate else phase.lower(),
    }
