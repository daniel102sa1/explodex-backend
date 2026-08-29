from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.prediction_guarded import build_pre_move_prediction
from app.services.trade_thesis import apply_thesis_to_score, apply_trade_thesis

HEART_VERSION = "explodex_heart_v1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _canonical_gate(scored: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    """Apply one READY/PREPARING policy for scanner, live analysis and PAPER inputs."""
    out = dict(scored)
    direction_match = str(prediction.get("direction") or "") == str(out.get("direction") or "")
    phase = str(prediction.get("phase") or "SIN_SETUP")
    prediction_score = _f(prediction.get("preactivation_score"))
    sequence = _dict(prediction.get("sequence"))
    decision_guard = _dict(prediction.get("decision_guard"))
    chase_risk = bool(sequence.get("chase_risk"))
    risk_guard_pass = bool(sequence.get("risk_guard_pass", decision_guard.get("risk_guard_pass", True)))
    risk_guard_blocks = list(sequence.get("risk_guard_blocks") or decision_guard.get("risk_guard_blocks") or [])

    metrics = dict(out.get("metrics") or {})
    rejects = list(metrics.get("reject_reasons") or [])

    if out.get("state") == "READY" and not (
        direction_match and phase == "ACTIVADO" and not chase_risk and risk_guard_pass
    ):
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

    # Early warning is informational/preparatory only. It never promotes a
    # NO_TRADE candidate and never fabricates READY from a high score.
    if (
        out.get("state") == "WATCH"
        and direction_match
        and phase in {"PREACTIVACION", "VIGILAR_CONFIRMACION"}
        and prediction_score >= 72.0
        and _f(out.get("risk_score"), 100.0) <= 48.0
    ):
        out["state"] = "PREPARING"

    use_prediction_plan = (
        direction_match
        and phase not in {"SIN_SETUP", "SIN_DATOS"}
        and prediction_score >= 55.0
    )
    if use_prediction_plan:
        for key in (
            "entry_low",
            "entry_high",
            "stop_loss",
            "tp1",
            "tp2",
            "tp3",
            "expected_duration_min_minutes",
            "expected_duration_max_minutes",
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
    out["metrics"] = metrics
    out["prediction"] = prediction
    return out


def _market_event(scored: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    """Describe what the market appears to be doing without pretending it is certainty."""
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

    if seq.get("sweep_low") and kind == "REBOTE_LONG":
        event = "LIQUIDITY_SWEEP_REBOUND"
        evidence.append("sweep_low_reclaimed")
    elif seq.get("sweep_high") and kind == "RECHAZO_SHORT":
        event = "LIQUIDITY_SWEEP_REJECTION"
        evidence.append("sweep_high_rejected")
    elif phase == "ACTIVADO":
        event = "EXPLOSION_TRIGGERED"
        evidence.append("planned_trigger_activated")
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

    # This is a technical preparation index, not a calibrated probability.
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
        "message": "Índice técnico de preparación/expansión; no es probabilidad garantizada de subida o bajada.",
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
        "action": (
            "NO_PERSIGAS_ESPERA_RETEST"
            if bool(_dict(prediction.get("sequence")).get("chase_risk"))
            else "ENTRAR_SOLO_SI_ACTIVADO_Y_EN_ZONA"
            if prediction.get("phase") == "ACTIVADO"
            else "ESPERAR_CONFIRMACION"
        ),
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


async def run_explodex_heart(
    db: AsyncSession | None,
    *,
    symbol: str,
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None = None,
    persist_thesis: bool = True,
) -> dict[str, Any]:
    """Single canonical decision path for live analysis, scanner and PAPER research."""
    cg = coinglass or {}
    prediction = build_pre_move_prediction(scored, snapshot, cg)
    canonical = _canonical_gate(scored, prediction)

    thesis: dict[str, Any] | None = None
    if db is not None and persist_thesis:
        thesis = await apply_trade_thesis(
            db,
            symbol=symbol,
            score=canonical,
            prediction=prediction,
        )
        canonical, prediction = apply_thesis_to_score(canonical, prediction, thesis)

    market_event = _market_event(canonical, prediction)
    plan = _plan(canonical, prediction, thesis)
    execution_allowed = (
        str(canonical.get("state") or "") == "READY"
        and str(prediction.get("phase") or "") == "ACTIVADO"
        and not bool(_dict(prediction.get("sequence")).get("chase_risk"))
        and (not thesis or not thesis.get("frozen_plan") or str(thesis.get("status")) == "ENTER_NOW")
    )

    heart = {
        "version": HEART_VERSION,
        "symbol": symbol,
        "mission": "detectar preparación, barridas, presión y próxima expansión antes de perseguir la vela",
        "direction": canonical.get("direction"),
        "state": canonical.get("state"),
        "execution_allowed": execution_allowed,
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
    }
    return {
        "score": canonical,
        "prediction": prediction,
        "heart": heart,
    }
