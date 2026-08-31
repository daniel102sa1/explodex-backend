from __future__ import annotations

import math
from statistics import median
from typing import Any

VERSION = "trajectory_forecast_v3_horizon_matched_range"


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _frame_vote(frame: dict[str, Any], weight: float, long_score: float, short_score: float) -> tuple[float, float]:
    trend = str(frame.get("trend") or "UNKNOWN").upper()
    strength = abs(_f(frame.get("trend_strength_signed"), 0.35))
    strength_factor = max(0.45, min(1.0, strength + 0.35))
    if trend == "BULLISH":
        long_score += weight * strength_factor
    elif trend == "BEARISH":
        short_score += weight * strength_factor
    return long_score, short_score


def _expected_ranges(frames: dict[str, Any]) -> dict[str, float]:
    f4 = _d(frames.get("4h"))
    f6 = _d(frames.get("6h"))
    f1d = _d(frames.get("1d"))
    v4 = max(_f(f4.get("robust_bar_range_pct")), _f(f4.get("atr_pct")))
    v6 = max(_f(f6.get("robust_bar_range_pct")), _f(f6.get("atr_pct")))
    v1d = max(_f(f1d.get("robust_bar_range_pct")), _f(f1d.get("atr_pct")))
    estimates8 = [x for x in (v4 * math.sqrt(2.0), v6 * math.sqrt(8.0 / 6.0)) if x > 0]
    estimates24 = [x for x in (v4 * math.sqrt(6.0), v6 * 2.0, v1d) if x > 0]
    estimates48 = [x for x in (v4 * math.sqrt(12.0), v6 * math.sqrt(8.0), v1d * math.sqrt(2.0)) if x > 0]

    def robust(values: list[float]) -> float:
        if not values:
            return 0.0
        return max(0.25, min(25.0, median(values)))

    return {
        "8h_pct": round(robust(estimates8), 4),
        "24h_pct": round(robust(estimates24), 4),
        "48h_pct": round(robust(estimates48), 4),
    }


def _structural_geometry(direction: str, current: float, htf: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    frames = _d(htf.get("frames"))
    frame4 = _d(frames.get("4h"))
    robust4 = max(
        _f(frame4.get("robust_bar_range_pct")),
        _f(frame4.get("atr_pct")),
        _f(_d(score.get("metrics")).get("atr_pct"), 0.8),
        0.35,
    )
    robust4 = min(5.0, robust4)
    swing_low = _f(frame4.get("swing_low"))
    swing_high = _f(frame4.get("swing_high"))
    swing_low_outer = _f(frame4.get("swing_low_outer"), swing_low)
    swing_high_outer = _f(frame4.get("swing_high_outer"), swing_high)

    if direction == "LONG":
        inner_distance = ((current - swing_low) / current * 100.0) if 0 < swing_low < current else 0.0
        outer_distance = ((current - swing_low_outer) / current * 100.0) if 0 < swing_low_outer < current else 0.0
    else:
        inner_distance = ((swing_high - current) / current * 100.0) if swing_high > current else 0.0
        outer_distance = ((swing_high_outer - current) / current * 100.0) if swing_high_outer > current else 0.0

    structural = inner_distance + robust4 * 0.30 if inner_distance > 0 else robust4 * 1.20
    if outer_distance > 0 and outer_distance <= structural * 1.6:
        structural = max(structural, outer_distance + robust4 * 0.15)
    stop_pct = max(0.9, robust4 * 1.15, structural)
    stop_pct = min(6.5, stop_pct)
    risk_unit = current * stop_pct / 100.0
    stop = current - risk_unit if direction == "LONG" else current + risk_unit

    target1 = current + risk_unit * 2.6 if direction == "LONG" else current - risk_unit * 2.6
    target2 = current + risk_unit * 3.4 if direction == "LONG" else current - risk_unit * 3.4
    target3 = current + risk_unit * 4.2 if direction == "LONG" else current - risk_unit * 4.2
    entry_half_width_pct = min(1.2, max(0.25, robust4 * 0.30))
    entry_low = current * (1.0 - entry_half_width_pct / 100.0)
    entry_high = current * (1.0 + entry_half_width_pct / 100.0)

    return {
        "entry_low": round(entry_low, 12),
        "entry_high": round(entry_high, 12),
        "structural_stop": round(stop, 12),
        "stop_distance_pct": round(stop_pct, 3),
        "target1": round(target1, 12),
        "target2": round(target2, 12),
        "target3": round(target3, 12),
        "target_zone_low": round(min(target2, target3), 12),
        "target_zone_high": round(max(target2, target3), 12),
        "robust_4h_range_pct": round(robust4, 4),
        "stop_is_fixed_at_entry": True,
        "widen_stop_after_entry": False,
    }


def build_trajectory_forecast(
    score: dict[str, Any],
    prediction: dict[str, Any],
    htf: dict[str, Any],
    liquidity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = _d(score.get("metrics"))
    frames = _d(htf.get("frames"))
    liquidity = _d(liquidity)

    long_score = 12.0
    short_score = 12.0
    long_score, short_score = _frame_vote(_d(frames.get("4h")), 18.0, long_score, short_score)
    long_score, short_score = _frame_vote(_d(frames.get("6h")), 16.0, long_score, short_score)
    long_score, short_score = _frame_vote(_d(frames.get("1d")), 12.0, long_score, short_score)

    trend_1h = str(metrics.get("trend_1h") or "NEUTRAL").upper()
    trend_15m = str(metrics.get("trend_15m") or "NEUTRAL").upper()
    if trend_1h == "BULLISH": long_score += 10
    elif trend_1h == "BEARISH": short_score += 10
    if trend_15m == "BULLISH": long_score += 6
    elif trend_15m == "BEARISH": short_score += 6

    futures_delta = _f(metrics.get("futures_delta_ratio"))
    spot_delta = _f(metrics.get("spot_delta_ratio"))
    book = _f(metrics.get("order_book_imbalance"))
    change_1h = _f(metrics.get("change_1h_pct"))
    oi = _f(metrics.get("oi_change_pct"))
    funding = _f(metrics.get("funding_rate"))
    if futures_delta >= 0.08: long_score += 8
    elif futures_delta <= -0.08: short_score += 8
    if spot_delta >= 0.06: long_score += 7
    elif spot_delta <= -0.06: short_score += 7
    if book >= 0.08: long_score += 5
    elif book <= -0.08: short_score += 5
    if oi >= 0.25 and change_1h >= 0.20: long_score += 8
    elif oi >= 0.25 and change_1h <= -0.20: short_score += 8
    elif oi <= -0.60:
        long_score -= 2
        short_score -= 2

    attraction = str(liquidity.get("attraction_direction") or "NEUTRAL").upper()
    if attraction == "UP": long_score += 8
    elif attraction == "DOWN": short_score += 8
    if funding >= 0.0005: short_score += 3
    elif funding <= -0.0005: long_score += 3

    prediction_direction = str(prediction.get("direction") or "").upper()
    prediction_phase = str(prediction.get("phase") or "SIN_SETUP").upper()
    if prediction_phase not in {"SIN_SETUP", "SIN_DATOS"}:
        if prediction_direction == "LONG": long_score += 6
        elif prediction_direction == "SHORT": short_score += 6

    long_score = _clip(long_score)
    short_score = _clip(short_score)
    direction = "LONG" if long_score >= short_score else "SHORT"
    dominant, opposite = max(long_score, short_score), min(long_score, short_score)
    edge = dominant - opposite

    wanted = "BULLISH" if direction == "LONG" else "BEARISH"
    opposite_trend = "BEARISH" if direction == "LONG" else "BULLISH"
    aligned_frames = sum(1 for item in frames.values() if isinstance(item, dict) and item.get("trend") == wanted)
    conflicting_frames = sum(1 for item in frames.values() if isinstance(item, dict) and item.get("trend") == opposite_trend)
    strengths = [_f(item.get("trend_strength_signed")) for item in frames.values() if isinstance(item, dict) and item.get("available")]
    mean_strength = sum(strengths) / len(strengths) if strengths else 0.0
    directional_strength = mean_strength if direction == "LONG" else -mean_strength

    current = _f(score.get("current_price"))
    geometry = _structural_geometry(direction, current, htf, score) if current > 0 else {}
    expected_ranges = _expected_ranges(frames)
    risk_score = _f(score.get("risk_score"), 100.0)
    extended = abs(change_1h) >= max(5.5, expected_ranges.get("8h_pct", 0.0) * 0.9)

    target1_distance_pct = abs(_f(geometry.get("target1")) - current) / current * 100.0 if current > 0 and geometry else 0.0
    expected24 = _f(expected_ranges.get("24h_pct"))
    expected48 = _f(expected_ranges.get("48h_pct"))
    fits24 = expected24 > 0 and target1_distance_pct <= expected24 * 1.20
    fits48 = expected48 > 0 and target1_distance_pct <= expected48 * 1.20
    target_plausible = fits24 or fits48

    if not fits24 and fits48:
        horizon = "24-48h"
        max_hold_minutes = 2880
    elif aligned_frames == 3 and dominant >= 75 and fits48:
        horizon = "24-48h"
        max_hold_minutes = 2880
    elif aligned_frames >= 2 and dominant >= 66 and fits24:
        horizon = "8-24h"
        max_hold_minutes = 1440
    else:
        horizon = "4-12h"
        max_hold_minutes = 720

    should_enter_paper = (
        current > 0
        and dominant >= 62.0
        and edge >= 12.0
        and aligned_frames >= 2
        and conflicting_frames <= 1
        and directional_strength >= 0.08
        and risk_score <= 70.0
        and not extended
        and target_plausible
        and bool(geometry)
    )

    blockers: list[str] = []
    if current <= 0: blockers.append("no_price")
    if dominant < 62: blockers.append("trajectory_score_below_62")
    if edge < 12: blockers.append("direction_not_separated")
    if aligned_frames < 2: blockers.append("insufficient_htf_alignment")
    if conflicting_frames > 1: blockers.append("too_many_htf_conflicts")
    if directional_strength < 0.08: blockers.append("htf_trend_strength_too_weak")
    if risk_score > 70: blockers.append("risk_score_above_70")
    if extended: blockers.append("already_extended_1h")
    if not target_plausible: blockers.append("target_exceeds_48h_expected_range")

    return {
        "version": VERSION,
        "direction": direction,
        "long_trajectory_score": round(long_score, 1),
        "short_trajectory_score": round(short_score, 1),
        "trajectory_score": round(dominant, 1),
        "direction_edge": round(edge, 1),
        "score_is_probability": False,
        "aligned_htf_frames": aligned_frames,
        "conflicting_htf_frames": conflicting_frames,
        "directional_htf_strength": round(directional_strength, 5),
        "expected_ranges": expected_ranges,
        "horizon": horizon,
        "max_hold_minutes": max_hold_minutes,
        "should_enter_paper_swing": should_enter_paper,
        "blockers": blockers,
        "swing_plan": {
            **geometry,
            "direction": direction,
            "horizon": horizon,
            "max_hold_minutes": max_hold_minutes,
            "risk_budget_pct": 0.50,
            "max_leverage": 2,
            "target1_distance_pct": round(target1_distance_pct, 4),
            "target_fits_expected_24h_range": fits24,
            "target_fits_expected_48h_range": fits48,
            "management": "Aguantar retrocesos normales mientras no toque el stop estructural; no ensanchar el stop después de entrar.",
        },
        "interpretation": "Trayectoria 4h-48h: el horizonte se adapta al rango esperado; no supone una ruta recta ni un precio garantizado.",
    }
