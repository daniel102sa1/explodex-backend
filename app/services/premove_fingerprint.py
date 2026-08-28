from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def build_premove_fingerprint(scored: dict[str, Any], snapshot: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    fusion = dict(prediction.get("verdict_fusion") or {})
    zone = dict(prediction.get("entry_zone_engine") or {})
    sequence = dict(prediction.get("sequence") or {})
    context = dict(prediction.get("context_engine") or {})
    micro = dict(context.get("microstructure") or {})
    path = dict(prediction.get("path_forecast") or {})

    direction = str(prediction.get("direction") or scored.get("direction") or "LONG").upper()
    phase = str(prediction.get("phase") or "SIN_SETUP")
    prep = _f(prediction.get("preactivation_score"), 0.0)
    setup = _f(scored.get("setup_score"), 0.0)
    technical = _f(fusion.get("technical_confidence"), prep)
    mtf = _f(fusion.get("mtf_strength"), 50.0)
    flow = _f(fusion.get("flow_strength"), 50.0)
    trap_risk = _f(fusion.get("trap_risk"), 50.0)
    decay_risk = _f(fusion.get("decay_risk"), 50.0)
    acceleration = _f(fusion.get("acceleration_score"), 0.0)
    entry_quality = _f(fusion.get("entry_quality"), _f(zone.get("quality_score"), 0.0))
    pass_count = int(_f(fusion.get("pass_count"), 0))
    hard_block = bool(fusion.get("hard_block"))
    invalidated = bool(fusion.get("invalidated"))
    chase = bool(sequence.get("chase_risk")) or str(zone.get("state") or "").upper() == "CHASE"

    compressed = bool(sequence.get("compressed"))
    seq_ready = bool(sequence.get("sequential_microstructure_ready")) or bool(micro.get("sequential_ready"))
    absorption = _f(sequence.get("sequential_absorption"), _f(micro.get("sequential_absorption"), 0.0))
    ofi = _f(sequence.get("ofi"), _f(micro.get("ofi"), 0.0))
    replenishment = _f(sequence.get("replenishment"), _f(micro.get("replenishment"), 0.0))
    path_bias = str(path.get("final_bias") or "").upper()
    path_clear = str(path.get("clarity") or "") in {"CLEAR", "USABLE"}
    path_aligned = path_bias == direction

    side = 1.0 if direction == "LONG" else -1.0
    absorption_aligned = absorption * side > 0.20
    ofi_aligned = ofi * side > 0.12
    replenishment_aligned = replenishment * side > 0.15

    components = {
        "compression": 100.0 if compressed else 35.0,
        "absorption": 85.0 if absorption_aligned else 60.0 if seq_ready else 35.0,
        "flow_shift": _clamp(flow),
        "mtf": _clamp(mtf),
        "acceleration": _clamp(acceleration),
        "anti_trap": _clamp(100.0 - trap_risk),
        "momentum_freshness": _clamp(100.0 - decay_risk),
        "entry_location": _clamp(entry_quality),
        "path_alignment": 85.0 if path_aligned and path_clear else 65.0 if path_aligned else 35.0,
        "micro_confirmation": 85.0 if (ofi_aligned or replenishment_aligned) else 60.0 if seq_ready else 35.0,
    }

    fingerprint_score = _clamp(
        components["compression"] * 0.08
        + components["absorption"] * 0.10
        + components["flow_shift"] * 0.13
        + components["mtf"] * 0.13
        + components["acceleration"] * 0.12
        + components["anti_trap"] * 0.11
        + components["momentum_freshness"] * 0.10
        + components["entry_location"] * 0.11
        + components["path_alignment"] * 0.07
        + components["micro_confirmation"] * 0.05
    )

    early_signals = []
    if compressed:
        early_signals.append("compresión")
    if absorption_aligned:
        early_signals.append("absorción alineada")
    if ofi_aligned:
        early_signals.append("OFI alineado")
    if replenishment_aligned:
        early_signals.append("reposición alineada")
    if flow >= 58:
        early_signals.append("flow favorable")
    if acceleration >= 58:
        early_signals.append("aceleración")
    if trap_risk <= 42:
        early_signals.append("anti-trap fuerte")
    if path_aligned:
        early_signals.append("Path Forecast alineado")

    if invalidated or hard_block:
        stage = "REJECTED"
    elif fingerprint_score >= 78 and pass_count >= 5 and not chase:
        stage = "ARMED"
    elif fingerprint_score >= 66:
        stage = "BUILDING"
    elif fingerprint_score >= 54:
        stage = "EARLY"
    else:
        stage = "COLD"

    zone_state = str(zone.get("state") or "N/D").upper()
    zone_action = str(zone.get("action") or "N/D").upper()
    trigger_hit = bool(prediction.get("trigger_hit"))

    trigger_conditions = {
        "fingerprint_ready": fingerprint_score >= 74,
        "locks_sufficient": pass_count >= 5,
        "flow_ready": flow >= 55,
        "anti_trap_ready": trap_risk <= 55,
        "momentum_ready": decay_risk <= 62,
        "entry_location_ready": zone_state in {"OPTIMAL", "ACCEPTABLE"} or zone_action == "ENTER_ZONE",
        "not_chasing": not chase,
        "not_invalidated": not invalidated and not hard_block,
        "path_not_conflicting": path_aligned or not path_bias,
    }
    trigger_passes = sum(1 for v in trigger_conditions.values() if v)

    if invalidated or hard_block:
        trade_class = "NO_TRADE"
        grade = "X"
        label = "NO TRADE"
    elif stage == "ARMED" and trigger_passes >= 8 and (trigger_hit or zone_action == "ENTER_ZONE"):
        trade_class = "TRADE_NOW"
        grade = "A+" if fingerprint_score >= 84 and technical >= 82 else "A"
        label = "TRADE NOW"
    elif stage in {"ARMED", "BUILDING"} and trigger_passes >= 7:
        trade_class = "TRADE_SOON"
        grade = "A" if fingerprint_score >= 76 else "B"
        label = "TRADE SOON"
    elif stage in {"BUILDING", "EARLY"}:
        trade_class = "WATCHLIST"
        grade = "B" if fingerprint_score >= 62 else "C"
        label = "WATCHLIST"
    else:
        trade_class = "NO_TRADE"
        grade = "C" if not invalidated else "X"
        label = "NO TRADE"

    if chase and trade_class == "TRADE_NOW":
        trade_class = "TRADE_SOON"
        grade = "B"
        label = "WAIT RETEST"

    missing = [name for name, ok in trigger_conditions.items() if not ok]

    return {
        "version": "premove_fingerprint_v1",
        "direction": direction,
        "phase": phase,
        "stage": stage,
        "fingerprint_score": round(fingerprint_score, 1),
        "score_is_probability": False,
        "trade_class": trade_class,
        "trade_label": label,
        "grade": grade,
        "trigger_passes": trigger_passes,
        "trigger_total": len(trigger_conditions),
        "trigger_conditions": trigger_conditions,
        "early_signals": early_signals,
        "missing": missing,
        "components": {k: round(v, 1) for k, v in components.items()},
        "technical_confidence": round(technical, 1),
        "locks_passed": pass_count,
        "zone_state": zone_state,
        "zone_action": zone_action,
        "path_aligned": path_aligned,
        "safety": {
            "creates_orders": False,
            "changes_leverage": False,
            "overrides_stop": False,
            "paper_research_first": True,
        },
        "note": (
            "TRADE NOW/TRADE SOON classify technical readiness, not certainty. "
            "The fingerprint score is not the probability that the next trade will win."
        ),
    }
