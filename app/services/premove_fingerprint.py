from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
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

    early_signals: list[str] = []
    if compressed:
        early_signals.append("compresión")
    if absorption_aligned:
        early_signals.append("absorción alineada")
    if ofi_aligned:
        early_signals.append("OFI alineado")
    if replenishment_aligned:
        early_signals.append("reposición alineada")
    if flow >= 55:
        early_signals.append("flow favorable")
    if acceleration >= 55:
        early_signals.append("aceleración")
    if trap_risk <= 45:
        early_signals.append("anti-trap fuerte")
    if path_aligned:
        early_signals.append("Path Forecast alineado")

    # V2 intentionally stops requiring near-perfection just to reach ARMED.
    # Hard safety blocks remain unchanged: invalidation, hard block and chase.
    if invalidated or hard_block:
        stage = "REJECTED"
    elif fingerprint_score >= 72 and pass_count >= 5 and not chase:
        stage = "ARMED"
    elif fingerprint_score >= 63:
        stage = "BUILDING"
    elif fingerprint_score >= 52:
        stage = "EARLY"
    else:
        stage = "COLD"

    zone_state = str(zone.get("state") or "N/D").upper()
    zone_action = str(zone.get("action") or "N/D").upper()
    zone_quality = _f(zone.get("quality_score"), 0.0)
    zone_distance_atr = _f(zone.get("distance_to_entry_atr"), 99.0)
    trigger_hit = bool(prediction.get("trigger_hit"))

    # A slightly weak acceptable zone can still be usable after the trigger when
    # the rest of the setup is strong. This prevents a good setup from jumping
    # from WAIT directly to CHASE without ever surfacing a usable entry.
    entry_zone_strong = zone_state in {"OPTIMAL", "ACCEPTABLE"} or zone_action == "ENTER_ZONE"
    entry_zone_early = zone_state == "ACCEPTABLE_WEAK" and zone_quality >= 56 and trigger_hit
    entry_location_ready = entry_zone_strong or entry_zone_early

    trigger_conditions = {
        "fingerprint_ready": fingerprint_score >= 68,
        "locks_sufficient": pass_count >= 5,
        "flow_ready": flow >= 52,
        "anti_trap_ready": trap_risk <= 60,
        "momentum_ready": decay_risk <= 68,
        "entry_location_ready": entry_location_ready,
        "not_chasing": not chase,
        "not_invalidated": not invalidated and not hard_block,
        "path_not_conflicting": path_aligned or not path_bias,
    }
    trigger_passes = sum(1 for value in trigger_conditions.values() if value)

    critical_ready = (
        trigger_conditions["locks_sufficient"]
        and trigger_conditions["flow_ready"]
        and trigger_conditions["anti_trap_ready"]
        and trigger_conditions["not_chasing"]
        and trigger_conditions["not_invalidated"]
        and trigger_conditions["path_not_conflicting"]
    )

    # TRADE_NOW v2: quality + critical safety + a usable price location.
    # We no longer require 8/9 plus the strictest stage simultaneously.
    trade_now_ready = (
        critical_ready
        and fingerprint_score >= 70
        and trigger_passes >= 7
        and entry_location_ready
        and (trigger_hit or entry_zone_strong)
    )

    if invalidated or hard_block:
        trade_class = "NO_TRADE"
        grade = "X"
        label = "NO TRADE"
    elif trade_now_ready:
        trade_class = "TRADE_NOW"
        grade = "A+" if fingerprint_score >= 82 and technical >= 80 and trigger_passes >= 8 else "A"
        label = "TRADE NOW"
    elif not chase and not invalidated and fingerprint_score >= 62 and trigger_passes >= 6:
        trade_class = "TRADE_SOON"
        grade = "A" if fingerprint_score >= 74 and trigger_passes >= 7 else "B"
        label = "TRADE SOON"
    elif stage in {"BUILDING", "EARLY", "ARMED"}:
        trade_class = "WATCHLIST"
        grade = "B" if fingerprint_score >= 60 else "C"
        label = "WATCHLIST"
    else:
        trade_class = "NO_TRADE"
        grade = "C" if not invalidated else "X"
        label = "NO TRADE"

    if chase and trade_class in {"TRADE_NOW", "TRADE_SOON"}:
        trade_class = "TRADE_SOON" if fingerprint_score >= 68 and not invalidated else "WATCHLIST"
        grade = "B" if trade_class == "TRADE_SOON" else "C"
        label = "WAIT RETEST"

    missing = [name for name, ok in trigger_conditions.items() if not ok]
    yes_requirements = {
        "fingerprint_70": fingerprint_score >= 70,
        "locks_5": pass_count >= 5,
        "flow_52": flow >= 52,
        "trap_60_or_less": trap_risk <= 60,
        "momentum_fresh": decay_risk <= 68,
        "usable_entry_location": entry_location_ready,
        "not_chasing": not chase,
        "not_invalidated": not invalidated and not hard_block,
        "path_not_conflicting": path_aligned or not path_bias,
    }
    yes_missing = [name for name, ok in yes_requirements.items() if not ok]

    return {
        "version": "premove_fingerprint_v2",
        "direction": direction,
        "phase": phase,
        "stage": stage,
        "fingerprint_score": round(fingerprint_score, 1),
        "score_is_probability": False,
        "trade_class": trade_class,
        "trade_label": label,
        "grade": grade,
        "trade_now_ready": trade_now_ready,
        "steps_to_yes": len(yes_missing),
        "yes_missing": yes_missing,
        "trigger_passes": trigger_passes,
        "trigger_total": len(trigger_conditions),
        "trigger_conditions": trigger_conditions,
        "early_signals": early_signals,
        "missing": missing,
        "components": {key: round(value, 1) for key, value in components.items()},
        "technical_confidence": round(technical, 1),
        "locks_passed": pass_count,
        "zone_state": zone_state,
        "zone_action": zone_action,
        "zone_quality": round(zone_quality, 1),
        "zone_distance_atr": round(zone_distance_atr, 3),
        "entry_zone_strong": entry_zone_strong,
        "entry_zone_early": entry_zone_early,
        "path_aligned": path_aligned,
        "safety": {
            "creates_orders": False,
            "changes_leverage": False,
            "overrides_stop": False,
            "paper_research_first": True,
            "hard_blocks_preserved": True,
        },
        "note": (
            "V2 reduces over-filtering while preserving invalidation, hard-block, chase and trap protections. "
            "TRADE NOW remains a technical readiness classification, not a probability or guarantee of profit."
        ),
    }
