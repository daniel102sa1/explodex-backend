from __future__ import annotations

from typing import Any

from app.services.position_continuation_engine import build_continuation_outlook, build_entry_readiness


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _protective_orders(orders: list[dict[str, Any]], direction: str) -> dict[str, Any]:
    close_side = "SELL" if direction == "LONG" else "BUY"
    relevant = [row for row in orders if str(row.get("side") or "").upper() == close_side and (bool(row.get("reduce_only")) or bool(row.get("close_position")))]
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
    return {"stop_prices": sorted(stops), "target_prices": sorted(targets), "has_protective_stop": bool(stops), "has_take_profit": bool(targets)}


def build_position_coach(position: dict[str, Any], analysis: dict[str, Any] | None, open_orders: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
    entry_readiness = build_entry_readiness(fusion=fusion, zone=zone, phase=phase, invalidated=invalidated)
    continuation = build_continuation_outlook(direction=direction, analysis_direction=analysis_direction, fusion=fusion, move_pct=move_pct, invalidated=invalidated)

    continuation_state = str(continuation.get("state") or "MIXED")
    if continuation_state == "THESIS_DAMAGED":
        state, title, message = "THESIS_DAMAGED", "TESIS DAÑADA", "La estructura principal se invalidó; esto ya no se clasifica como un retroceso normal."
    elif continuation_state == "CAUTION":
        state, title, message = "DETERIORATING", "CUIDADO · DETERIORO", "La continuación perdió calidad por riesgo de trampa, agotamiento o conflicto de dirección."
    elif continuation_state == "CONTINUATION_STRONG" and move_pct >= 0:
        state = "STRENGTHENING"
        title = "SUBIDA FORTALECIÉNDOSE" if direction == "LONG" else "BAJADA FORTALECIÉNDOSE"
        message = "La posición mantiene continuación técnica fuerte. Puede ocurrir aunque una nueva entrada ya no tenga 6/6 locks."
    elif continuation_state in {"CONTINUATION_STRONG", "CONTINUATION_MODERATE"} and move_pct < 0:
        state, title, message = "NORMAL_PULLBACK", "RETROCESO NORMAL · POR AHORA", "El precio retrocede, pero estructura, flujo y contexto todavía conservan soporte técnico."
    elif continuation_state == "CONTINUATION_MODERATE":
        state, title, message = "HEALTHY", "OPERACIÓN SALUDABLE", "La continuación sigue favorable, aunque no con fuerza máxima."
    else:
        state, title, message = "WATCH", "VIGILAR", "La continuación actual es mixta o débil; todavía no hay invalidación definitiva."

    next_watch = list(continuation.get("warnings") or [])
    if not protective.get("has_protective_stop"):
        next_watch.append("No se detectó una orden reduce-only/close-position de stop en las órdenes abiertas.")

    return {
        "version": "live_position_coach_v2",
        "state": state,
        "title": title,
        "message": message,
        "health_score": round(_f(continuation.get("score"), 50.0), 1),
        "score_is_probability": False,
        "direction": direction,
        "analysis_direction": analysis_direction,
        "direction_aligned": analysis_direction == direction,
        "entry_price": entry,
        "mark_price": mark,
        "move_pct": round(move_pct, 4),
        "unrealized_pnl": round(unrealized, 8),
        "pnl_on_notional_pct": round(pnl_on_notional_pct, 4),
        "approx_margin_roi_pct": round(approximate_margin_roi_pct, 4),
        "leverage": leverage,
        "locks_passed": pass_count,
        "locks": locks,
        "entry_readiness": entry_readiness,
        "continuation_outlook": continuation,
        "technical_confidence": round(confidence, 2),
        "trap_risk": round(trap, 2),
        "trap_safety": round(100.0 - trap, 2),
        "decay_risk": round(decay, 2),
        "momentum_quality": round(100.0 - decay, 2),
        "acceleration_score": round(acceleration, 2),
        "flow_strength": round(flow, 2),
        "mtf_strength": round(mtf, 2),
        "prediction_phase": phase,
        "entry_zone_state": zone_state,
        "entry_zone_quality": None if zone_quality < 0 else round(zone_quality, 2),
        "invalidated": invalidated,
        "protective_orders": protective,
        "next_watch": next_watch[:6],
        "note": "Entry Locks answer whether a new entry is ready. Continuation Outlook answers whether an already-open position still has technical support. Neither is a probability or guarantee.",
    }
