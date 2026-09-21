from __future__ import annotations

from typing import Any

VERSION = "evidence_council_v1"


def _d(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _vote(direction: str, strength: float, evidence: list[str] | None = None, *, shadow: bool = False) -> dict[str, Any]:
    direction = str(direction or "NEUTRAL").upper()
    if direction not in {"LONG", "SHORT"}:
        direction = "NEUTRAL"
    return {
        "direction": direction,
        "strength": round(_clip(strength), 2),
        "evidence": list(evidence or [])[:8],
        "shadow_only": shadow,
    }


def build_evidence_council(
    *,
    score: dict[str, Any],
    prediction: dict[str, Any],
    heart: dict[str, Any],
) -> dict[str, Any]:
    """Fuse independent evidence families without double-counting derived labels.

    The council is deliberately not another entry engine. It summarizes whether
    *different* families agree with the primary direction. Each family gets one
    vote so ten indicators from the same category cannot masquerade as ten
    independent confirmations.
    """
    primary = str(heart.get("direction") or prediction.get("direction") or score.get("direction") or "").upper()
    metrics = _d(score.get("metrics"))

    domains: dict[str, dict[str, Any]] = {}

    macro = _d(heart.get("macro_cycle"))
    if macro.get("available"):
        domains["macro_cycle"] = _vote(
            str(macro.get("bias") or "NEUTRAL"),
            _f(macro.get("confidence_score"), 50.0),
            [
                str(macro.get("state") or ""),
                *(list(macro.get("accumulation_evidence") or [])[:3] if str(macro.get("bias")) == "LONG" else list(macro.get("distribution_evidence") or [])[:3]),
            ],
        )

    sarpon = _d(prediction.get("sarpon_classic"))
    if sarpon.get("available"):
        side = str(sarpon.get("side") or primary)
        stage = str(sarpon.get("stage") or "")
        if stage == "RED_INVALIDATED":
            direction = "SHORT" if side == "LONG" else "LONG" if side == "SHORT" else "NEUTRAL"
            strength = max(65.0, 100.0 - _f(sarpon.get("score"), 50.0))
        elif stage in {"GREEN_CONFIRMATION", "YELLOW_FORMING"}:
            direction = side
            strength = _f(sarpon.get("score"), 50.0)
        else:
            direction = "NEUTRAL"
            strength = _f(sarpon.get("score"), 50.0)
        domains["structure_sarpon"] = _vote(
            direction,
            strength,
            [
                stage,
                str(_d(sarpon.get("murphy")).get("structure_aligned")),
                str(_d(sarpon.get("compression_priority")).get("stage") or ""),
            ],
        )

    futures_delta = _f(metrics.get("futures_delta_ratio"))
    spot_delta = _f(metrics.get("spot_delta_ratio"))
    book = _f(metrics.get("order_book_imbalance"))
    oi = _f(metrics.get("oi_change_pct"))
    flow_score = futures_delta * 45.0 + spot_delta * 40.0 + book * 20.0
    if flow_score >= 5.0:
        flow_direction = "LONG"
    elif flow_score <= -5.0:
        flow_direction = "SHORT"
    else:
        flow_direction = "NEUTRAL"
    flow_strength = _clip(50.0 + abs(flow_score) * 2.2 + min(10.0, abs(oi) * 4.0))
    domains["flow_derivatives"] = _vote(
        flow_direction,
        flow_strength,
        [
            f"futures_delta={futures_delta:.3f}",
            f"spot_delta={spot_delta:.3f}",
            f"book={book:.3f}",
            f"oi_change={oi:.3f}",
        ],
    )

    quant = _d(heart.get("quant_brain"))
    if quant.get("available"):
        edge = _f(quant.get("directional_edge"))
        qdir = primary if edge >= 8 else ("SHORT" if primary == "LONG" else "LONG") if edge <= -8 and primary in {"LONG", "SHORT"} else "NEUTRAL"
        qstrength = _clip(50.0 + abs(edge) * 0.55)
        domains["quant"] = _vote(
            qdir,
            qstrength,
            [str(quant.get("stance") or ""), f"edge={edge:.1f}", f"evidence={_f(quant.get('evidence_strength')):.1f}"],
        )

    trajectory = _d(heart.get("trajectory_forecast"))
    if trajectory:
        tdir = str(trajectory.get("direction") or "NEUTRAL")
        domains["trajectory"] = _vote(
            tdir,
            _f(trajectory.get("trajectory_score"), 50.0),
            [
                str(trajectory.get("horizon") or ""),
                f"edge={_f(trajectory.get('direction_edge')):.1f}",
                f"aligned_htf={int(_f(trajectory.get('aligned_htf_frames')))}",
            ],
        )

    decision = _d(heart.get("action_decision"))
    ignition = _d(heart.get("ignition"))
    if bool(decision.get("should_enter")):
        timing_direction = primary
        timing_strength = max(72.0, _f(ignition.get("score"), 72.0))
    elif str(ignition.get("stage") or "").upper() in {"ARMED", "IGNITING"}:
        timing_direction = primary
        timing_strength = _f(ignition.get("score"), 65.0)
    else:
        timing_direction = "NEUTRAL"
        timing_strength = _f(ignition.get("score"), 50.0)
    domains["timing"] = _vote(
        timing_direction,
        timing_strength,
        [str(decision.get("action") or ""), str(ignition.get("stage") or "")],
    )

    research: dict[str, dict[str, Any]] = {}
    murphy = _d(prediction.get("murphy_patterns"))
    if murphy.get("available"):
        top = _d(murphy.get("top_pattern"))
        research["murphy_patterns"] = _vote(
            str(murphy.get("aggregate_bias") or "NEUTRAL"),
            _f(top.get("confidence_score"), 50.0),
            [str(top.get("name") or ""), str(top.get("state") or "")],
            shadow=True,
        )
    formula = _d(prediction.get("formula_brain"))
    if formula.get("available"):
        research["formula_brain"] = _vote(
            str(formula.get("direction") or "NEUTRAL"),
            _f(formula.get("consensus_score"), 50.0),
            [str(formula.get("regime") or ""), *list(formula.get("risk_flags") or [])[:3]],
            shadow=True,
        )

    aligned: list[str] = []
    opposed: list[str] = []
    neutral: list[str] = []
    weighted = 0.0
    weight_total = 0.0
    for name, item in domains.items():
        direction = str(item.get("direction") or "NEUTRAL")
        strength = _f(item.get("strength"), 50.0)
        if direction == primary and primary in {"LONG", "SHORT"}:
            aligned.append(name)
            sign = 1.0
        elif direction in {"LONG", "SHORT"} and primary in {"LONG", "SHORT"} and direction != primary:
            opposed.append(name)
            sign = -1.0
        else:
            neutral.append(name)
            sign = 0.0
        confidence_weight = max(0.25, min(1.0, abs(strength - 50.0) / 35.0))
        weighted += sign * confidence_weight
        weight_total += confidence_weight

    normalized = weighted / weight_total if weight_total > 1e-12 else 0.0
    council_score = _clip(50.0 + normalized * 50.0)
    risk_multiplier = 1.0
    if len(opposed) >= 3:
        risk_multiplier = 0.55
    elif len(opposed) == 2:
        risk_multiplier = 0.70
    elif len(opposed) == 1:
        risk_multiplier = 0.85

    return {
        "version": VERSION,
        "primary_direction": primary,
        "domains": domains,
        "research_shadow_domains": research,
        "aligned_domains": aligned,
        "opposed_domains": opposed,
        "neutral_domains": neutral,
        "independent_support_count": len(aligned),
        "independent_conflict_count": len(opposed),
        "council_score": round(council_score, 2),
        "score_is_probability": False,
        "risk_multiplier_recommendation": risk_multiplier,
        "policy": {
            "can_create_entry": False,
            "can_flip_direction": False,
            "can_override_hard_safety": False,
            "can_raise_risk": False,
            "may_reduce_risk_after_validation": True,
            "one_vote_per_independent_domain": True,
            "shadow_patterns_do_not_count_as_production_votes": True,
        },
        "note": "Council summarizes independent evidence families; it does not manufacture certainty or replace timing/hard-safety rules.",
    }
