from __future__ import annotations

from statistics import median
from typing import Any

VERSION = "impulse_pullback_confirmation_v1"

POLICY = {
    "paper_only": True,
    "can_create_entry_by_itself": False,
    "can_raise_leverage_by_itself": False,
    "requires_reaction_confirmation": True,
    "do_not_chase": True,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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
    trs: list[float] = []
    prev = bars[0]["close"]
    for bar in bars[1:]:
        trs.append(max(bar["high"] - bar["low"], abs(bar["high"] - prev), abs(bar["low"] - prev)))
        prev = bar["close"]
    sample = trs[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def _body(bar: dict[str, float]) -> float:
    return abs(bar["close"] - bar["open"])


def _bull_reaction(bar: dict[str, float], atr: float) -> bool:
    rng = max(bar["high"] - bar["low"], 1e-12)
    close_pos = (bar["close"] - bar["low"]) / rng
    return bar["close"] > bar["open"] and _body(bar) >= atr * 0.22 and close_pos >= 0.65


def _bear_reaction(bar: dict[str, float], atr: float) -> bool:
    rng = max(bar["high"] - bar["low"], 1e-12)
    close_pos = (bar["close"] - bar["low"]) / rng
    return bar["close"] < bar["open"] and _body(bar) >= atr * 0.22 and close_pos <= 0.35


def _bullish_engulf(prev: dict[str, float], cur: dict[str, float]) -> bool:
    return (
        prev["close"] < prev["open"]
        and cur["close"] > cur["open"]
        and cur["open"] <= prev["close"]
        and cur["close"] >= prev["open"]
    )


def _bearish_engulf(prev: dict[str, float], cur: dict[str, float]) -> bool:
    return (
        prev["close"] > prev["open"]
        and cur["close"] < cur["open"]
        and cur["open"] >= prev["close"]
        and cur["close"] <= prev["open"]
    )


def build_impulse_pullback_confirmation(rows: list[list[Any]]) -> dict[str, Any]:
    """Detect displacement -> structure break -> pending zone -> pullback -> reaction.

    The sequence is intentionally stricter than a simple momentum candle. It does
    not authorize a trade on its own; it contributes bounded timing evidence.
    """
    bars = _bars(rows)
    if len(bars) < 36:
        return {"version": VERSION, "available": False, "reason": "insufficient_klines", "policy": POLICY}

    closed = bars[:-1] if len(bars) >= 45 else bars
    atr = _atr(closed)
    if atr <= 0:
        return {"version": VERSION, "available": False, "reason": "atr_unavailable", "policy": POLICY}

    start = max(14, len(closed) - 16)
    candidate: dict[str, Any] | None = None

    for i in range(start, len(closed) - 1):
        bar = closed[i]
        prior = closed[max(0, i - 12):i]
        if len(prior) < 8:
            continue
        prior_high = max(x["high"] for x in prior)
        prior_low = min(x["low"] for x in prior)
        vols = [x["volume"] for x in prior if x["volume"] > 0]
        med_vol = median(vols) if vols else 0.0
        vol_ratio = bar["volume"] / med_vol if med_vol > 0 else 1.0
        body_atr = _body(bar) / atr

        bullish = (
            bar["close"] > bar["open"]
            and body_atr >= 0.95
            and bar["close"] > prior_high + atr * 0.08
        )
        bearish = (
            bar["close"] < bar["open"]
            and body_atr >= 0.95
            and bar["close"] < prior_low - atr * 0.08
        )
        if not bullish and not bearish:
            continue

        direction = "LONG" if bullish else "SHORT"

        # Prefer a three-candle fair-value-gap around the displacement candle.
        zone_low = zone_high = 0.0
        zone_type = "NONE"
        if i - 1 >= 0 and i + 1 < len(closed):
            before = closed[i - 1]
            after = closed[i + 1]
            if direction == "LONG" and after["low"] > before["high"]:
                zone_low, zone_high = before["high"], after["low"]
                zone_type = "BULLISH_FVG"
            elif direction == "SHORT" and after["high"] < before["low"]:
                zone_low, zone_high = after["high"], before["low"]
                zone_type = "BEARISH_FVG"

        # Fallback: origin candle before displacement, an order-block-like zone.
        if zone_type == "NONE" and i - 1 >= 0:
            origin = closed[i - 1]
            if direction == "LONG" and origin["close"] <= origin["open"]:
                zone_low, zone_high = origin["low"], origin["high"]
                zone_type = "DEMAND_ORIGIN"
            elif direction == "SHORT" and origin["close"] >= origin["open"]:
                zone_low, zone_high = origin["low"], origin["high"]
                zone_type = "SUPPLY_ORIGIN"

        if zone_type == "NONE":
            continue

        candidate = {
            "direction": direction,
            "impulse_index": i,
            "impulse_time": bar["time"],
            "impulse_body_atr": body_atr,
            "impulse_volume_ratio": vol_ratio,
            "broken_level": prior_high if direction == "LONG" else prior_low,
            "zone_low": zone_low,
            "zone_high": zone_high,
            "zone_type": zone_type,
        }

    if not candidate:
        return {
            "version": VERSION,
            "available": True,
            "phase": "NO_SETUP",
            "policy": POLICY,
            "reason": "no_recent_displacement_plus_structure_break",
        }

    direction = candidate["direction"]
    i = int(candidate["impulse_index"])
    zone_low = float(candidate["zone_low"])
    zone_high = float(candidate["zone_high"])
    after = closed[i + 1:]
    touch_index: int | None = None
    invalidated = False

    for j, bar in enumerate(after, start=i + 1):
        touches = bar["low"] <= zone_high and bar["high"] >= zone_low
        if touches and touch_index is None:
            touch_index = j
        if direction == "LONG" and bar["close"] < zone_low - atr * 0.12:
            invalidated = True
            break
        if direction == "SHORT" and bar["close"] > zone_high + atr * 0.12:
            invalidated = True
            break

    reaction_confirmed = False
    reaction_time = None
    reaction_price = None
    confirmation_type = None

    if touch_index is not None and not invalidated:
        for j in range(touch_index, len(closed)):
            cur = closed[j]
            prev = closed[j - 1] if j > 0 else cur
            if direction == "LONG":
                confirmed = _bull_reaction(cur, atr) or _bullish_engulf(prev, cur)
                if confirmed and cur["close"] >= zone_low:
                    reaction_confirmed = True
                    reaction_time = cur["time"]
                    reaction_price = cur["close"]
                    confirmation_type = "BULLISH_ENGULFING" if _bullish_engulf(prev, cur) else "BULLISH_REACTION_CANDLE"
                    break
            else:
                confirmed = _bear_reaction(cur, atr) or _bearish_engulf(prev, cur)
                if confirmed and cur["close"] <= zone_high:
                    reaction_confirmed = True
                    reaction_time = cur["time"]
                    reaction_price = cur["close"]
                    confirmation_type = "BEARISH_ENGULFING" if _bearish_engulf(prev, cur) else "BEARISH_REACTION_CANDLE"
                    break

    current = closed[-1]["close"]
    distance_from_zone_atr = (
        max(0.0, current - zone_high) / atr
        if direction == "LONG"
        else max(0.0, zone_low - current) / atr
    )
    chased = touch_index is None and distance_from_zone_atr > 0.80

    if invalidated:
        phase = "INVALIDATED"
    elif reaction_confirmed:
        phase = "CONFIRMED"
    elif touch_index is not None:
        phase = "ZONE_TOUCHED_WAIT_CONFIRMATION"
    elif chased:
        phase = "WAIT_PULLBACK_NO_CHASE"
    else:
        phase = "WAIT_PULLBACK"

    quality = 42.0
    quality += min(20.0, max(0.0, (float(candidate["impulse_body_atr"]) - 0.95) * 18.0))
    quality += min(12.0, max(0.0, (float(candidate["impulse_volume_ratio"]) - 1.0) * 10.0))
    if candidate["zone_type"] in {"BULLISH_FVG", "BEARISH_FVG"}:
        quality += 8.0
    if touch_index is not None:
        quality += 8.0
    if reaction_confirmed:
        quality += 10.0
    if invalidated:
        quality = min(quality, 25.0)

    structural_invalidation = zone_low - atr * 0.20 if direction == "LONG" else zone_high + atr * 0.20
    entry_low = zone_low
    entry_high = zone_high
    if reaction_price:
        if direction == "LONG":
            entry_high = min(max(zone_high, reaction_price), zone_high + atr * 0.35)
        else:
            entry_low = max(min(zone_low, reaction_price), zone_low - atr * 0.35)

    return {
        "version": VERSION,
        "available": True,
        "direction": direction,
        "phase": phase,
        "quality_score": round(max(0.0, min(100.0, quality)), 1),
        "impulse": {
            "time": candidate["impulse_time"],
            "body_atr": round(float(candidate["impulse_body_atr"]), 3),
            "volume_ratio": round(float(candidate["impulse_volume_ratio"]), 3),
            "broke_structure": True,
            "broken_level": round(float(candidate["broken_level"]), 12),
        },
        "pending_zone": {
            "type": candidate["zone_type"],
            "low": round(zone_low, 12),
            "high": round(zone_high, 12),
            "touched": touch_index is not None,
        },
        "reaction": {
            "confirmed": reaction_confirmed,
            "time": reaction_time,
            "price": round(float(reaction_price), 12) if reaction_price else None,
            "type": confirmation_type,
        },
        "entry_zone": {
            "low": round(entry_low, 12),
            "high": round(entry_high, 12),
        },
        "structural_invalidation": round(structural_invalidation, 12),
        "distance_from_zone_atr": round(distance_from_zone_atr, 3),
        "chased": chased,
        "paper_candidate": bool(phase == "CONFIRMED" and quality >= 68.0),
        "policy": POLICY,
        "rule": (
            "Do not buy/sell the displacement candle late. Wait for price to revisit "
            "the imbalance/origin zone and require an in-direction reaction before using it as timing evidence."
        ),
    }
