from __future__ import annotations

from statistics import mean
from typing import Any

VERSION = "sarpon_compression_priority_v1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _range_pct(rows: list[list[Any]]) -> float:
    if not rows:
        return 0.0
    highs = [_f(r[2]) for r in rows]
    lows = [_f(r[3]) for r in rows]
    close = _f(rows[-1][4])
    if close <= 0:
        return 0.0
    return (max(highs) - min(lows)) / close * 100.0


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


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _block_extrema(values: list[float], *, blocks: int = 4, block_size: int = 3, mode: str = "min") -> list[float]:
    needed = blocks * block_size
    if len(values) < needed:
        return []
    data = values[-needed:]
    result: list[float] = []
    for i in range(blocks):
        block = data[i * block_size:(i + 1) * block_size]
        result.append(min(block) if mode == "min" else max(block))
    return result


def _rising(values: list[float], tolerance: float = 0.0015) -> bool:
    return (
        len(values) >= 4
        and values[1] >= values[0] * (1 - tolerance)
        and values[2] >= values[1] * (1 - tolerance)
        and values[3] > values[2]
    )


def _falling(values: list[float], tolerance: float = 0.0015) -> bool:
    return (
        len(values) >= 4
        and values[1] <= values[0] * (1 + tolerance)
        and values[2] <= values[1] * (1 + tolerance)
        and values[3] < values[2]
    )


def build_sarpon_compression_context(
    klines: list[list[Any]],
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect early compression pressure before the breakout.

    This layer intentionally gives more importance to a clean squeeze/pressure
    structure than to late expansion confirmation. It is based on the user's
    observed SARPON rule that compression is especially important. It does not
    claim to reproduce any private or undisclosed SARPON method.
    """
    metrics = dict(metrics or {})
    rows = [r for r in (klines or []) if isinstance(r, list) and len(r) >= 8][-96:]
    if len(rows) < 48:
        return {
            "version": VERSION,
            "available": False,
            "reason": "insufficient_klines",
            "priority_rule": "compression_is_early_evidence_not_entry_by_itself",
        }

    closes = [_f(r[4]) for r in rows]
    highs = [_f(r[2]) for r in rows]
    lows = [_f(r[3]) for r in rows]
    opens = [_f(r[1]) for r in rows]
    volumes = [_f(r[7]) for r in rows]
    current = closes[-1]

    range_48 = _range_pct(rows[-48:])
    range_24 = _range_pct(rows[-24:])
    range_12 = _range_pct(rows[-12:])
    range_6 = _range_pct(rows[-6:])

    ratio_12_48 = range_12 / range_48 if range_48 > 1e-12 else 1.0
    ratio_6_24 = range_6 / range_24 if range_24 > 1e-12 else 1.0
    ratio_24_48 = range_24 / range_48 if range_48 > 1e-12 else 1.0

    trs = _true_ranges(rows)
    recent_tr = mean(trs[-6:]) if len(trs) >= 6 else 0.0
    prior_tr = mean(trs[-24:-6]) if len(trs) >= 24 else (mean(trs[:-6]) if len(trs) > 6 else 0.0)
    tr_contraction_ratio = recent_tr / prior_tr if prior_tr > 1e-12 else 1.0

    bodies = [abs(c - o) for o, c in zip(opens, closes)]
    recent_body = mean(bodies[-6:]) if len(bodies) >= 6 else 0.0
    prior_body = mean(bodies[-24:-6]) if len(bodies) >= 24 else (mean(bodies[:-6]) if len(bodies) > 6 else 0.0)
    body_contraction_ratio = recent_body / prior_body if prior_body > 1e-12 else 1.0

    recent_volume = mean(volumes[-6:]) if len(volumes) >= 6 else 0.0
    prior_volume = mean(volumes[-24:-6]) if len(volumes) >= 24 else (mean(volumes[:-6]) if len(volumes) > 6 else 0.0)
    volume_contraction_ratio = recent_volume / prior_volume if prior_volume > 1e-12 else 1.0

    low_blocks = _block_extrema(lows[:-1], mode="min")
    high_blocks = _block_extrema(highs[:-1], mode="max")
    higher_lows = _rising(low_blocks)
    lower_highs = _falling(high_blocks)

    atr_pct = max(0.03, _f(metrics.get("atr_pct"), 0.5))
    atr_abs = current * atr_pct / 100.0 if current > 0 else 0.0
    recent_high = max(highs[-24:])
    recent_low = min(lows[-24:])
    dist_high_atr = (recent_high - current) / atr_abs if atr_abs > 1e-12 else 99.0
    dist_low_atr = (current - recent_low) / atr_abs if atr_abs > 1e-12 else 99.0

    # "Flat" boundary + rising/falling opposite side approximates ascending /
    # descending triangle pressure without pretending that every squeeze breaks.
    high_span = max(high_blocks) - min(high_blocks) if high_blocks else 0.0
    low_span = max(low_blocks) - min(low_blocks) if low_blocks else 0.0
    resistance_flat = bool(high_blocks and atr_abs > 0 and high_span <= atr_abs * 1.10)
    support_flat = bool(low_blocks and atr_abs > 0 and low_span <= atr_abs * 1.10)

    ema9 = _ema(closes[-40:], 9)
    ema21 = _ema(closes[-60:], 21)
    ema_gap_atr = abs(ema9 - ema21) / atr_abs if atr_abs > 1e-12 else 99.0
    ema_squeezed = ema_gap_atr <= 0.65
    ema_long_bias = ema9 >= ema21
    ema_short_bias = ema9 <= ema21

    contraction_score = 0.0
    evidence: list[str] = []
    warnings: list[str] = []

    if ratio_12_48 <= 0.55:
        contraction_score += 22
        evidence.append("range_12_vs_48_compressed")
    elif ratio_12_48 <= 0.68:
        contraction_score += 14
        evidence.append("range_12_vs_48_tightening")

    if ratio_6_24 <= 0.58:
        contraction_score += 16
        evidence.append("range_6_vs_24_compressed")
    elif ratio_6_24 <= 0.72:
        contraction_score += 9

    if ratio_24_48 <= 0.72:
        contraction_score += 10
        evidence.append("multi_stage_range_contraction")

    if tr_contraction_ratio <= 0.72:
        contraction_score += 14
        evidence.append("true_range_contraction")
    elif tr_contraction_ratio <= 0.85:
        contraction_score += 8

    if body_contraction_ratio <= 0.70:
        contraction_score += 10
        evidence.append("candle_body_contraction")
    elif body_contraction_ratio <= 0.85:
        contraction_score += 5

    if volume_contraction_ratio <= 0.78:
        contraction_score += 8
        evidence.append("volume_dry_up")
    elif volume_contraction_ratio > 1.60:
        warnings.append("volume_already_expanding_may_be_late")

    if ema_squeezed:
        contraction_score += 8
        evidence.append("ema9_ema21_squeeze")

    compression_score = _clip(contraction_score)

    long_pressure = 0.0
    short_pressure = 0.0
    long_evidence: list[str] = []
    short_evidence: list[str] = []

    if higher_lows:
        long_pressure += 26
        long_evidence.append("higher_lows")
    if lower_highs:
        short_pressure += 26
        short_evidence.append("lower_highs")

    if resistance_flat and higher_lows:
        long_pressure += 24
        long_evidence.append("ascending_triangle_pressure")
    if support_flat and lower_highs:
        short_pressure += 24
        short_evidence.append("descending_triangle_pressure")

    if -0.20 <= dist_high_atr <= 1.10:
        long_pressure += 18
        long_evidence.append("pressing_resistance")
    if -0.20 <= dist_low_atr <= 1.10:
        short_pressure += 18
        short_evidence.append("pressing_support")

    if ema_long_bias:
        long_pressure += 8
        long_evidence.append("ema_bias_long")
    if ema_short_bias:
        short_pressure += 8
        short_evidence.append("ema_bias_short")

    flow = _f(metrics.get("futures_delta_ratio"))
    spot = _f(metrics.get("spot_delta_ratio"))
    if flow >= 0.04 or spot >= 0.03:
        long_pressure += 10
        long_evidence.append("early_buy_flow_support")
    if flow <= -0.04 or spot <= -0.03:
        short_pressure += 10
        short_evidence.append("early_sell_flow_support")

    long_pressure = _clip(long_pressure)
    short_pressure = _clip(short_pressure)

    edge = long_pressure - short_pressure
    if edge >= 18:
        direction = "LONG"
        directional_pressure = long_pressure
        directional_evidence = long_evidence
    elif edge <= -18:
        direction = "SHORT"
        directional_pressure = short_pressure
        directional_evidence = short_evidence
    else:
        direction = "NEUTRAL"
        directional_pressure = max(long_pressure, short_pressure)
        directional_evidence = long_evidence if long_pressure >= short_pressure else short_evidence

    # Compression deserves priority only when both squeeze quality and directional
    # pressure are present. Pure low volatility without pressure stays neutral.
    early_score = _clip(compression_score * 0.58 + directional_pressure * 0.42)
    if compression_score >= 72 and directional_pressure >= 68:
        stage = "ARMED_EARLY"
    elif compression_score >= 58 and directional_pressure >= 52:
        stage = "BUILDING"
    elif compression_score >= 52:
        stage = "SQUEEZE_NEUTRAL"
    else:
        stage = "NO_COMPRESSION_EDGE"

    priority_bonus = 0.0
    if direction in {"LONG", "SHORT"}:
        if stage == "ARMED_EARLY":
            priority_bonus = min(28.0, 14.0 + (early_score - 70.0) * 0.55)
        elif stage == "BUILDING":
            priority_bonus = min(18.0, 8.0 + max(0.0, early_score - 55.0) * 0.35)

    return {
        "version": VERSION,
        "available": True,
        "stage": stage,
        "direction": direction,
        "compression_score": round(compression_score, 2),
        "directional_pressure_score": round(directional_pressure, 2),
        "early_score": round(early_score, 2),
        "priority_bonus": round(priority_bonus, 2),
        "score_is_probability": False,
        "evidence": evidence,
        "directional_evidence": directional_evidence,
        "warnings": warnings,
        "features": {
            "range_48_pct": round(range_48, 4),
            "range_24_pct": round(range_24, 4),
            "range_12_pct": round(range_12, 4),
            "range_6_pct": round(range_6, 4),
            "range_ratio_12_48": round(ratio_12_48, 4),
            "range_ratio_6_24": round(ratio_6_24, 4),
            "range_ratio_24_48": round(ratio_24_48, 4),
            "true_range_contraction_ratio": round(tr_contraction_ratio, 4),
            "body_contraction_ratio": round(body_contraction_ratio, 4),
            "volume_contraction_ratio": round(volume_contraction_ratio, 4),
            "higher_lows": higher_lows,
            "lower_highs": lower_highs,
            "resistance_flat": resistance_flat,
            "support_flat": support_flat,
            "distance_to_high_atr": round(dist_high_atr, 4),
            "distance_to_low_atr": round(dist_low_atr, 4),
            "ema_gap_atr": round(ema_gap_atr, 4),
            "ema_squeezed": ema_squeezed,
        },
        "policy": {
            "compression_has_priority_in_early_detection": True,
            "can_create_entry_alone": False,
            "can_override_invalidation": False,
            "can_override_chase": False,
            "can_raise_leverage_alone": False,
            "paper_shadow_learning_required": True,
        },
        "note": (
            "Compression is weighted as early structural evidence so ExplodeX can notice "
            "the setup before the breakout. It still needs safe entry geometry and cannot "
            "override stop, invalidation or chase protections."
        ),
    }
