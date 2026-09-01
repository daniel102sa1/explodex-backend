from __future__ import annotations

from typing import Any

VERSION = "risk_conviction_engine_v2_elliott"


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


def _quality_for_lane(lane_name: str, lane: dict[str, Any], setup_score: float) -> float:
    if lane_name == "SWING_PAPER":
        return _f(lane.get("trajectory_score"), setup_score)
    return _f(lane.get("ignition_score"), setup_score)


def _selected_net_rr(lane: dict[str, Any]) -> float:
    math = _d(lane.get("execution_math"))
    chosen = _d(math.get("chosen_target"))
    return _f(chosen.get("net_rr"))


def build_risk_conviction(
    *,
    lane_name: str,
    lane: dict[str, Any],
    setup_score: float,
    risk_score: float,
    forecast_matrix: dict[str, Any] | None,
    elliott_structure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lane_name = str(lane_name or "").upper()
    lane = _d(lane)
    matrix = _d(forecast_matrix)
    elliott = _d(elliott_structure)
    direction = str(lane.get("direction") or "").upper()
    quality = _quality_for_lane(lane_name, lane, setup_score)
    net_rr = _selected_net_rr(lane)

    conviction = 35.0 + max(0.0, quality - 55.0) * 0.65
    reasons: list[str] = [f"quality={quality:.1f}"]

    if net_rr >= 3.5:
        conviction += 12.0
        reasons.append("net_rr>=3.5")
    elif net_rr >= 3.0:
        conviction += 9.0
        reasons.append("net_rr>=3.0")
    elif net_rr >= 2.6:
        conviction += 6.0
        reasons.append("net_rr>=2.6")
    elif net_rr > 0:
        conviction += 2.0
        reasons.append("net_rr_positive")

    risk_score = _f(risk_score, 100.0)
    conviction -= max(0.0, risk_score - 25.0) * 0.32
    if risk_score <= 30:
        conviction += 5.0
        reasons.append("low_risk_score")
    elif risk_score >= 55:
        reasons.append("elevated_risk_score")

    horizons = _d(matrix.get("horizons"))
    aligned = 0
    opposing = 0
    edge_sum = 0.0
    edge_count = 0
    for horizon in ("15m", "1h", "4h", "6h", "24h"):
        item = _d(horizons.get(horizon))
        hdir = str(item.get("direction") or "").upper()
        edge = _f(item.get("edge"))
        if hdir == direction:
            aligned += 1
            edge_sum += edge
            edge_count += 1
        elif hdir in {"LONG", "SHORT"} and hdir != direction:
            opposing += 1
    avg_aligned_edge = edge_sum / edge_count if edge_count else 0.0

    consensus = str(matrix.get("consensus") or "MIXED").upper()
    horizon_conflict = bool(matrix.get("horizon_conflict"))
    if aligned >= 5:
        conviction += 14.0
        reasons.append("5of5_horizon_alignment")
    elif aligned >= 4:
        conviction += 10.0
        reasons.append("4of5_horizon_alignment")
    elif aligned >= 3:
        conviction += 5.0
        reasons.append("3of5_horizon_alignment")
    if avg_aligned_edge >= 24:
        conviction += 8.0
        reasons.append("strong_horizon_edge")
    elif avg_aligned_edge >= 16:
        conviction += 4.0
        reasons.append("good_horizon_edge")

    if consensus == direction:
        conviction += 7.0
        reasons.append("matrix_consensus_aligned")
    elif consensus in {"LONG", "SHORT"} and consensus != direction:
        conviction -= 18.0
        reasons.append("matrix_consensus_opposes")
    if horizon_conflict:
        conviction -= 14.0
        reasons.append("short_long_horizon_conflict")
    if opposing >= 2:
        conviction -= min(15.0, opposing * 5.0)
        reasons.append(f"opposing_horizons={opposing}")

    # Elliott is deliberately bounded. A subjective wave count can support or
    # reduce size, but can never create an entry or override the canonical side.
    ebest = _d(elliott.get("best"))
    elliott_score = _f(ebest.get("score"))
    elliott_direction = str(ebest.get("direction") or "").upper()
    elliott_status = str(elliott.get("status") or "")
    if elliott_status == "CLEAR_COUNT" and elliott_score >= 68.0:
        if elliott_direction == direction:
            bonus = 8.0 if elliott_score >= 82.0 else 5.0
            if bool(elliott.get("timeframe_agreement")):
                bonus += 2.0
            conviction += min(10.0, bonus)
            reasons.append(f"elliott_aligned={elliott_score:.1f}")
        elif elliott_direction in {"LONG", "SHORT"}:
            penalty = 12.0 if elliott_score >= 82.0 else 8.0
            conviction -= penalty
            reasons.append(f"elliott_conflict={elliott_score:.1f}")

    conviction = _clip(conviction)

    if conviction >= 90:
        multiplier = 1.50
        tier = "MAX_CONVICTION"
    elif conviction >= 82:
        multiplier = 1.25
        tier = "HIGH"
    elif conviction >= 72:
        multiplier = 1.00
        tier = "NORMAL_PLUS"
    elif conviction >= 62:
        multiplier = 0.75
        tier = "NORMAL"
    elif conviction >= 52:
        multiplier = 0.50
        tier = "LOW"
    else:
        multiplier = 0.25
        tier = "MINIMAL"

    if lane_name == "AGGRESSIVE_PAPER":
        multiplier = min(multiplier, 0.50)
        tier = "EARLY_CAPPED" if multiplier >= 0.50 else tier
    elif lane_name == "SWING_PAPER":
        multiplier = min(multiplier, 1.25)
        if multiplier >= 1.25 and tier == "MAX_CONVICTION":
            tier = "HIGH_SWING_CAPPED"

    if horizon_conflict:
        multiplier = min(multiplier, 0.50)
    if consensus in {"LONG", "SHORT"} and consensus != direction:
        multiplier = min(multiplier, 0.25)
    if elliott_status == "CLEAR_COUNT" and elliott_direction in {"LONG", "SHORT"} and elliott_direction != direction and elliott_score >= 82.0:
        multiplier = min(multiplier, 0.50)

    return {
        "version": VERSION,
        "paper_only": True,
        "creates_entry": False,
        "changes_direction": False,
        "conviction_score": round(conviction, 1),
        "tier": tier,
        "risk_budget_multiplier": round(multiplier, 2),
        "target_account_risk_pct_before_portfolio_brakes": round(multiplier, 2),
        "lane": lane_name,
        "direction": direction,
        "quality": round(quality, 1),
        "risk_score": round(risk_score, 1),
        "net_rr": round(net_rr, 3),
        "aligned_horizons": aligned,
        "opposing_horizons": opposing,
        "average_aligned_edge": round(avg_aligned_edge, 1),
        "horizon_conflict": horizon_conflict,
        "consensus": consensus,
        "elliott": {
            "status": elliott_status or "MISSING",
            "direction": elliott_direction or None,
            "score": round(elliott_score, 1),
            "timeframe_agreement": bool(elliott.get("timeframe_agreement")),
            "pattern": ebest.get("pattern"),
        },
        "reasons": reasons,
        "rule": "Higher Heart conviction may increase PAPER size; uncertainty, conflict, Elliott disagreement and portfolio brakes reduce it.",
    }
