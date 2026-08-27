from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _protective_orders(orders: list[dict[str, Any]], direction: str) -> dict[str, Any]:
    close_side = "SELL" if direction == "LONG" else "BUY"
    relevant = [
        row for row in orders
        if str(row.get("side") or "").upper() == close_side
        and (bool(row.get("reduce_only")) or bool(row.get("close_position")))
    ]
    stops: list[float] = []
    targets: list[float] = []
    for row in relevant:
        order_type = str(row.get("type") or "").upper()
        stop_price = _f(row.get("stop_price"))
        price = _f(row.get("price"))
        trigger = stop_price if stop_price > 0 else price
        if trigger <= 0:
            continue
        if "STOP" in order_type and "TAKE_PROFIT" not in order_type:
            stops.append(trigger)
        elif "TAKE_PROFIT" in order_type or order_type in {"LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"}:
            targets.append(trigger)
    return {
        "stop_prices": sorted(stops),
        "target_prices": sorted(targets),
        "has_protective_stop": bool(stops),
        "has_take_profit": bool(targets),
    }


def build_position_coach(
    position: dict[str, Any],
    analysis: dict[str, Any] | None,
    open_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    analysis = dict(analysis or {})
    prediction = dict(analysis.get("prediction") or {})
    fusion = dict(prediction.get("verdict_fusion") or {})
    zone = dict(prediction.get("entry_zone_engine") or {})

    direction = str(position.get("direction") or "LONG").upper()
    side = 1.0 if direction == "LONG" else -1.0
    entry = _f(position.get("entry_price"))
    mark = _f(position.get("mark_price"), _f(analysis.get("current_price")))
    notional = abs(_f(position.get("notional")))
    leverage = max(1, int(_f(position.get("leverage"), 1)))
    unrealized = _f(position.get("unrealized_pnl"))

    move_pct = ((mark - entry) / entry * 100.0 * side) if entry > 0 and mark > 0 else 0.0
    pnl_on_notional_pct = unrealized / notional * 100.0 if notional > 0 else move_pct
    approximate_margin_roi_pct = pnl_on_notional_pct * leverage

    analysis_direction = str(analysis.get("direction") or prediction.get("direction") or "N/D").upper()
    direction_aligned = analysis_direction == direction
    locks = dict(fusion.get("locks") or {})
    pass_count = int(_f(fusion.get("pass_count")))
    trap = _f(fusion.get("trap_risk"), 50.0)
    decay = _f(fusion.get("decay_risk"), 50.0)
    acceleration = _f(fusion.get("acceleration_score"))
    flow = _f(fusion.get("flow_strength"), 50.0)
    mtf = _f(fusion.get("mtf_strength"), 50.0)
    confidence = _f(fusion.get("technical_confidence"))
    invalidated = bool(fusion.get("invalidated"))
    phase = str(prediction.get("phase") or "N/D")
    zone_state = str(zone.get("state") or "N/D")
    zone_quality = _f(zone.get("quality_score"), -1.0)

    protective = _protective_orders(list(open_orders or []), direction)

    score = 50.0
    score += 14 if direction_aligned else -22
    score += 12 if pass_count >= 5 else 5 if pass_count == 4 else -10
    score += 8 if bool(locks.get("mtf")) else -5
    score += 8 if bool(locks.get("flow")) else -6
    score += 8 if trap <= 45 else -12 if trap >= 65 else 0
    score += 8 if decay <= 50 else -12 if decay >= 72 else 0
    score += 7 if acceleration >= 58 else 0
    score += 5 if phase == "ACTIVADO" else -4 if phase in {"SIN_SETUP", "SIN_DATOS"} else 0
    if invalidated:
        score -= 35
    score = max(0.0, min(100.0, score))

    strengthening = (
        direction_aligned
        and pass_count >= 5
        and trap <= 45
        and decay <= 52
        and acceleration >= 58
        and flow >= 55
    )
    structurally_healthy = (
        direction_aligned
        and pass_count >= 4
        and trap < 62
        and decay < 68
        and not invalidated
    )
    deterioration = (
        invalidated
        or (not direction_aligned and pass_count >= 4)
        or trap >= 72
        or decay >= 78
    )

    if invalidated:
        state = "THESIS_DAMAGED"
        title = "TESIS DAÑADA"
        message = "El análisis actual cruzó su invalidación técnica. No se interpreta como un retroceso normal."
    elif deterioration:
        state = "DETERIORATING"
        title = "CUIDADO · DETERIORO"
        message = "Aumentaron señales contrarias, riesgo de trampa o agotamiento. La operación necesita vigilancia."
    elif strengthening and move_pct >= 0:
        state = "STRENGTHENING"
        title = "SUBIDA FORTALECIÉNDOSE" if direction == "LONG" else "BAJADA FORTALECIÉNDOSE"
        message = "La dirección, flujo y momentum siguen alineados; todavía no aparece deterioro técnico fuerte."
    elif move_pct < 0 and structurally_healthy:
        state = "NORMAL_PULLBACK"
        title = "RETROCESO NORMAL · POR AHORA"
        message = "El precio va contra la entrada, pero la estructura técnica principal todavía se mantiene."
    elif structurally_healthy:
        state = "HEALTHY"
        title = "OPERACIÓN SALUDABLE"
        message = "La tesis principal sigue viva y los filtros técnicos permanecen razonablemente alineados."
    else:
        state = "WATCH"
        title = "VIGILAR"
        message = "No hay invalidación clara, pero la confluencia actual tampoco es suficientemente fuerte para llamarla saludable."

    next_watch: list[str] = []
    if not direction_aligned:
        next_watch.append("La dirección actual de ExplodeX no coincide con tu posición.")
    if trap >= 60:
        next_watch.append(f"Riesgo de trampa elevado: {trap:.0f}/100.")
    if decay >= 65:
        next_watch.append(f"Momentum cansado: decay {decay:.0f}/100.")
    if acceleration >= 58 and direction_aligned:
        next_watch.append(f"Aceleración favorable: {acceleration:.0f}/100.")
    if not protective.get("has_protective_stop"):
        next_watch.append("No se detectó una orden reduce-only/close-position de stop en las órdenes abiertas.")

    return {
        "version": "live_position_coach_v1",
        "state": state,
        "title": title,
        "message": message,
        "health_score": round(score, 1),
        "score_is_probability": False,
        "direction": direction,
        "analysis_direction": analysis_direction,
        "direction_aligned": direction_aligned,
        "entry_price": entry,
        "mark_price": mark,
        "move_pct": round(move_pct, 4),
        "unrealized_pnl": round(unrealized, 8),
        "pnl_on_notional_pct": round(pnl_on_notional_pct, 4),
        "approx_margin_roi_pct": round(approximate_margin_roi_pct, 4),
        "leverage": leverage,
        "locks_passed": pass_count,
        "locks": locks,
        "technical_confidence": round(confidence, 2),
        "trap_risk": round(trap, 2),
        "decay_risk": round(decay, 2),
        "acceleration_score": round(acceleration, 2),
        "flow_strength": round(flow, 2),
        "mtf_strength": round(mtf, 2),
        "prediction_phase": phase,
        "entry_zone_state": zone_state,
        "entry_zone_quality": None if zone_quality < 0 else round(zone_quality, 2),
        "invalidated": invalidated,
        "protective_orders": protective,
        "next_watch": next_watch,
        "note": (
            "Live Position Coach is observational/read-only. Health score is a technical state score, "
            "not a probability and not an instruction to hold, close, add size, or move a stop."
        ),
    }
