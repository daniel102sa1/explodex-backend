from __future__ import annotations

from typing import Any

VERSION = "horizon_forecast_matrix_v1"


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _side_from_scores(long_score: float, short_score: float) -> tuple[str, float]:
    edge = abs(long_score - short_score)
    if edge < 8:
        return "NEUTRAL", edge
    return ("LONG" if long_score > short_score else "SHORT"), edge


def _trend_vote(trend: str, weight: float) -> tuple[float, float]:
    trend = str(trend or "").upper()
    if trend == "BULLISH":
        return weight, 0.0
    if trend == "BEARISH":
        return 0.0, weight
    return 0.0, 0.0


def build_horizon_forecast_matrix(
    *,
    score: dict[str, Any],
    prediction: dict[str, Any],
    heart: dict[str, Any],
) -> dict[str, Any]:
    """Produce one coherent forecast split across time horizons.

    The matrix is descriptive, not a second execution authority. It helps the
    canonical Heart distinguish immediate timing from slower trajectory so a
    15m pullback does not automatically negate a 24h thesis (or vice versa).
    Scores are technical indices, not probabilities.
    """
    metrics = _d(score.get("metrics"))
    ignition = _d(heart.get("ignition"))
    trajectory = _d(heart.get("trajectory_forecast"))
    htf = _d(heart.get("higher_timeframe_context"))
    frames = _d(htf.get("frames"))
    liquidity = _d(heart.get("liquidity_intelligence"))

    direction = str(heart.get("direction") or prediction.get("direction") or score.get("direction") or "").upper()
    sign_long = 1.0 if direction == "LONG" else 0.0
    sign_short = 1.0 if direction == "SHORT" else 0.0

    delta_f = _f(metrics.get("futures_delta_ratio"))
    delta_s = _f(metrics.get("spot_delta_ratio"))
    book = _f(metrics.get("order_book_imbalance"))
    ch5 = _f(metrics.get("change_5m_pct"))
    ch15 = _f(metrics.get("change_15m_pct"))
    ch1h = _f(metrics.get("change_1h_pct"))
    oi = _f(metrics.get("oi_change_pct"))
    ignition_score = _f(ignition.get("score"))
    trajectory_long = _f(trajectory.get("long_trajectory_score"), 50.0)
    trajectory_short = _f(trajectory.get("short_trajectory_score"), 50.0)

    def micro_component(value: float, scale: float) -> tuple[float, float]:
        strength = min(18.0, abs(value) * scale)
        return (strength, 0.0) if value > 0 else (0.0, strength) if value < 0 else (0.0, 0.0)

    horizons: dict[str, Any] = {}

    # 15m: dominated by ignition, aggressive flow and immediate momentum.
    l15 = 35.0 + sign_long * ignition_score * 0.22
    s15 = 35.0 + sign_short * ignition_score * 0.22
    for value, scale in ((delta_f, 80.0), (delta_s, 70.0), (book, 45.0), (ch5, 5.5), (ch15, 3.0)):
        a, b = micro_component(value, scale)
        l15 += a
        s15 += b
    if oi > 0.25:
        if ch15 >= 0:
            l15 += min(8.0, oi * 5.0)
        else:
            s15 += min(8.0, oi * 5.0)
    l15, s15 = _clip(l15), _clip(s15)

    # 1h: still uses flow, but structure and trajectory begin to matter.
    l1 = 30.0 + trajectory_long * 0.22
    s1 = 30.0 + trajectory_short * 0.22
    for value, scale in ((delta_f, 55.0), (delta_s, 50.0), (ch15, 2.5), (ch1h, 2.0)):
        a, b = micro_component(value, scale)
        l1 += a
        s1 += b
    l1, s1 = _clip(l1), _clip(s1)

    # 4h/6h/24h: use the same trajectory brain, progressively weighting HTF.
    def long_horizon(label: str, frame_name: str, traj_weight: float, frame_weight: float) -> dict[str, Any]:
        base_long = 22.0 + trajectory_long * traj_weight
        base_short = 22.0 + trajectory_short * traj_weight
        frame = _d(frames.get(frame_name))
        a, b = _trend_vote(str(frame.get("trend") or ""), frame_weight)
        base_long += a
        base_short += b
        strength = _f(frame.get("trend_strength_signed"))
        if strength > 0:
            base_long += min(12.0, abs(strength) * 12.0)
        elif strength < 0:
            base_short += min(12.0, abs(strength) * 12.0)
        attraction = str(liquidity.get("attraction_direction") or "").upper()
        if attraction == "UP":
            base_long += 5.0
        elif attraction == "DOWN":
            base_short += 5.0
        return _entry(label, base_long, base_short)

    def _entry(label: str, long_score: float, short_score: float) -> dict[str, Any]:
        long_score, short_score = _clip(long_score), _clip(short_score)
        side, edge = _side_from_scores(long_score, short_score)
        return {
            "horizon": label,
            "direction": side,
            "long_score": round(long_score, 1),
            "short_score": round(short_score, 1),
            "edge": round(edge, 1),
            "score_is_probability": False,
        }

    horizons["15m"] = _entry("15m", l15, s15)
    horizons["1h"] = _entry("1h", l1, s1)
    horizons["4h"] = long_horizon("4h", "4h", 0.42, 22.0)
    horizons["6h"] = long_horizon("6h", "6h", 0.46, 24.0)
    horizons["24h"] = long_horizon("24h", "1d", 0.50, 26.0)

    directional = [h for h in horizons.values() if h["direction"] in {"LONG", "SHORT"}]
    long_count = sum(1 for h in directional if h["direction"] == "LONG")
    short_count = sum(1 for h in directional if h["direction"] == "SHORT")
    if long_count >= 4:
        consensus = "LONG"
    elif short_count >= 4:
        consensus = "SHORT"
    else:
        consensus = "MIXED"

    short_term = horizons["15m"]["direction"]
    long_term = horizons["24h"]["direction"]
    horizon_conflict = short_term in {"LONG", "SHORT"} and long_term in {"LONG", "SHORT"} and short_term != long_term

    return {
        "version": VERSION,
        "horizons": horizons,
        "consensus": consensus,
        "horizon_conflict": horizon_conflict,
        "short_term_direction": short_term,
        "long_term_direction": long_term,
        "use": "Context for the single canonical Heart. This matrix never authorizes a trade by itself.",
    }
