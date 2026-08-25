from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def evaluate_expected_value_gate(
    *,
    similar: dict[str, Any] | None,
    reward_risk_tp1: float,
) -> dict[str, Any]:
    """Evaluate historical edge without pretending it guarantees the next trade.

    The gate only hard-blocks when the similar-case sample is calibrated. While
    learning, it reports missing statistical confirmation but leaves the technical
    system available for PAPER data collection.
    """
    similar = similar or {}
    status = str(similar.get("calibration_status") or "INSUFFICIENT_SIMILAR_CASES")
    decided = int(similar.get("decided") or 0)
    minimum = int(similar.get("minimum_decided_for_probability") or 30)
    weighted_win_rate = similar.get("weighted_win_rate_pct")
    weighted_avg_r = similar.get("weighted_avg_r")
    wilson_low = similar.get("wilson_low_pct")
    avg_similarity = _f(similar.get("avg_similarity_pct"))
    rr1 = max(_f(reward_risk_tp1), 0.0)

    calibrated = status == "CALIBRATED" and weighted_win_rate is not None and decided >= minimum
    if not calibrated:
        return {
            "pass": True,
            "hard_block": False,
            "status": "LEARNING",
            "calibrated": False,
            "decided": decided,
            "minimum": minimum,
            "avg_similarity_pct": round(avg_similarity, 1),
            "weighted_win_rate_pct": None,
            "weighted_avg_r": weighted_avg_r,
            "reward_risk_tp1": round(rr1, 2),
            "expected_value_r": None,
            "conservative_expected_value_r": None,
            "blocks": [],
            "note": "Aún no hay muestra similar suficiente; no se presenta ventaja estadística como certeza.",
        }

    p = _f(weighted_win_rate) / 100.0
    conservative_p = max(0.0, min(p, _f(wilson_low, weighted_win_rate) / 100.0))
    expected_value_r = p * rr1 - (1.0 - p)
    conservative_ev_r = conservative_p * rr1 - (1.0 - conservative_p)

    blocks: list[str] = []
    if _f(weighted_win_rate) < 50.0:
        blocks.append("historical_win_rate_weak")
    if weighted_avg_r is not None and _f(weighted_avg_r) <= 0.10:
        blocks.append("historical_avg_r_weak")
    if expected_value_r <= 0.10:
        blocks.append("expected_value_too_low")
    # Conservative EV may be negative with modest samples; use it as a hard
    # warning only when materially negative rather than requiring it > 0.
    if conservative_ev_r < -0.25:
        blocks.append("uncertainty_too_high")
    if avg_similarity < 55.0:
        blocks.append("similarity_quality_low")

    return {
        "pass": not blocks,
        "hard_block": bool(blocks),
        "status": "POSITIVE_EDGE" if not blocks else "NEGATIVE_OR_UNCERTAIN_EDGE",
        "calibrated": True,
        "decided": decided,
        "minimum": minimum,
        "avg_similarity_pct": round(avg_similarity, 1),
        "weighted_win_rate_pct": round(_f(weighted_win_rate), 2),
        "weighted_avg_r": round(_f(weighted_avg_r), 3) if weighted_avg_r is not None else None,
        "wilson_low_pct": round(_f(wilson_low), 2) if wilson_low is not None else None,
        "reward_risk_tp1": round(rr1, 2),
        "expected_value_r": round(expected_value_r, 3),
        "conservative_expected_value_r": round(conservative_ev_r, 3),
        "blocks": blocks,
        "note": "Expected Value Gate usa resultados de casos similares; una expectativa positiva no garantiza que la siguiente operación gane.",
    }
