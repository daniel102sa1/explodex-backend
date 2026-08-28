from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def build_entry_readiness(*, fusion: dict[str, Any], zone: dict[str, Any], phase: str, invalidated: bool) -> dict[str, Any]:
    locks = dict(fusion.get("locks") or {})
    pass_count = int(_f(fusion.get("pass_count")))
    candidate_enter = bool(fusion.get("candidate_enter"))
    fast_track = bool(fusion.get("fast_track"))
    zone_state = str(zone.get("state") or "N/D").upper()
    zone_action = str(zone.get("action") or "").upper()

    if invalidated:
        state, label, reason = "NO_NEW_ENTRY", "NO NUEVA ENTRADA", "La tesis está invalidada."
    elif zone_state == "CHASE" or zone_action == "WAIT_RETEST":
        state, label, reason = "WAIT_RETEST", "ESPERAR RETEST", "El movimiento puede continuar, pero perseguir el precio empeora la entrada."
    elif candidate_enter and pass_count >= 5 and zone_action == "ENTER_ZONE":
        state, label, reason = "ENTRY_READY", "ENTRADA HABILITADA", "La zona y los filtros de entrada están suficientemente alineados."
    elif pass_count >= 5:
        state, label, reason = "WAIT_ZONE", "ESPERAR ZONA", "Hay confluencia, pero la ubicación del precio todavía no es suficientemente limpia."
    elif pass_count == 4:
        state, label, reason = "PREPARING", "PREPARÁNDOSE", "Cuatro locks están listos; aún faltan confirmaciones para una entrada nueva."
    elif phase in {"SIN_SETUP", "SIN_DATOS"}:
        state, label, reason = "NO_CURRENT_SETUP", "SIN SETUP NUEVO", "No hay una entrada nueva activa; esto no implica que una posición ya abierta esté mal."
    else:
        state, label, reason = "WAIT_CONFIRMATION", "FALTA CONFIRMACIÓN", "Aún faltan filtros de entrada."

    return {
        "state": state,
        "label": label,
        "reason": reason,
        "locks_passed": pass_count,
        "locks": locks,
        "candidate_enter": candidate_enter,
        "fast_track": fast_track,
        "zone_state": zone_state,
        "zone_action": zone_action or None,
        "is_new_entry_signal": state == "ENTRY_READY",
    }


def build_continuation_outlook(
    *,
    direction: str,
    analysis_direction: str,
    fusion: dict[str, Any],
    move_pct: float,
    invalidated: bool,
) -> dict[str, Any]:
    locks = dict(fusion.get("locks") or {})
    aligned = analysis_direction == direction
    mtf = _f(fusion.get("mtf_strength"), 50.0)
    flow = _f(fusion.get("flow_strength"), 50.0)
    trap_risk = _f(fusion.get("trap_risk"), 50.0)
    decay_risk = _f(fusion.get("decay_risk"), 50.0)
    acceleration = _f(fusion.get("acceleration_score"), 0.0)
    trap_safety = _clamp(100.0 - trap_risk)
    momentum_quality = _clamp(100.0 - decay_risk)

    if move_pct >= 1.0:
        price_behavior = 88.0
    elif move_pct >= 0.25:
        price_behavior = 78.0
    elif move_pct >= 0:
        price_behavior = 68.0
    elif move_pct > -0.4:
        price_behavior = 48.0
    else:
        price_behavior = 28.0

    score = _clamp(
        (100.0 if aligned else 20.0) * 0.22
        + _clamp(mtf) * 0.18
        + _clamp(flow) * 0.18
        + momentum_quality * 0.14
        + _clamp(acceleration) * 0.10
        + trap_safety * 0.10
        + price_behavior * 0.08
    )

    health_locks = {
        "alignment": aligned,
        "mtf": bool(locks.get("mtf")) or mtf >= 52,
        "flow": bool(locks.get("flow")) or flow >= 55,
        "momentum": bool(locks.get("momentum")) or momentum_quality >= 52 or acceleration >= 58,
        "trap_safe": bool(locks.get("trap")) or trap_safety >= 58,
        "structure": not invalidated and (bool(locks.get("core")) or bool(locks.get("mtf")) or mtf >= 48),
    }
    health_count = sum(1 for value in health_locks.values() if value)

    if invalidated:
        state, strength = "THESIS_DAMAGED", "INVALIDADA"
    elif trap_risk >= 75 or decay_risk >= 82 or (not aligned and mtf < 45):
        state, strength = "CAUTION", "CUIDADO"
    elif score >= 72 and aligned and flow >= 52 and mtf >= 50 and trap_risk < 65:
        state, strength = "CONTINUATION_STRONG", "FUERTE"
    elif score >= 60 and aligned:
        state, strength = "CONTINUATION_MODERATE", "MODERADA"
    elif score >= 48:
        state, strength = "MIXED", "MIXTA"
    else:
        state, strength = "WEAK", "DÉBIL"

    reasons: list[str] = []
    warnings: list[str] = []
    if aligned:
        reasons.append("Dirección actual alineada con tu posición.")
    else:
        warnings.append("La dirección actual ya no coincide con tu posición.")
    if flow >= 60:
        reasons.append(f"Flujo favorable {flow:.0f}/100.")
    elif flow < 45:
        warnings.append(f"Flujo débil {flow:.0f}/100.")
    if mtf >= 55:
        reasons.append(f"Contexto MTF favorable {mtf:.0f}/100.")
    elif mtf < 45:
        warnings.append(f"Contexto MTF débil {mtf:.0f}/100.")
    if trap_safety >= 65:
        reasons.append(f"Seguridad anti-trap {trap_safety:.0f}/100.")
    elif trap_risk >= 65:
        warnings.append(f"Riesgo de trampa alto {trap_risk:.0f}/100.")
    if momentum_quality >= 58:
        reasons.append(f"Momentum conservado {momentum_quality:.0f}/100.")
    elif momentum_quality < 45:
        warnings.append(f"Momentum bajo {momentum_quality:.0f}/100.")
    if acceleration >= 58:
        reasons.append(f"Aceleración favorable {acceleration:.0f}/100.")
    elif acceleration < 30:
        warnings.append(f"Aceleración baja {acceleration:.0f}/100.")
    if move_pct > 0:
        reasons.append(f"Precio {move_pct:+.2f}% a favor desde la entrada.")

    label_direction = "ALCISTA" if direction == "LONG" else "BAJISTA"
    return {
        "version": "continuation_outlook_v1",
        "state": state,
        "strength": strength,
        "label": f"CONTINUACIÓN {label_direction} {strength}" if state in {"CONTINUATION_STRONG", "CONTINUATION_MODERATE"} else strength,
        "score": round(score, 1),
        "score_is_probability": False,
        "trap_safety": round(trap_safety, 1),
        "momentum_quality": round(momentum_quality, 1),
        "health_locks": health_locks,
        "health_locks_passed": health_count,
        "reasons": reasons[:5],
        "warnings": warnings[:5],
        "note": "Continuation score is a technical state score, not a probability that price must continue.",
    }
