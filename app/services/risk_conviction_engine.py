from __future__ import annotations

from typing import Any

VERSION = "risk_conviction_engine_v1"


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
) -> dict[str, Any]:
    """Choose PAPER capital risk from the same Heart evidence.

    Output is a multiplier of the portfolio's base 1% risk budget. For example,
    1.50 means target roughly 1.5% account risk before portfolio brakes. This
    changes PAPER sizing only; it never creates an entry or changes direction.
    """
    lane_name = str(lane_name or "").upper()
    lane = _d(lane)
    matrix = _d(forecast_matrix)
    direction = str(lane.get("direction") or "").upper()
    quality = _quality_for_lane(lane_name, lane, setup_score)
    net_rr = _selected_net_rr(lane)

    conviction = 35.0 + max(0.0, quality - 55.0) * 0.65
    reasons: list[str] = [f"quality={quality:.1f}"]

    # Reward plans with real room after estimated costs.
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

    # Risk score is a penalty, not a direction vote.
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

    # Experimental early entries never receive high-conviction sizing.
    if lane_name == "AGGRESSIVE_PAPER":
        multiplier = min(multiplier, 0.50)
        tier = "EARLY_CAPPED" if multiplier >= 0.50 else tier
    elif lane_name == "SWING_PAPER":
        multiplier = min(multiplier, 1.25)
        if multiplier >= 1.25 and tier == "MAX_CONVICTION":
            tier = "HIGH_SWING_CAPPED"

    # Explicit timeframe disagreement prevents large sizing even if other
    # components are strong. The entry can remain valid, but size must shrink.
    if horizon_conflict:
        multiplier = min(multiplier, 0.50)
    if consensus in {"LONG", "SHORT"} and consensus != direction:
        multiplier = min(multiplier, 0.25)

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
        "reasons": reasons,
        "rule": "Higher Heart conviction may increase PAPER size; uncertainty, conflict and portfolio brakes reduce it.",
    }
