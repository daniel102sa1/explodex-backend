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
    """Add a conservative decision guard on top of the pre-move predictor.

    This layer intentionally does not create a new probability. It prevents a
    tiny LONG-vs-SHORT score difference from looking decisive and audits whether
    R-multiple targets are plausible relative to current ATR.
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
        if str(result.get("phase")) in {"ACTIVADO", "PREACTIVACION", "VIGILAR_CONFIRMACION"}:
            result["phase"] = "VIGILAR_CONFLICTOS"
    elif stability == "ESTABLE":
        marker = "dirección con ventaja suficiente sobre el lado contrario"
        if marker not in confirmations:
            confirmations.append(marker)

    current = _f(scored.get("current_price"))
    atr_pct = max(_f(metrics.get("atr_pct")), 0.0)
    atr_abs = current * atr_pct / 100 if current > 0 and atr_pct > 0 else 0.0
    trigger = _f(result.get("trigger_price"), current)

    target_info: dict[str, Any] = {}
    for key in ("tp1", "tp2", "tp3"):
        target = _f(result.get(key))
        distance_abs = abs(target - trigger) if target > 0 and trigger > 0 else 0.0
        distance_atr = distance_abs / atr_abs if atr_abs > 0 else 0.0
        distance_pct = distance_abs / trigger * 100 if trigger > 0 else 0.0
        target_info[key] = {
            "price": round(target, 12) if target else None,
            "distance_pct": round(distance_pct, 3),
            "distance_atr": round(distance_atr, 2),
            "feasibility": _target_label(distance_atr, key),
        }

    tp1_far = target_info.get("tp1", {}).get("feasibility") == "LEJANO"
    if tp1_far:
        marker = "TP1 lejano para la volatilidad actual; exigir seguimiento fuerte"
        if marker not in conflicts:
            conflicts.append(marker)

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
        }
    )

    result["sequence"] = sequence
    result["conflicts"] = conflicts[:12]
    result["confirmations"] = confirmations[:12]
    result["decision_guard"] = {
        "direction_stability": stability,
        "direction_edge_score": round(direction_edge, 2),
        "direction_match": direction_match,
        "ema_aligned": ema_aligned if ema_known else None,
        "mtf_direction_votes": mtf_votes,
        "targets": target_info,
        "entry_rule": "Solo considerar entrada con dirección ESTABLE, fase ACTIVADO, sin chase y precio dentro de zona.",
        "hold_rule": "Tras entrar, mantener mientras la estructura siga válida, no se rompa el stop y no aparezca conflicto fuerte; TP1 protege, TP2 es objetivo principal y el time-stop limita operaciones sin seguimiento.",
        "certainty_note": "ESTABLE no significa seguro; significa que LONG/SHORT ya no están casi empatados según las reglas actuales.",
    }
    return result
