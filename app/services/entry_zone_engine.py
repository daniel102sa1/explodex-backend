from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _candle_levels(snapshot: dict[str, Any], direction: str) -> dict[str, Any]:
    klines = snapshot.get("klines") or []
    parsed: list[tuple[float, float, float, float]] = []
    for k in klines[-36:]:
        if not isinstance(k, (list, tuple)) or len(k) < 5:
            continue
        parsed.append((_f(k[1]), _f(k[2]), _f(k[3]), _f(k[4])))
    if len(parsed) < 8:
        return {"available": False}

    highs = [x[1] for x in parsed]
    lows = [x[2] for x in parsed]
    closes = [x[3] for x in parsed]
    current = closes[-1]

    # Recent swing levels are intentionally simple and auditable. They are used
    # only to refine price quality inside the already-valid structural envelope.
    recent_support = max((low for low in lows[-14:-1] if low <= current), default=min(lows[-14:-1]))
    recent_resistance = min((high for high in highs[-14:-1] if high >= current), default=max(highs[-14:-1]))

    last_h, last_l, last_c = highs[-1], lows[-1], closes[-1]
    prev_high = max(highs[-7:-1])
    prev_low = min(lows[-7:-1])
    if direction == "LONG":
        retest_level = prev_high
        retest_confirmed = last_l <= prev_high <= last_h and last_c >= prev_high
    else:
        retest_level = prev_low
        retest_confirmed = last_l <= prev_low <= last_h and last_c <= prev_low

    return {
        "available": True,
        "recent_support": recent_support,
        "recent_resistance": recent_resistance,
        "retest_level": retest_level,
        "retest_confirmed": bool(retest_confirmed),
    }


def build_entry_zone_engine(
    scored: dict[str, Any],
    prediction: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refine the active structural plan into optimal/acceptable/chase bands.

    V2 keeps the original range as the hard envelope, then refines the optimal
    slice using nearby structure, retest evidence and sequential absorption.
    It never widens the source entry range, never moves the stop farther away,
    and never creates an entry when the source plan is missing.
    """
    snapshot = snapshot or {}
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
            "version": "entry_zone_v2",
        }

    width = raw_high - raw_low
    midpoint = (raw_low + raw_high) / 2.0
    kind = str(prediction.get("type") or "")
    continuation = kind.startswith("IMPULSO")

    if continuation and direction == "LONG":
        optimal_low = raw_low
        optimal_high = raw_low + width * 0.55
    elif continuation and direction == "SHORT":
        optimal_low = raw_high - width * 0.55
        optimal_high = raw_high
    else:
        optimal_low = midpoint - width * 0.30
        optimal_high = midpoint + width * 0.30

    structure = _candle_levels(snapshot, direction)
    retest_confirmed = bool(structure.get("retest_confirmed"))
    retest_level = _f(structure.get("retest_level"))

    context = dict(prediction.get("context_engine") or {})
    micro = dict(context.get("microstructure") or {})
    sequential = dict(context.get("sequential_microstructure") or {})
    seq_ready = bool(sequential.get("ready") or micro.get("sequential_ready"))
    absorption = _f(sequential.get("sequential_absorption"), _f(micro.get("sequential_absorption")))
    ofi = _f(sequential.get("ofi"), _f(micro.get("ofi")))
    side = 1.0 if direction == "LONG" else -1.0
    absorption_aligned = seq_ready and absorption * side > 0.5
    ofi_aligned = seq_ready and ofi * side >= 0.18

    # Structure can only tighten/shift the optimal slice within the original band.
    if structure.get("available"):
        anchor = 0.0
        if direction == "LONG":
            anchor = max(_f(structure.get("recent_support")), retest_level if retest_confirmed else 0.0)
        else:
            candidates = [x for x in [_f(structure.get("recent_resistance")), retest_level if retest_confirmed else 0.0] if x > 0]
            anchor = min(candidates) if candidates else 0.0

        if raw_low <= anchor <= raw_high:
            half = width * (0.18 if retest_confirmed else 0.24)
            structure_low = _clamp(anchor - half, raw_low, raw_high)
            structure_high = _clamp(anchor + half, raw_low, raw_high)
            # Intersect when possible. If not, move toward the anchor without
            # widening the original interval.
            intersect_low = max(optimal_low, structure_low)
            intersect_high = min(optimal_high, structure_high)
            if intersect_low < intersect_high:
                optimal_low, optimal_high = intersect_low, intersect_high
            else:
                optimal_low, optimal_high = structure_low, structure_high

    # When sequential microstructure confirms the side, tighten the band modestly
    # around its center. Conflict does not move the band; it lowers quality instead.
    if absorption_aligned or ofi_aligned:
        center = (optimal_low + optimal_high) / 2.0
        half = (optimal_high - optimal_low) * 0.42
        optimal_low = _clamp(center - half, raw_low, raw_high)
        optimal_high = _clamp(center + half, raw_low, raw_high)

    optimal_low = _clamp(optimal_low, raw_low, raw_high)
    optimal_high = _clamp(optimal_high, raw_low, raw_high)
    acceptable_low = raw_low
    acceptable_high = raw_high

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

    quality_score = 50.0
    quality_reasons: list[str] = []
    quality_conflicts: list[str] = []
    if in_optimal:
        quality_score += 24
        quality_reasons.append("precio dentro de la zona óptima")
    elif in_acceptable:
        quality_score += 10
        quality_reasons.append("precio dentro de la zona aceptable")
    if retest_confirmed:
        quality_score += 12
        quality_reasons.append("retest estructural confirmado")
    if absorption_aligned:
        quality_score += 8
        quality_reasons.append("absorción secuencial acompaña")
    if ofi_aligned:
        quality_score += 6
        quality_reasons.append("OFI secuencial acompaña")
    if rr1_mid >= 1.25:
        quality_score += 6
        quality_reasons.append("R:R TP1 suficiente")
    elif 0 < rr1_mid < 0.9:
        quality_score -= 12
        quality_conflicts.append("R:R TP1 pobre")
    if seq_ready and absorption * side < -0.5:
        quality_score -= 14
        quality_conflicts.append("absorción secuencial contraria")
    if seq_ready and ofi * side <= -0.18:
        quality_score -= 10
        quality_conflicts.append("OFI secuencial contrario")
    if in_chase or beyond_chase:
        quality_score -= 35
        quality_conflicts.append("precio fuera del plan: chase")

    quality_score = _clamp(quality_score, 0.0, 100.0)
    if in_optimal and quality_score >= 78:
        state = "OPTIMAL"
        action = "ENTER_ZONE"
        quality_label = "HIGH"
    elif in_acceptable and quality_score >= 62:
        state = "ACCEPTABLE"
        action = "ENTER_ZONE"
        quality_label = "MEDIUM"
    elif in_acceptable:
        state = "ACCEPTABLE_WEAK"
        action = "WAIT_CONFIRMATION"
        quality_label = "LOW"
    elif in_chase or beyond_chase:
        state = "CHASE"
        action = "WAIT_RETEST"
        quality_label = "LOW"
    else:
        state = "WAITING"
        action = "WAIT_ZONE"
        quality_label = "LOW"

    return {
        "available": True,
        "version": "entry_zone_v2",
        "direction": direction,
        "state": state,
        "action": action,
        "quality_score": round(quality_score, 1),
        "quality_label": quality_label,
        "quality_reasons": quality_reasons[:8],
        "quality_conflicts": quality_conflicts[:8],
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
        "structure": structure,
        "microstructure": {
            "sequential_ready": seq_ready,
            "absorption_aligned": absorption_aligned,
            "ofi_aligned": ofi_aligned,
        },
        "rule": "V2 refines the optimal slice with recent structure, retest and sequential microstructure, but never widens the original structural entry plan. Scores are technical quality, not next-trade probability.",
    }
