from __future__ import annotations

from typing import Any

VERSION = "trajectory_forecast_v1"


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
    if trend == "BULLISH":
        long_score += weight
    elif trend == "BEARISH":
        short_score += weight
    return long_score, short_score


def _structural_geometry(direction: str, current: float, htf: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    frames = _d(htf.get("frames"))
    frame4 = _d(frames.get("4h"))
    atr_pct = _f(frame4.get("atr_pct"), _f(_d(score.get("metrics")).get("atr_pct"), 1.0))
    atr_pct = max(0.35, min(4.0, atr_pct))
    swing_low = _f(frame4.get("swing_low"))
    swing_high = _f(frame4.get("swing_high"))

    if direction == "LONG":
        structure_distance = ((current - swing_low) / current * 100.0) if current > 0 and 0 < swing_low < current else 0.0
    else:
        structure_distance = ((swing_high - current) / current * 100.0) if current > 0 and swing_high > current else 0.0

    stop_pct = max(1.0, atr_pct * 1.45, structure_distance + atr_pct * 0.20)
    stop_pct = min(6.0, stop_pct)
    risk_unit = current * stop_pct / 100.0
    stop = current - risk_unit if direction == "LONG" else current + risk_unit

    # Swing targets are intentionally zones, not exact guaranteed prices.
    target1 = current + risk_unit * 1.8 if direction == "LONG" else current - risk_unit * 1.8
    target2 = current + risk_unit * 2.6 if direction == "LONG" else current - risk_unit * 2.6
    target3 = current + risk_unit * 3.4 if direction == "LONG" else current - risk_unit * 3.4
    entry_half_width_pct = min(1.0, max(0.25, atr_pct * 0.30))
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
        "atr_4h_pct": round(atr_pct, 4),
        "stop_is_fixed_at_entry": True,
        "widen_stop_after_entry": False,
    }


def build_trajectory_forecast(
    score: dict[str, Any],
    prediction: dict[str, Any],
    htf: dict[str, Any],
    liquidity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate directional trajectory from 4h to 48h.

    This does not replace the tactical Heart. It answers a different question:
    whether the market has enough multi-timeframe evidence to justify holding a
    reduced-size PAPER swing through normal pullbacks. Scores are technical
    indices, not calibrated probabilities.
    """
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
    if trend_1h == "BULLISH":
        long_score += 10
    elif trend_1h == "BEARISH":
        short_score += 10
    if trend_15m == "BULLISH":
        long_score += 6
    elif trend_15m == "BEARISH":
        short_score += 6

    futures_delta = _f(metrics.get("futures_delta_ratio"))
    spot_delta = _f(metrics.get("spot_delta_ratio"))
    book = _f(metrics.get("order_book_imbalance"))
    change_1h = _f(metrics.get("change_1h_pct"))
    oi = _f(metrics.get("oi_change_pct"))
    funding = _f(metrics.get("funding_rate"))

    if futures_delta >= 0.08:
        long_score += 8
    elif futures_delta <= -0.08:
        short_score += 8
    if spot_delta >= 0.06:
        long_score += 7
    elif spot_delta <= -0.06:
        short_score += 7
    if book >= 0.08:
        long_score += 5
    elif book <= -0.08:
        short_score += 5

    # Price + OI is useful for distinguishing fresh trend participation from
    # pure liquidation. New OI aligned with price gets continuation credit.
    if oi >= 0.25 and change_1h >= 0.20:
        long_score += 8
    elif oi >= 0.25 and change_1h <= -0.20:
        short_score += 8
    elif oi <= -0.60:
        long_score -= 2
        short_score -= 2

    attraction = str(liquidity.get("attraction_direction") or "NEUTRAL").upper()
    if attraction == "UP":
        long_score += 8
    elif attraction == "DOWN":
        short_score += 8

    # Mild contrarian crowding signal only; funding never decides direction.
    if funding >= 0.0005:
        short_score += 3
    elif funding <= -0.0005:
        long_score += 3

    prediction_direction = str(prediction.get("direction") or "").upper()
    prediction_phase = str(prediction.get("phase") or "SIN_SETUP").upper()
    if prediction_phase not in {"SIN_SETUP", "SIN_DATOS"}:
        if prediction_direction == "LONG":
            long_score += 6
        elif prediction_direction == "SHORT":
            short_score += 6

    long_score = _clip(long_score)
    short_score = _clip(short_score)
    direction = "LONG" if long_score >= short_score else "SHORT"
    dominant = max(long_score, short_score)
    opposite = min(long_score, short_score)
    edge = dominant - opposite

    wanted = "BULLISH" if direction == "LONG" else "BEARISH"
    opposite_trend = "BEARISH" if direction == "LONG" else "BULLISH"
    aligned_frames = sum(1 for item in frames.values() if isinstance(item, dict) and item.get("trend") == wanted)
    conflicting_frames = sum(1 for item in frames.values() if isinstance(item, dict) and item.get("trend") == opposite_trend)

    current = _f(score.get("current_price"))
    geometry = _structural_geometry(direction, current, htf, score) if current > 0 else {}
    risk_score = _f(score.get("risk_score"), 100.0)
    extended = abs(change_1h) >= 5.5

    # A swing lane is deliberately easier to trigger than tactical ENTER, but
    # still requires actual directional separation and HTF support.
    should_enter_paper = (
        current > 0
        and dominant >= 62.0
        and edge >= 12.0
        and aligned_frames >= 2
        and conflicting_frames <= 1
        and risk_score <= 70.0
        and not extended
        and bool(geometry)
    )

    if aligned_frames == 3 and dominant >= 75:
        horizon = "24-48h"
        max_hold_minutes = 2880
    elif aligned_frames >= 2 and dominant >= 66:
        horizon = "8-24h"
        max_hold_minutes = 1440
    else:
        horizon = "4-12h"
        max_hold_minutes = 720

    blockers: list[str] = []
    if current <= 0:
        blockers.append("no_price")
    if dominant < 62:
        blockers.append("trajectory_score_below_62")
    if edge < 12:
        blockers.append("direction_not_separated")
    if aligned_frames < 2:
        blockers.append("insufficient_htf_alignment")
    if conflicting_frames > 1:
        blockers.append("too_many_htf_conflicts")
    if risk_score > 70:
        blockers.append("risk_score_above_70")
    if extended:
        blockers.append("already_extended_1h")

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
            "management": "Aguantar retrocesos normales mientras no toque el stop estructural; no ensanchar el stop después de entrar.",
        },
        "interpretation": "Trayectoria 4h-48h para PAPER; estima dirección y zona objetivo, no una ruta recta ni un precio garantizado.",
    }
