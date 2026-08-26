from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_entry_zone_engine(scored: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    """Refine the active structural plan into optimal/acceptable/chase bands.

    The prediction plan is preferred because scanner.py may adopt it only after
    prediction returns. This never widens the source entry range, never moves the
    stop away from invalidation and never creates an entry when the plan is missing.
    """
    direction = str(prediction.get("direction") or scored.get("direction") or "LONG").upper()
    price = _f(scored.get("current_price"))
    source_low = _f(prediction.get("entry_low"), _f(scored.get("entry_low")))
    source_high = _f(prediction.get("entry_high"), _f(scored.get("entry_high")))
    raw_low = min(source_low, source_high)
    raw_high = max(source_low, source_high)
    stop = _f(prediction.get("stop_loss"), _f(scored.get("stop_loss")))
    tp1 = _f(prediction.get("tp1"), _f(scored.get("tp1")))
    trigger = _f(prediction.get("trigger_price"))
    metrics = dict(scored.get("metrics") or {})
    atr_pct = max(_f(metrics.get("atr_pct")), 0.0)
    atr_abs = price * atr_pct / 100.0 if price > 0 and atr_pct > 0 else 0.0

    if raw_low <= 0 or raw_high <= 0 or raw_high <= raw_low:
        return {
            "available": False,
            "reason": "missing_structural_entry_range",
            "version": "entry_zone_v1",
        }

    width = raw_high - raw_low
    midpoint = (raw_low + raw_high) / 2.0
    kind = str(prediction.get("type") or "")
    continuation = kind.startswith("IMPULSO")

    # Continuations favor the trigger-facing side of the structural band so the
    # entry is early without chasing. Reversal setups remain centered because the
    # reclaim/rejection itself is the key condition.
    if continuation and direction == "LONG":
        optimal_low = raw_low
        optimal_high = raw_low + width * 0.55
    elif continuation and direction == "SHORT":
        optimal_low = raw_high - width * 0.55
        optimal_high = raw_high
    else:
        optimal_low = midpoint - width * 0.30
        optimal_high = midpoint + width * 0.30

    optimal_low = _clamp(optimal_low, raw_low, raw_high)
    optimal_high = _clamp(optimal_high, raw_low, raw_high)
    acceptable_low = raw_low
    acceptable_high = raw_high

    # Chase is outside the original plan and never considered an enter zone.
    chase_pad = atr_abs * 0.35 if atr_abs > 0 else width * 1.25
    if direction == "LONG":
        chase_low = raw_high
        chase_high = raw_high + chase_pad
    else:
        chase_low = max(0.0, raw_low - chase_pad)
        chase_high = raw_low

    in_optimal = optimal_low <= price <= optimal_high
    in_acceptable = acceptable_low <= price <= acceptable_high
    in_chase = chase_low <= price <= chase_high and not in_acceptable
    beyond_chase = price > chase_high if direction == "LONG" else price < chase_low

    risk_unit = abs(midpoint - stop)
    rr1_mid = abs(tp1 - midpoint) / risk_unit if risk_unit > 0 and tp1 > 0 else 0.0
    distance_atr = 0.0
    if atr_abs > 0:
        if price < acceptable_low:
            distance_atr = (acceptable_low - price) / atr_abs
        elif price > acceptable_high:
            distance_atr = (price - acceptable_high) / atr_abs

    if in_optimal:
        state = "OPTIMAL"
        action = "ENTER_ZONE"
    elif in_acceptable:
        state = "ACCEPTABLE"
        action = "ENTER_ZONE"
    elif in_chase or beyond_chase:
        state = "CHASE"
        action = "WAIT_RETEST"
    else:
        state = "WAITING"
        action = "WAIT_ZONE"

    return {
        "available": True,
        "version": "entry_zone_v1",
        "direction": direction,
        "state": state,
        "action": action,
        "current_price": round(price, 12),
        "trigger_price": round(trigger, 12) if trigger > 0 else None,
        "optimal_low": round(optimal_low, 12),
        "optimal_high": round(optimal_high, 12),
        "acceptable_low": round(acceptable_low, 12),
        "acceptable_high": round(acceptable_high, 12),
        "chase_low": round(chase_low, 12),
        "chase_high": round(chase_high, 12),
        "in_optimal": in_optimal,
        "in_acceptable": in_acceptable,
        "in_chase": in_chase,
        "beyond_chase": beyond_chase,
        "distance_to_entry_atr": round(distance_atr, 3),
        "rr1_midpoint": round(rr1_mid, 2),
        "rule": "Optimal is the best price-quality slice inside the structural plan; acceptable is still valid; chase means wait for retest. The engine never widens the original plan.",
    }
