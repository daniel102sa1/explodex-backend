from __future__ import annotations

from typing import Any

VERSION = "risk_conviction_engine_v5_breadth_shadow"


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "": return default
        return float(value)
    except (TypeError, ValueError): return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _quality_for_lane(lane_name: str, lane: dict[str, Any], setup_score: float) -> float:
    if lane_name == "SWING_PAPER": return _f(lane.get("trajectory_score"), setup_score)
    if lane_name == "PRE_EVENT_PAPER": return _f(lane.get("preparation_score"), setup_score)
    return _f(lane.get("ignition_score"), setup_score)


def _selected_net_rr(lane: dict[str, Any]) -> float:
    chosen = _d(_d(lane.get("execution_math")).get("chosen_target"))
    return _f(chosen.get("net_rr"))


def build_risk_conviction(*, lane_name: str, lane: dict[str, Any], setup_score: float, risk_score: float,
                          forecast_matrix: dict[str, Any] | None, elliott_structure: dict[str, Any] | None = None) -> dict[str, Any]:
    lane_name = str(lane_name or "").upper(); lane = _d(lane); matrix = _d(forecast_matrix); elliott = _d(elliott_structure)
    direction = str(lane.get("direction") or "").upper(); quality = _quality_for_lane(lane_name, lane, setup_score); net_rr = _selected_net_rr(lane)
    conviction = 35.0 + max(0.0, quality - 55.0) * 0.65
    reasons = [f"quality={quality:.1f}"]

    if net_rr >= 3.5: conviction += 12; reasons.append("net_rr>=3.5")
    elif net_rr >= 3.0: conviction += 9; reasons.append("net_rr>=3.0")
    elif net_rr >= 2.6: conviction += 6; reasons.append("net_rr>=2.6")
    elif net_rr > 0: conviction += 2; reasons.append("net_rr_positive")

    risk_score = _f(risk_score, 100.0); conviction -= max(0.0, risk_score - 25.0) * 0.32
    if risk_score <= 30: conviction += 5; reasons.append("low_risk_score")
    elif risk_score >= 55: reasons.append("elevated_risk_score")

    horizons = _d(matrix.get("horizons")); aligned = 0; opposing = 0; edge_sum = 0.0; edge_count = 0
    for horizon in ("15m", "1h", "4h", "6h", "24h"):
        item = _d(horizons.get(horizon)); hdir = str(item.get("direction") or "").upper(); edge = _f(item.get("edge"))
        if hdir == direction: aligned += 1; edge_sum += edge; edge_count += 1
        elif hdir in {"LONG", "SHORT"}: opposing += 1
    avg_edge = edge_sum / edge_count if edge_count else 0.0
    consensus = str(matrix.get("consensus") or "MIXED").upper(); conflict = bool(matrix.get("horizon_conflict"))
    if aligned >= 5: conviction += 14; reasons.append("5of5_horizon_alignment")
    elif aligned >= 4: conviction += 10; reasons.append("4of5_horizon_alignment")
    elif aligned >= 3: conviction += 5; reasons.append("3of5_horizon_alignment")
    if avg_edge >= 24: conviction += 8; reasons.append("strong_horizon_edge")
    elif avg_edge >= 16: conviction += 4; reasons.append("good_horizon_edge")
    if consensus == direction: conviction += 7; reasons.append("matrix_consensus_aligned")
    elif consensus in {"LONG", "SHORT"}: conviction -= 18; reasons.append("matrix_consensus_opposes")
    if conflict: conviction -= 14; reasons.append("short_long_horizon_conflict")
    if opposing >= 2: conviction -= min(15.0, opposing * 5.0); reasons.append(f"opposing_horizons={opposing}")

    ebest = _d(elliott.get("best")); es = _f(ebest.get("score")); edir = str(ebest.get("direction") or "").upper(); estat = str(elliott.get("status") or "")
    if estat == "CLEAR_COUNT" and es >= 68:
        if edir == direction:
            bonus = 8 if es >= 82 else 5; bonus += 2 if bool(elliott.get("timeframe_agreement")) else 0; conviction += min(10, bonus); reasons.append(f"elliott_aligned={es:.1f}")
        elif edir in {"LONG", "SHORT"}: conviction -= 12 if es >= 82 else 8; reasons.append(f"elliott_conflict={es:.1f}")

    shadow_status = str(lane.get("shadow_calibration_status") or "CALIBRATING")
    shadow_sample = int(_f(lane.get("shadow_calibration_sample"), 0))
    shadow_adjustment = _f(lane.get("shadow_conviction_adjustment"), 0.0)
    if shadow_status == "USABLE" and shadow_sample >= 30 and shadow_adjustment:
        shadow_adjustment = max(-5.0, min(5.0, shadow_adjustment))
        conviction += shadow_adjustment
        reasons.append(f"shadow_history_adjustment={shadow_adjustment:+.1f}")

    conviction = _clip(conviction)
    if conviction >= 90: multiplier, tier = 1.50, "MAX_CONVICTION"
    elif conviction >= 82: multiplier, tier = 1.25, "HIGH"
    elif conviction >= 72: multiplier, tier = 1.00, "NORMAL_PLUS"
    elif conviction >= 62: multiplier, tier = 0.75, "NORMAL"
    elif conviction >= 52: multiplier, tier = 0.50, "LOW"
    else: multiplier, tier = 0.25, "MINIMAL"

    if lane_name == "AGGRESSIVE_PAPER": multiplier = min(multiplier, 0.50); tier = "EARLY_CAPPED"
    elif lane_name == "SWING_PAPER": multiplier = min(multiplier, 1.25)
    elif lane_name == "PRE_EVENT_PAPER":
        multiplier = min(multiplier, 0.25); tier = "PRE_EVENT_TINY"; reasons.append("pre_event_risk_cap=0.25")

    if conflict: multiplier = min(multiplier, 0.50)
    if consensus in {"LONG", "SHORT"} and consensus != direction: multiplier = min(multiplier, 0.25)
    if estat == "CLEAR_COUNT" and edir in {"LONG", "SHORT"} and edir != direction and es >= 82: multiplier = min(multiplier, 0.50)

    event_multiplier = max(0.0, min(1.0, _f(lane.get("event_risk_multiplier"), 1.0)))
    event_type = str(lane.get("event_type") or "NORMAL"); event_severity = str(lane.get("event_severity") or "NORMAL"); event_bias = str(lane.get("event_directional_bias") or "NEUTRAL").upper()
    if event_multiplier < 1.0: multiplier *= event_multiplier; reasons.append(f"event_risk_multiplier={event_multiplier:.2f}")
    if event_bias in {"LONG", "SHORT"} and event_bias != direction and event_severity in {"HIGH", "CRITICAL"}: multiplier = min(multiplier, 0.25); reasons.append("event_direction_conflict")

    breadth_multiplier = max(0.35, min(1.0, _f(lane.get("breadth_risk_multiplier"), 1.0)))
    breadth_regime = str(lane.get("market_breadth_regime") or "MISSING")
    breadth_alignment = str(lane.get("breadth_alignment") or "MISSING")
    breadth_score = _f(lane.get("market_breadth_score"))
    if breadth_multiplier < 1.0:
        multiplier *= breadth_multiplier
        reasons.append(f"breadth_risk_multiplier={breadth_multiplier:.2f}")

    # Historical calibration is deliberately weaker than current hard evidence.
    if shadow_status == "USABLE" and shadow_sample >= 30:
        if shadow_adjustment >= 5: multiplier *= 1.10
        elif shadow_adjustment >= 2.5: multiplier *= 1.05
        elif shadow_adjustment <= -5: multiplier *= 0.80
        elif shadow_adjustment <= -2.5: multiplier *= 0.90

    multiplier = max(0.05, min(1.50, multiplier))

    return {
        "version": VERSION, "paper_only": True, "creates_entry": False, "changes_direction": False,
        "conviction_score": round(conviction,1), "tier": tier, "risk_budget_multiplier": round(multiplier,3),
        "target_account_risk_pct_before_portfolio_brakes": round(multiplier,3), "lane": lane_name, "direction": direction,
        "quality": round(quality,1), "risk_score": round(risk_score,1), "net_rr": round(net_rr,3),
        "aligned_horizons": aligned, "opposing_horizons": opposing, "average_aligned_edge": round(avg_edge,1),
        "horizon_conflict": conflict, "consensus": consensus,
        "elliott": {"status": estat or "MISSING", "direction": edir or None, "score": round(es,1), "timeframe_agreement": bool(elliott.get("timeframe_agreement")), "pattern": ebest.get("pattern")},
        "event_risk": {"event_type": event_type, "severity": event_severity, "directional_bias": event_bias, "risk_multiplier": event_multiplier, "requires_extra_confirmation": bool(lane.get("event_requires_extra_confirmation"))},
        "market_breadth": {"regime": breadth_regime, "score": round(breadth_score,1), "alignment": breadth_alignment, "risk_multiplier": breadth_multiplier},
        "shadow_calibration": {"status": shadow_status, "sample": shadow_sample, "bounded_adjustment": shadow_adjustment, "minimum_sample": 30},
        "reasons": reasons,
        "rule": "Risk rises only with current Heart evidence plus bounded matured history; breadth/event/portfolio brakes dominate and shadow scores are not probabilities."
    }
