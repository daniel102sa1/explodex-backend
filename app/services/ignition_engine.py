from __future__ import annotations

from typing import Any

VERSION = "ignition_engine_v1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clip(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def build_ignition_signal(scored: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    """Estimate whether a prepared setup is beginning to ignite.

    This is a technical timing index, not a calibrated probability. The fast
    path cannot bypass master direction, hard veto, chase protection,
    invalidation or Risk Guard.
    """
    metrics = _d(scored.get("metrics"))
    fingerprint = _d(prediction.get("premove_fingerprint"))
    stack = _d(prediction.get("prediction_stack_v5"))
    master = _d(stack.get("master_decision"))
    risk_veto = _d(stack.get("risk_veto"))
    sequence = _d(prediction.get("sequence"))
    decision_guard = _d(prediction.get("decision_guard"))

    direction = str(prediction.get("direction") or scored.get("direction") or "").upper()
    sign = -1.0 if direction == "SHORT" else 1.0

    futures = sign * _f(metrics.get("futures_delta_ratio"))
    spot = sign * _f(metrics.get("spot_delta_ratio"))
    book = sign * _f(metrics.get("order_book_imbalance"))
    change_5m = sign * _f(metrics.get("change_5m_pct"))
    change_15m = sign * _f(metrics.get("change_15m_pct"))
    oi = _f(metrics.get("oi_change_pct"))
    volume_accel = _f(metrics.get("volume_acceleration"), 1.0)
    relative_volume = _f(metrics.get("relative_volume"), 1.0)

    pre = _f(prediction.get("preactivation_score"))
    fp = _f(fingerprint.get("fingerprint_score"))

    preparation = _clip(max(pre, fp))
    flow = _clip(max(0.0, futures) * 430.0 + max(0.0, spot) * 430.0)
    volume = _clip(max(0.0, volume_accel - 1.0) * 95.0 + max(0.0, relative_volume - 1.0) * 65.0)
    open_interest = _clip(max(0.0, oi) * 170.0)
    order_book = _clip(max(0.0, book) * 850.0)
    momentum = _clip(max(0.0, change_5m) * 42.0 + max(0.0, change_15m) * 24.0)

    structure = 0.0
    phase = str(prediction.get("phase") or "")
    if phase == "ACTIVADO":
        structure = 100.0
    elif phase in {"PREACTIVACION", "VIGILAR_CONFIRMACION"}:
        structure = 72.0
    elif phase == "ESPERAR_RETEST":
        structure = 35.0
    elif phase not in {"SIN_SETUP", "SIN_DATOS", ""}:
        structure = 50.0
    if sequence.get("sweep_low") or sequence.get("sweep_high"):
        structure = min(100.0, structure + 10.0)
    if sequence.get("sell_absorption_rebound") or sequence.get("buy_absorption_rejection"):
        structure = min(100.0, structure + 10.0)

    components = {
        "preparation": round(preparation, 1),
        "flow": round(flow, 1),
        "volume": round(volume, 1),
        "open_interest": round(open_interest, 1),
        "order_book": round(order_book, 1),
        "momentum": round(momentum, 1),
        "structure": round(structure, 1),
    }
    score = (
        preparation * 0.27
        + flow * 0.21
        + volume * 0.15
        + open_interest * 0.10
        + order_book * 0.11
        + momentum * 0.08
        + structure * 0.08
    )

    supporting = sum(1 for value in components.values() if value >= 62.0)
    strong_supporting = sum(1 for value in components.values() if value >= 78.0)

    master_yes = str(master.get("state") or "").upper() == "YES"
    veto_clear = not bool(risk_veto.get("blocked"))
    not_chasing = not bool(sequence.get("chase_risk")) and not bool(risk_veto.get("chase"))
    not_invalidated = not bool(risk_veto.get("invalidated")) and not bool(risk_veto.get("hard_block"))
    risk_guard_pass = bool(sequence.get("risk_guard_pass", decision_guard.get("risk_guard_pass", True)))
    direction_match = direction == str(scored.get("direction") or "").upper()
    risk_ok = _f(scored.get("risk_score"), 100.0) <= 48.0
    base_state_ok = str(scored.get("state") or "") != "NO_TRADE"

    hard_checks = {
        "master_yes": master_yes,
        "veto_clear": veto_clear,
        "not_chasing": not_chasing,
        "not_invalidated": not_invalidated,
        "risk_guard_pass": risk_guard_pass,
        "direction_match": direction_match,
        "risk_ok": risk_ok,
        "base_state_ok": base_state_ok,
    }

    score = _clip(score)
    if not master_yes:
        score = max(0.0, score - 12.0)
    if not risk_guard_pass or not veto_clear or not not_invalidated:
        score = max(0.0, score - 24.0)
    if not direction_match:
        score = max(0.0, score - 18.0)

    fast_path_ready = score >= 82.0 and supporting >= 4 and strong_supporting >= 2 and all(hard_checks.values())

    if not veto_clear or not not_invalidated or not risk_guard_pass:
        stage = "BLOCKED"
    elif fast_path_ready:
        stage = "IGNITING"
    elif score >= 72.0 and supporting >= 3:
        stage = "ARMED"
    elif score >= 58.0:
        stage = "LOADING"
    else:
        stage = "QUIET"

    evidence: list[str] = []
    if flow >= 65:
        evidence.append("aggressive_flow_aligned")
    if volume >= 65:
        evidence.append("volume_expansion")
    if open_interest >= 60:
        evidence.append("oi_expanding")
    if order_book >= 65:
        evidence.append("orderbook_pressure_aligned")
    if momentum >= 60:
        evidence.append("momentum_starting")
    if preparation >= 78:
        evidence.append("pre_move_prepared")
    if structure >= 70:
        evidence.append("structure_ready")

    blockers = [key for key, ok in hard_checks.items() if not ok]
    return {
        "version": VERSION,
        "direction": direction,
        "score": round(score, 1),
        "stage": stage,
        "fast_path_ready": fast_path_ready,
        "supporting_components": supporting,
        "strong_components": strong_supporting,
        "components": components,
        "evidence": evidence,
        "blockers": blockers,
        "index_is_probability": False,
        "message": "Índice de ignición/timing; no es probabilidad garantizada.",
    }
