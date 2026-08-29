from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.prediction_guarded import build_pre_move_prediction
from app.services.trade_thesis import apply_thesis_to_score, apply_trade_thesis

HEART_VERSION = "explodex_heart_v2_actionable"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stack_actionable(prediction: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return whether the advanced stack itself already authorizes an entry.

    This deliberately does not require the legacy phase label ACTIVADO. The
    fingerprint and Prediction Stack can reach TRADE_NOW/YES slightly earlier;
    requiring both systems to flip at the same instant caused false negatives.
    """
    fingerprint = _dict(prediction.get("premove_fingerprint"))
    stack = _dict(prediction.get("prediction_stack_v5"))
    master = _dict(stack.get("master_decision"))
    risk_veto = _dict(stack.get("risk_veto"))
    timing = _dict(stack.get("entry_timing"))
    sequence = _dict(prediction.get("sequence"))
    decision_guard = _dict(prediction.get("decision_guard"))

    trade_now = bool(fingerprint.get("trade_now_ready")) or str(fingerprint.get("trade_class") or "").upper() == "TRADE_NOW"
    master_yes = str(master.get("state") or "").upper() == "YES"
    timing_enter = str(timing.get("state") or "").upper() in {"ENTER_NOW", "TRADE_NOW"} or trade_now
    veto_clear = not bool(risk_veto.get("blocked"))
    chase_clear = not bool(sequence.get("chase_risk")) and not bool(risk_veto.get("chase"))
    invalidation_clear = not bool(risk_veto.get("invalidated")) and not bool(risk_veto.get("hard_block"))
    risk_guard_pass = bool(sequence.get("risk_guard_pass", decision_guard.get("risk_guard_pass", True)))

    checks = {
        "fingerprint_trade_now": trade_now,
        "master_yes": master_yes,
        "timing_enter": timing_enter,
        "veto_clear": veto_clear,
        "not_chasing": chase_clear,
        "not_invalidated": invalidation_clear,
        "risk_guard_pass": risk_guard_pass,
    }
    missing = [key for key, ok in checks.items() if not ok]
    return all(checks.values()), missing


def _canonical_gate(scored: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    """One actionable READY policy for scanner, live analysis and PAPER."""
    out = dict(scored)
    direction_match = str(prediction.get("direction") or "") == str(out.get("direction") or "")
    phase = str(prediction.get("phase") or "SIN_SETUP")
    prediction_score = _f(prediction.get("preactivation_score"))
    sequence = _dict(prediction.get("sequence"))
    decision_guard = _dict(prediction.get("decision_guard"))
    chase_risk = bool(sequence.get("chase_risk"))
    risk_guard_pass = bool(sequence.get("risk_guard_pass", decision_guard.get("risk_guard_pass", True)))
    risk_guard_blocks = list(sequence.get("risk_guard_blocks") or decision_guard.get("risk_guard_blocks") or [])
    stack_actionable, stack_missing = _stack_actionable(prediction)
    risk_score = _f(out.get("risk_score"), 100.0)

    metrics = dict(out.get("metrics") or {})
    rejects = list(metrics.get("reject_reasons") or [])

    legacy_ready = direction_match and phase == "ACTIVADO" and not chase_risk and risk_guard_pass
    advanced_ready = (
        direction_match
        and stack_actionable
        and risk_score <= 48.0
        and str(out.get("state") or "") != "NO_TRADE"
    )

    if legacy_ready or advanced_ready:
        out["state"] = "READY"
        # Remove stale timing-only rejects when the advanced stack has now
        # explicitly authorized the entry.
        rejects = [
            item for item in rejects
            if item not in {"heart_trigger_not_activated", "pre_move_not_activated"}
        ]
    elif out.get("state") == "READY":
        out["state"] = "PREPARING"
        reason = (
            "heart_direction_conflict"
            if not direction_match
            else "heart_no_chase"
            if chase_risk
            else "heart_risk_guard_blocked"
            if not risk_guard_pass
            else "heart_trigger_not_activated"
        )
        if reason not in rejects:
            rejects.append(reason)

    if (
        out.get("state") == "WATCH"
        and direction_match
        and phase in {"PREACTIVACION", "VIGILAR_CONFIRMACION"}
        and prediction_score >= 72.0
        and risk_score <= 48.0
    ):
        out["state"] = "PREPARING"

    use_prediction_plan = (
        direction_match
        and phase not in {"SIN_SETUP", "SIN_DATOS"}
        and prediction_score >= 55.0
    )
    if use_prediction_plan:
        for key in (
            "entry_low", "entry_high", "stop_loss", "tp1", "tp2", "tp3",
            "expected_duration_min_minutes", "expected_duration_max_minutes",
        ):
            if prediction.get(key) is not None:
                out[key] = prediction.get(key)
        if prediction.get("invalidation_price") is not None:
            out["invalidation_price"] = prediction.get("invalidation_price")

    metrics["reject_reasons"] = rejects
    metrics["heart_version"] = HEART_VERSION
    metrics["pre_move_type"] = prediction.get("type")
    metrics["pre_move_phase"] = phase
    metrics["pre_move_score"] = prediction_score
    metrics["pre_move_trigger"] = prediction.get("trigger_price")
    metrics["pre_move_direction_match"] = direction_match
    metrics["pre_move_chase_risk"] = chase_risk
    metrics["risk_guard_pass"] = risk_guard_pass
    metrics["risk_guard_blocks"] = risk_guard_blocks
    metrics["advanced_stack_actionable"] = stack_actionable
    metrics["advanced_stack_missing"] = stack_missing
    metrics["ready_via"] = "ADVANCED_STACK" if advanced_ready and not legacy_ready else "LEGACY_TRIGGER" if legacy_ready else None
    out["metrics"] = metrics
    out["prediction"] = prediction
    return out


def _market_event(scored: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    metrics = _dict(scored.get("metrics"))
    seq = _dict(prediction.get("sequence"))
    fingerprint = _dict(prediction.get("premove_fingerprint"))
    stack = _dict(prediction.get("prediction_stack_v5"))
    master = _dict(stack.get("master_decision"))

    kind = str(prediction.get("type") or "SIN_SETUP")
    phase = str(prediction.get("phase") or "SIN_SETUP")
    direction = str(prediction.get("direction") or scored.get("direction") or "")
    pre_score = _f(prediction.get("preactivation_score"))
    fp_score = _f(fingerprint.get("fingerprint_score"))

    change_5m = _f(metrics.get("change_5m_pct"))
    change_15m = _f(metrics.get("change_15m_pct"))
    volume_accel = _f(metrics.get("volume_acceleration"), 1.0)
    relative_volume = _f(metrics.get("relative_volume"), 1.0)
    futures_delta = _f(metrics.get("futures_delta_ratio"))
    spot_delta = _f(metrics.get("spot_delta_ratio"))
    oi_change = _f(metrics.get("oi_change_pct"))
    book = _f(metrics.get("order_book_imbalance"))

    evidence: list[str] = []
    risk_flags: list[str] = []
    event = "NORMAL"
    stack_actionable, _ = _stack_actionable(prediction)

    if seq.get("sweep_low") and kind == "REBOTE_LONG":
        event = "LIQUIDITY_SWEEP_REBOUND"
        evidence.append("sweep_low_reclaimed")
    elif seq.get("sweep_high") and kind == "RECHAZO_SHORT":
        event = "LIQUIDITY_SWEEP_REJECTION"
        evidence.append("sweep_high_rejected")
    elif phase == "ACTIVADO" or stack_actionable:
        event = "EXPLOSION_TRIGGERED"
        evidence.append("entry_stack_authorized" if stack_actionable else "planned_trigger_activated")
    elif phase == "ESPERAR_RETEST":
        event = "EXPLOSION_MOVED_WAIT_RETEST"
        risk_flags.append("late_entry_chase_risk")
    elif phase in {"PREACTIVACION", "VIGILAR_CONFIRMACION"} and pre_score >= 72:
        event = "PRE_EXPLOSION_LOADING"
        evidence.append("preactivation_sequence")
    elif change_5m <= -0.8 and change_15m <= -1.0 and volume_accel >= 1.2:
        event = "DUMP_PRESSURE_ACTIVE"
        evidence.append("fast_downside_with_volume")
    elif change_5m >= 0.8 and change_15m >= 1.0 and volume_accel >= 1.2:
        event = "PUMP_PRESSURE_ACTIVE"
        evidence.append("fast_upside_with_volume")

    if volume_accel >= 1.2:
        evidence.append("volume_accelerating")
    if relative_volume >= 1.25:
        evidence.append("relative_volume_expanding")
    if oi_change >= 0.2:
        evidence.append("open_interest_expanding")
    if direction == "LONG":
        if futures_delta >= 0.06:
            evidence.append("futures_buy_flow")
        elif futures_delta <= -0.08:
            risk_flags.append("futures_sell_flow")
        if spot_delta >= 0.04:
            evidence.append("spot_buy_flow")
        elif spot_delta <= -0.08:
            risk_flags.append("spot_sell_flow")
        if book >= 0.05:
            evidence.append("bid_book_support")
    elif direction == "SHORT":
        if futures_delta <= -0.06:
            evidence.append("futures_sell_flow")
        elif futures_delta >= 0.08:
            risk_flags.append("futures_buy_flow")
        if spot_delta <= -0.04:
            evidence.append("spot_sell_flow")
        elif spot_delta >= 0.08:
            risk_flags.append("spot_buy_flow")
        if book <= -0.05:
            evidence.append("ask_book_pressure")

    if seq.get("sell_absorption_rebound"):
        evidence.append("seller_absorption")
    if seq.get("buy_absorption_rejection"):
        evidence.append("buyer_absorption")
    if bool(seq.get("chase_risk")):
        risk_flags.append("do_not_chase")
    if master and str(master.get("state") or "").upper() not in {"YES", "READY", "TRADE_NOW"}:
        risk_flags.append(f"master_{str(master.get('state') or 'unknown').lower()}")

    pressure_index = max(pre_score, fp_score)
    if event in {"PRE_EXPLOSION_LOADING", "EXPLOSION_TRIGGERED"}:
        pressure_index = min(100.0, pressure_index + min(8.0, len(evidence) * 1.2))
    pressure_index = max(0.0, min(100.0, pressure_index - min(12.0, len(risk_flags) * 2.0)))

    return {
        "event": event,
        "direction": direction,
        "phase": phase,
        "prediction_type": kind,
        "pressure_index": round(pressure_index, 2),
        "index_is_probability": False,
        "evidence": list(dict.fromkeys(evidence))[:14],
        "risk_flags": list(dict.fromkeys(risk_flags))[:12],
        "message": "Índice técnico de preparación/expansión; no es probabilidad garantizada.",
    }


def _plan(scored: dict[str, Any], prediction: dict[str, Any], thesis: dict[str, Any] | None) -> dict[str, Any]:
    if thesis and thesis.get("frozen_plan"):
        return {
            "source": "FROZEN_THESIS",
            "direction": thesis.get("direction"),
            "status": thesis.get("status"),
            "action": thesis.get("action"),
            "entry_low": thesis.get("entry_low"),
            "entry_high": thesis.get("entry_high"),
            "trigger_price": thesis.get("trigger_price"),
            "invalidation_price": thesis.get("invalidation_price"),
            "stop_loss": thesis.get("stop_loss"),
            "tp1": thesis.get("tp1"),
            "tp2": thesis.get("tp2"),
            "tp3": thesis.get("tp3"),
            "chase_limit": thesis.get("chase_limit"),
            "do_not_recalculate": True,
        }
    return {
        "source": "CURRENT_HEART",
        "direction": prediction.get("direction") or scored.get("direction"),
        "status": prediction.get("phase"),
        "entry_low": prediction.get("entry_low", scored.get("entry_low")),
        "entry_high": prediction.get("entry_high", scored.get("entry_high")),
        "trigger_price": prediction.get("trigger_price"),
        "invalidation_price": prediction.get("invalidation_price", scored.get("stop_loss")),
        "stop_loss": prediction.get("stop_loss", scored.get("stop_loss")),
        "tp1": prediction.get("tp1", scored.get("tp1")),
        "tp2": prediction.get("tp2", scored.get("tp2")),
        "tp3": prediction.get("tp3", scored.get("tp3")),
        "do_not_recalculate": False,
    }


def _price_in_plan(current: float, plan: dict[str, Any]) -> bool:
    low = _f(plan.get("entry_low"))
    high = _f(plan.get("entry_high"))
    return current > 0 and low > 0 and high > 0 and min(low, high) <= current <= max(low, high)


def _action_decision(*, canonical: dict[str, Any], prediction: dict[str, Any], thesis: dict[str, Any] | None, plan: dict[str, Any]) -> dict[str, Any]:
    direction = str(canonical.get("direction") or prediction.get("direction") or "").upper()
    current = _f(canonical.get("current_price"))
    sequence = _dict(prediction.get("sequence"))
    stack = _dict(prediction.get("prediction_stack_v5"))
    risk_veto = _dict(stack.get("risk_veto"))
    stack_ready, stack_missing = _stack_actionable(prediction)
    in_zone = _price_in_plan(current, plan)

    terminal = bool(thesis and str(thesis.get("status") or "") in {"INVALIDATED", "EXPIRED", "CLOSED"})
    cooldown = bool(thesis and str(thesis.get("action") or "") == "COOLDOWN_NO_CAMBIAR_DE_LADO")
    chase = bool(sequence.get("chase_risk")) or bool(risk_veto.get("chase")) or str(thesis.get("status") if thesis else "") == "NO_CHASE"
    hard_block = bool(risk_veto.get("blocked")) or bool(risk_veto.get("invalidated")) or bool(risk_veto.get("hard_block"))

    execution_allowed = (
        str(canonical.get("state") or "") == "READY"
        and stack_ready
        and in_zone
        and not chase
        and not hard_block
        and not terminal
        and not cooldown
    )

    if execution_allowed:
        action = "ENTRAR_LONG" if direction == "LONG" else "ENTRAR_SHORT"
        reason = "Stack avanzado en TRADE_NOW/YES, sin veto ni chase y precio dentro de la zona."
    elif terminal or cooldown or hard_block:
        action = "NO_ENTRAR"
        reason = "Hay invalidación, veto o cooldown activo."
    elif chase:
        action = "ESPERAR_RETEST"
        reason = "La oportunidad puede existir, pero el precio ya salió de la zona; no perseguir."
    else:
        action = "ESPERAR"
        reason = "Todavía falta autorización completa o que el precio llegue a la zona."

    return {
        "action": action,
        "should_enter": execution_allowed,
        "direction": direction,
        "price_in_entry_zone": in_zone,
        "advanced_stack_ready": stack_ready,
        "advanced_stack_missing": stack_missing,
        "reason": reason,
    }


async def run_explodex_heart(
    db: AsyncSession | None,
    *,
    symbol: str,
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None = None,
    persist_thesis: bool = True,
) -> dict[str, Any]:
    cg = coinglass or {}
    prediction = build_pre_move_prediction(scored, snapshot, cg)
    canonical = _canonical_gate(scored, prediction)

    thesis: dict[str, Any] | None = None
    if db is not None and persist_thesis:
        thesis = await apply_trade_thesis(db, symbol=symbol, score=canonical, prediction=prediction)
        canonical, prediction = apply_thesis_to_score(canonical, prediction, thesis)

    plan = _plan(canonical, prediction, thesis)

    # A frozen thesis may still be WAITING_ENTRY because it was created one scan
    # before the advanced stack became actionable. Re-evaluate action now without
    # changing its direction/levels.
    stack_ready, _ = _stack_actionable(prediction)
    if (
        stack_ready
        and str(canonical.get("state") or "") == "PREPARING"
        and str(scored.get("state") or "") != "NO_TRADE"
        and _f(canonical.get("risk_score"), 100.0) <= 48.0
    ):
        canonical["state"] = "READY"

    decision = _action_decision(canonical=canonical, prediction=prediction, thesis=thesis, plan=plan)
    market_event = _market_event(canonical, prediction)
    execution_allowed = bool(decision["should_enter"])

    heart = {
        "version": HEART_VERSION,
        "symbol": symbol,
        "mission": "detectar la próxima expansión y dar una decisión clara de entrar o no entrar",
        "direction": canonical.get("direction"),
        "state": canonical.get("state"),
        "execution_allowed": execution_allowed,
        "action_decision": decision,
        "market_event": market_event,
        "plan": plan,
        "thesis": thesis,
        "prediction_phase": prediction.get("phase"),
        "prediction_type": prediction.get("type"),
        "score_is_probability": False,
    }
    canonical["explodex_heart"] = heart
    prediction["explodex_heart"] = {
        "version": HEART_VERSION,
        "market_event": market_event,
        "plan": plan,
        "execution_allowed": execution_allowed,
        "action_decision": decision,
    }
    return {"score": canonical, "prediction": prediction, "heart": heart}
