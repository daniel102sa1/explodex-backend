from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _target_label(distance_atr: float, target: str) -> str:
    if distance_atr <= 0:
        return "SIN_DATOS"
    limits = {
        "tp1": (2.0, 3.0),
        "tp2": (3.5, 5.0),
        "tp3": (5.5, 7.5),
    }
    realistic, demanding = limits.get(target, (2.0, 3.0))
    if distance_atr <= realistic:
        return "REALISTA"
    if distance_atr <= demanding:
        return "EXIGENTE"
    return "LEJANO"


def apply_prediction_safety(scored: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    """Conservative guard that may downgrade a prediction before READY.

    It never invents probability. It checks whether the directional edge is
    actually meaningful, whether the stop is reasonable relative to ATR, and
    whether TP distances offer enough reward for the planned risk.
    """
    if not prediction:
        return prediction

    result = dict(prediction)
    sequence = dict(result.get("sequence") or {})
    metrics = dict(scored.get("metrics") or {})
    conflicts = list(result.get("conflicts") or [])
    confirmations = list(result.get("confirmations") or [])

    long_score = _f(scored.get("long_score"))
    short_score = _f(scored.get("short_score"))
    direction = str(result.get("direction") or scored.get("direction") or "LONG")
    scoring_direction = str(scored.get("direction") or direction)
    direction_match = direction == scoring_direction
    direction_edge = abs(long_score - short_score)

    ema9 = _f(metrics.get("ema9"))
    ema21 = _f(metrics.get("ema21"))
    ema_known = ema9 > 0 and ema21 > 0
    ema_aligned = ema_known and ((direction == "LONG" and ema9 > ema21) or (direction == "SHORT" and ema9 < ema21))
    trend_15m = str(metrics.get("trend_15m") or "NEUTRAL")
    trend_1h = str(metrics.get("trend_1h") or "NEUTRAL")
    mtf_votes = sum(
        1
        for trend in (trend_15m, trend_1h)
        if (direction == "LONG" and trend == "BULLISH") or (direction == "SHORT" and trend == "BEARISH")
    )

    if not direction_match:
        stability = "CONFLICTO"
    elif direction_edge < 6:
        stability = "INESTABLE"
    elif direction_edge < 12:
        stability = "DESARROLLANDO"
    elif ema_known and not ema_aligned and mtf_votes == 0:
        stability = "DESARROLLANDO"
    else:
        stability = "ESTABLE"

    if stability in {"CONFLICTO", "INESTABLE"}:
        marker = "dirección inestable: LONG y SHORT están demasiado parejos"
        if marker not in conflicts:
            conflicts.append(marker)
    elif stability == "ESTABLE":
        marker = "dirección con ventaja suficiente sobre el lado contrario"
        if marker not in confirmations:
            confirmations.append(marker)

    current = _f(scored.get("current_price"))
    atr_pct = max(_f(metrics.get("atr_pct")), 0.0)
    atr_abs = current * atr_pct / 100 if current > 0 and atr_pct > 0 else 0.0
    trigger = _f(result.get("trigger_price"), current)
    stop = _f(result.get("stop_loss"))
    entry_low = _f(result.get("entry_low"))
    entry_high = _f(result.get("entry_high"))

    stop_distance_abs = abs(trigger - stop) if trigger > 0 and stop > 0 else 0.0
    stop_distance_pct = stop_distance_abs / trigger * 100 if trigger > 0 else 0.0
    stop_distance_atr = stop_distance_abs / atr_abs if atr_abs > 0 else 0.0
    entry_zone_width_abs = abs(entry_high - entry_low) if entry_low > 0 and entry_high > 0 else 0.0
    entry_zone_width_atr = entry_zone_width_abs / atr_abs if atr_abs > 0 else 0.0

    target_info: dict[str, Any] = {}
    for key in ("tp1", "tp2", "tp3"):
        target = _f(result.get(key))
        distance_abs = abs(target - trigger) if target > 0 and trigger > 0 else 0.0
        distance_atr = distance_abs / atr_abs if atr_abs > 0 else 0.0
        distance_pct = distance_abs / trigger * 100 if trigger > 0 else 0.0
        rr = distance_abs / stop_distance_abs if stop_distance_abs > 0 else 0.0
        target_info[key] = {
            "price": round(target, 12) if target else None,
            "distance_pct": round(distance_pct, 3),
            "distance_atr": round(distance_atr, 2),
            "reward_risk": round(rr, 2),
            "feasibility": _target_label(distance_atr, key),
        }

    tp1_far = target_info.get("tp1", {}).get("feasibility") == "LEJANO"
    rr1 = _f(target_info.get("tp1", {}).get("reward_risk"))
    rr2 = _f(target_info.get("tp2", {}).get("reward_risk"))

    # Hard risk checks. These are deliberately conservative while the model is
    # still being calibrated in PAPER.
    stop_too_wide = stop_distance_atr > 2.4 or stop_distance_pct > max(2.5, atr_pct * 2.8)
    rr_poor = rr1 < 1.15 or rr2 < 1.80
    entry_zone_too_wide = entry_zone_width_atr > 0.50
    mtf_against = mtf_votes == 0 and ema_known and not ema_aligned

    # Approximate leverage caps based only on stop distance. This is not a
    # liquidation-price calculation. It limits margin loss if price reaches stop.
    max_margin_loss_benchmark_pct = 10.0
    leverage_for_10pct_stop_loss = (
        max(1.0, min(20.0, max_margin_loss_benchmark_pct / stop_distance_pct))
        if stop_distance_pct > 0
        else 1.0
    )
    leverage_for_5pct_stop_loss = (
        max(1.0, min(10.0, 5.0 / stop_distance_pct))
        if stop_distance_pct > 0
        else 1.0
    )

    hard_blocks: list[str] = []
    if stability in {"CONFLICTO", "INESTABLE"}:
        hard_blocks.append("direction_unstable")
    if tp1_far:
        hard_blocks.append("tp1_too_far")
    if stop_too_wide:
        hard_blocks.append("stop_too_wide")
    if rr_poor:
        hard_blocks.append("reward_risk_poor")
    if entry_zone_too_wide:
        hard_blocks.append("entry_zone_too_wide")
    if mtf_against:
        hard_blocks.append("ema_mtf_conflict")

    human_blocks = {
        "direction_unstable": "dirección inestable; LONG y SHORT todavía no están suficientemente separados",
        "tp1_too_far": "TP1 demasiado lejano para la volatilidad actual",
        "stop_too_wide": "stop demasiado amplio respecto al ATR/precio actual",
        "reward_risk_poor": "relación beneficio/riesgo insuficiente para TP1/TP2",
        "entry_zone_too_wide": "zona de entrada demasiado amplia respecto al ATR",
        "ema_mtf_conflict": "EMA y marcos 15m/1h no acompañan la dirección",
    }
    for key in hard_blocks:
        marker = human_blocks[key]
        if marker not in conflicts:
            conflicts.append(marker)

    if hard_blocks and str(result.get("phase")) in {"ACTIVADO", "PREACTIVACION", "VIGILAR_CONFIRMACION"}:
        result["phase"] = "VIGILAR_CONFLICTOS"

    sequence.update(
        {
            "direction_stability": stability,
            "direction_edge_score": round(direction_edge, 2),
            "scoring_long_score": round(long_score, 2),
            "scoring_short_score": round(short_score, 2),
            "ema_aligned": ema_aligned if ema_known else None,
            "mtf_direction_votes": mtf_votes,
            "target_feasibility": target_info,
            "tp1_far": tp1_far,
            "stop_distance_pct": round(stop_distance_pct, 3),
            "stop_distance_atr": round(stop_distance_atr, 2),
            "entry_zone_width_atr": round(entry_zone_width_atr, 2),
            "reward_risk_tp1": round(rr1, 2),
            "reward_risk_tp2": round(rr2, 2),
            "risk_guard_blocks": hard_blocks,
            "risk_guard_pass": not hard_blocks,
        }
    )

    result["sequence"] = sequence
    result["conflicts"] = conflicts[:14]
    result["confirmations"] = confirmations[:12]
    result["decision_guard"] = {
        "direction_stability": stability,
        "direction_edge_score": round(direction_edge, 2),
        "direction_match": direction_match,
        "ema_aligned": ema_aligned if ema_known else None,
        "mtf_direction_votes": mtf_votes,
        "targets": target_info,
        "stop_distance_pct": round(stop_distance_pct, 3),
        "stop_distance_atr": round(stop_distance_atr, 2),
        "entry_zone_width_atr": round(entry_zone_width_atr, 2),
        "reward_risk_tp1": round(rr1, 2),
        "reward_risk_tp2": round(rr2, 2),
        "risk_guard_pass": not hard_blocks,
        "risk_guard_blocks": hard_blocks,
        "suggested_max_leverage_10pct_margin_loss": round(leverage_for_10pct_stop_loss, 1),
        "suggested_max_leverage_5pct_margin_loss": round(leverage_for_5pct_stop_loss, 1),
        "entry_rule": "Solo considerar entrada con dirección ESTABLE, fase ACTIVADO, sin chase, precio dentro de zona y Risk Guard aprobado.",
        "hold_rule": "Tras entrar, mantener mientras la estructura siga válida, no se rompa el stop y no aparezca conflicto fuerte; TP1 protege, TP2 es objetivo principal y el time-stop limita operaciones sin seguimiento.",
        "certainty_note": "Risk Guard reduce exposiciones malas; no convierte una señal en segura ni garantiza beneficio.",
    }
    return result
