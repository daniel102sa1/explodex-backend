from __future__ import annotations

from typing import Any

from app.services.server_verdict_fusion import build_server_verdict_fusion as build_base_server_verdict_fusion


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _active_plan_score(scored: dict[str, Any], prediction: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Mirror scanner.py's exact plan-selection contract before fusion is calculated."""
    prediction_matches_direction = prediction.get("direction") == scored.get("direction")
    prediction_phase = str(prediction.get("phase", "SIN_SETUP"))
    prediction_score = _f(prediction.get("preactivation_score"))
    use_prediction_plan = (
        prediction_matches_direction
        and prediction_phase not in {"SIN_SETUP", "SIN_DATOS"}
        and prediction_score >= 55
    )

    planned = dict(scored)
    if use_prediction_plan:
        for target, source in (
            ("entry_low", "entry_low"),
            ("entry_high", "entry_high"),
            ("stop_loss", "stop_loss"),
            ("tp1", "tp1"),
            ("tp2", "tp2"),
            ("tp3", "tp3"),
        ):
            value = prediction.get(source)
            if value is not None:
                planned[target] = value
    return planned, use_prediction_plan


def build_guarded_verdict_fusion(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    prediction: dict[str, Any],
    entry_zone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the exact active plan plus a conservative entry-zone gate.

    Entry Zone v2 is a soft quality gate: it may demote ENTER to WAIT, but it
    never creates an entry, promotes a direction, raises leverage, or widens a plan.
    """
    planned, used_prediction_plan = _active_plan_score(scored, prediction)
    fusion = dict(build_base_server_verdict_fusion(planned, snapshot, prediction))
    zone = dict(entry_zone or {})

    state = str(zone.get("state") or "N/D")
    action = str(zone.get("action") or "N/D")
    quality_label = str(zone.get("quality_label") or "N/D")
    quality_score = _f(zone.get("quality_score"), -1.0)

    zone_available = bool(zone.get("available"))
    chase_zone = zone_available and (state == "CHASE" or action == "WAIT_RETEST")
    weak_zone = zone_available and (
        state == "ACCEPTABLE_WEAK"
        or quality_label == "LOW"
        or (quality_score >= 0 and quality_score < 55)
    )
    high_quality_zone = zone_available and (
        state == "OPTIMAL"
        and quality_label == "HIGH"
        and quality_score >= 72
    )

    locks = dict(fusion.get("locks") or {})
    blocked_by_zone = chase_zone or weak_zone
    if blocked_by_zone:
        locks["entry"] = False
        fusion["locks"] = locks
        fusion["pass_count"] = sum(1 for value in locks.values() if bool(value))
        fusion["candidate_enter"] = False
        fusion["fast_track"] = False

    # Do not manufacture confidence. For a genuinely high-quality optimal zone,
    # only preserve the stronger of the already-calculated entry-quality readings.
    if high_quality_zone:
        fusion["entry_quality"] = round(max(_f(fusion.get("entry_quality")), min(100.0, quality_score)), 2)

    # Keep the version understood by the current web client while exposing the
    # stronger contract explicitly in separate metadata. This avoids a fallback
    # to browser-only fusion until the typed frontend migration lands.
    fusion["version"] = "server_parity_v1"
    fusion["guard_version"] = "entry_zone_guard_v2"
    fusion["plan_contract"] = {
        "used_prediction_plan": used_prediction_plan,
        "matches_scanner_plan_selection": True,
    }
    fusion["entry_zone_gate"] = {
        "available": zone_available,
        "state": state,
        "action": action,
        "quality_label": quality_label,
        "quality_score": None if quality_score < 0 else round(quality_score, 2),
        "blocked_by_zone": blocked_by_zone,
        "block_type": "SOFT_WAIT" if blocked_by_zone else None,
        "reason": "chase_wait_retest" if chase_zone else "weak_entry_quality" if weak_zone else None,
        "high_quality_optimal": high_quality_zone,
    }
    fusion["probability_note"] = (
        "technical_confidence and entry-zone quality are technical scores, not next-trade probability."
    )
    return fusion
