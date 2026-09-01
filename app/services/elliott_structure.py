from __future__ import annotations

import asyncio
from typing import Any

from app.services.binance import binance_client

VERSION = "elliott_structure_v1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(a: float, b: float) -> float:
    return ((b - a) / a * 100.0) if a else 0.0


def _ratio(num: float, den: float) -> float:
    return abs(num / den) if den else 0.0


def _pivot_points(rows: list[list[Any]], *, left: int = 2, right: int = 2, min_move_pct: float = 0.35) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    for i in range(left, max(left, len(rows) - right)):
        if i + right >= len(rows):
            break
        high = _f(rows[i][2])
        low = _f(rows[i][3])
        if high <= 0 or low <= 0:
            continue
        highs = [_f(rows[j][2]) for j in range(i - left, i + right + 1)]
        lows = [_f(rows[j][3]) for j in range(i - left, i + right + 1)]
        if high >= max(highs):
            raw.append({"index": i, "type": "H", "price": high})
        if low <= min(lows):
            raw.append({"index": i, "type": "L", "price": low})
    raw.sort(key=lambda x: (x["index"], 0 if x["type"] == "L" else 1))

    pivots: list[dict[str, Any]] = []
    for p in raw:
        if not pivots:
            pivots.append(p)
            continue
        prev = pivots[-1]
        if p["type"] == prev["type"]:
            better = (p["type"] == "H" and p["price"] > prev["price"]) or (p["type"] == "L" and p["price"] < prev["price"])
            if better:
                pivots[-1] = p
            continue
        move = abs(_pct(prev["price"], p["price"]))
        if move >= min_move_pct:
            pivots.append(p)
    return pivots[-12:]


def _impulse_candidate(points: list[dict[str, Any]], direction: str) -> dict[str, Any] | None:
    if len(points) < 6:
        return None
    p = points[-6:]
    prices = [x["price"] for x in p]
    types = [x["type"] for x in p]
    if direction == "LONG":
        if types != ["L", "H", "L", "H", "L", "H"]:
            return None
        w1 = prices[1] - prices[0]
        w2 = prices[2] - prices[1]
        w3 = prices[3] - prices[2]
        w4 = prices[4] - prices[3]
        w5 = prices[5] - prices[4]
        rules = {
            "wave2_above_start": prices[2] > prices[0],
            "wave3_exceeds_wave1": prices[3] > prices[1],
            "wave4_above_wave1_top": prices[4] > prices[1],
            "wave5_new_high": prices[5] > prices[3],
            "wave3_not_shortest": abs(w3) >= min(abs(w1), abs(w5)),
        }
        fib2 = _ratio(w2, w1)
        fib3 = _ratio(w3, w1)
        fib4 = _ratio(w4, w3)
        fib5 = _ratio(w5, w1)
        invalidation = prices[4]
        next_target = prices[4] + abs(w1) * 1.618
    else:
        if types != ["H", "L", "H", "L", "H", "L"]:
            return None
        w1 = prices[0] - prices[1]
        w2 = prices[2] - prices[1]
        w3 = prices[2] - prices[3]
        w4 = prices[4] - prices[3]
        w5 = prices[4] - prices[5]
        rules = {
            "wave2_below_start": prices[2] < prices[0],
            "wave3_exceeds_wave1": prices[3] < prices[1],
            "wave4_below_wave1_bottom": prices[4] < prices[1],
            "wave5_new_low": prices[5] < prices[3],
            "wave3_not_shortest": abs(w3) >= min(abs(w1), abs(w5)),
        }
        fib2 = _ratio(w2, w1)
        fib3 = _ratio(w3, w1)
        fib4 = _ratio(w4, w3)
        fib5 = _ratio(w5, w1)
        invalidation = prices[4]
        next_target = prices[4] - abs(w1) * 1.618

    passed = sum(1 for v in rules.values() if v)
    fib_score = 0
    if 0.30 <= fib2 <= 0.82:
        fib_score += 1
    if 1.0 <= fib3 <= 2.8:
        fib_score += 1
    if 0.18 <= fib4 <= 0.65:
        fib_score += 1
    if 0.50 <= fib5 <= 2.0:
        fib_score += 1
    score = min(100.0, passed * 13.0 + fib_score * 7.0 + 7.0)
    return {
        "pattern": "IMPULSE_1_2_3_4_5",
        "direction": direction,
        "score": round(score, 1),
        "score_is_probability": False,
        "wave_prices": {str(i): round(prices[i], 12) for i in range(6)},
        "rules": rules,
        "fib": {
            "wave2_retrace_of_wave1": round(fib2, 3),
            "wave3_extension_of_wave1": round(fib3, 3),
            "wave4_retrace_of_wave3": round(fib4, 3),
            "wave5_vs_wave1": round(fib5, 3),
        },
        "invalidation": round(invalidation, 12),
        "fib_1_618_projection": round(next_target, 12),
    }


def _abc_candidate(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(points) < 4:
        return None
    p = points[-4:]
    prices = [x["price"] for x in p]
    types = [x["type"] for x in p]
    if types == ["H", "L", "H", "L"]:
        direction = "SHORT"
        a = prices[0] - prices[1]
        b = prices[2] - prices[1]
        c = prices[2] - prices[3]
        valid = prices[2] < prices[0] and prices[3] < prices[1]
        target = prices[2] - abs(a) * 1.0
        invalidation = prices[2]
    elif types == ["L", "H", "L", "H"]:
        direction = "LONG"
        a = prices[1] - prices[0]
        b = prices[1] - prices[2]
        c = prices[3] - prices[2]
        valid = prices[2] > prices[0] and prices[3] > prices[1]
        target = prices[2] + abs(a) * 1.0
        invalidation = prices[2]
    else:
        return None
    rb = _ratio(b, a)
    rc = _ratio(c, a)
    score = 35.0 + (25.0 if valid else 0.0)
    if 0.38 <= rb <= 0.82:
        score += 15.0
    if 0.62 <= rc <= 1.62:
        score += 15.0
    return {
        "pattern": "ABC_CORRECTION",
        "direction": direction,
        "score": round(min(score, 100.0), 1),
        "score_is_probability": False,
        "pivot_prices": [round(x, 12) for x in prices],
        "fib": {"b_retrace_of_a": round(rb, 3), "c_vs_a": round(rc, 3)},
        "invalidation": round(invalidation, 12),
        "fib_projection": round(target, 12),
    }


def _wxy_candidate(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(points) < 6:
        return None
    p = points[-6:]
    prices = [x["price"] for x in p]
    types = [x["type"] for x in p]
    if types == ["H", "L", "H", "L", "H", "L"]:
        direction = "SHORT"
        lower_highs = prices[2] < prices[0] and prices[4] < prices[2]
        lower_lows = prices[3] < prices[1] and prices[5] < prices[3]
        valid = lower_highs and lower_lows
    elif types == ["L", "H", "L", "H", "L", "H"]:
        direction = "LONG"
        higher_lows = prices[2] > prices[0] and prices[4] > prices[2]
        higher_highs = prices[3] > prices[1] and prices[5] > prices[3]
        valid = higher_lows and higher_highs
    else:
        return None
    legs = [abs(prices[i + 1] - prices[i]) for i in range(5)]
    symmetry = min(legs[0], legs[2], legs[4]) / max(legs[0], legs[2], legs[4]) if max(legs[0], legs[2], legs[4]) > 0 else 0.0
    score = 45.0 + (30.0 if valid else 0.0) + min(20.0, symmetry * 20.0)
    return {
        "pattern": "W_X_Y_COMPLEX",
        "direction": direction,
        "score": round(min(score, 95.0), 1),
        "score_is_probability": False,
        "pivot_prices": [round(x, 12) for x in prices],
        "leg_symmetry": round(symmetry, 3),
        "valid_sequence": valid,
    }


def analyze_rows(rows: list[list[Any]], timeframe: str) -> dict[str, Any]:
    pivots = _pivot_points(rows)
    candidates = [
        c for c in (
            _impulse_candidate(pivots, "LONG"),
            _impulse_candidate(pivots, "SHORT"),
            _abc_candidate(pivots),
            _wxy_candidate(pivots),
        ) if c is not None
    ]
    candidates.sort(key=lambda x: _f(x.get("score")), reverse=True)
    best = candidates[0] if candidates else None
    clear = bool(best and _f(best.get("score")) >= 68.0)
    return {
        "timeframe": timeframe,
        "pivot_count": len(pivots),
        "recent_pivots": pivots[-8:],
        "best": best if clear else None,
        "alternative": candidates[1] if len(candidates) > 1 else None,
        "status": "CLEAR_COUNT" if clear else "NO_CLEAR_COUNT",
        "candidates": candidates[:4],
    }


async def analyze_symbol_elliott(symbol: str) -> dict[str, Any]:
    symbol = str(symbol).upper()
    r1h, r4h = await asyncio.gather(
        binance_client.klines(symbol, interval="1h", limit=140),
        binance_client.klines(symbol, interval="4h", limit=120),
        return_exceptions=True,
    )
    frames: dict[str, Any] = {}
    errors: list[str] = []
    for name, rows in (("1h", r1h), ("4h", r4h)):
        if isinstance(rows, Exception):
            frames[name] = {"timeframe": name, "status": "UNAVAILABLE", "best": None}
            errors.append(f"{name}:{type(rows).__name__}:{str(rows)[:120]}")
        else:
            frames[name] = analyze_rows(rows, name)

    valid = [
        f["best"] for f in frames.values()
        if isinstance(f, dict) and isinstance(f.get("best"), dict)
    ]
    valid.sort(key=lambda x: _f(x.get("score")), reverse=True)
    best = valid[0] if valid else None
    agreement = False
    if len(valid) >= 2:
        agreement = str(valid[0].get("direction")) == str(valid[1].get("direction"))
    return {
        "version": VERSION,
        "symbol": symbol,
        "frames": frames,
        "best": best,
        "timeframe_agreement": agreement,
        "status": "CLEAR_COUNT" if best else "NO_CLEAR_COUNT",
        "errors": errors,
        "creates_entry": False,
        "changes_direction": False,
        "note": "Elliott is treated as structural evidence, not certainty. Counts are discarded when rules/pivots are not clear.",
    }
