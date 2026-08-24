from __future__ import annotations

from statistics import mean
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _block_extrema(values: list[float], *, blocks: int = 3, block_size: int = 3, mode: str = "min") -> list[float]:
    needed = blocks * block_size
    if len(values) < needed:
        return []
    chunk = values[-needed:]
    result: list[float] = []
    for i in range(blocks):
        part = chunk[i * block_size : (i + 1) * block_size]
        result.append(min(part) if mode == "min" else max(part))
    return result


def _rising(values: list[float], tolerance: float = 0.0) -> bool:
    return len(values) >= 3 and values[1] >= values[0] * (1 - tolerance) and values[2] > values[1]


def _falling(values: list[float], tolerance: float = 0.0) -> bool:
    return len(values) >= 3 and values[1] <= values[0] * (1 + tolerance) and values[2] < values[1]


def _wick_features(kline: list[Any]) -> dict[str, float]:
    if len(kline) < 5:
        return {"body": 0.0, "upper": 0.0, "lower": 0.0, "range": 0.0}
    o, h, l, c = map(_f, [kline[1], kline[2], kline[3], kline[4]])
    body = abs(c - o)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - l)
    return {"body": body, "upper": upper, "lower": lower, "range": max(0.0, h - l)}


def _side_alignment(metrics: dict[str, Any], direction: str, coinglass: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    confirmations: list[str] = []
    conflicts: list[str] = []

    rv = _f(metrics.get("relative_volume"), 1.0)
    va = _f(metrics.get("volume_acceleration"), 1.0)
    futures_delta = _f(metrics.get("futures_delta_ratio"))
    spot_delta = _f(metrics.get("spot_delta_ratio"))
    book = _f(metrics.get("order_book_imbalance"))
    oi_local = _f(metrics.get("oi_change_pct"))
    taker_local = _f(metrics.get("taker_avg_3"), 1.0)
    btc = str(metrics.get("btc_trend", "NEUTRAL"))

    cg_oi = coinglass.get("open_interest", {}) if isinstance(coinglass, dict) else {}
    cg_taker = coinglass.get("taker", {}) if isinstance(coinglass, dict) else {}
    oi_15m = _f(cg_oi.get("change_15m_pct")) if cg_oi.get("available") else 0.0
    taker_cg = _f(cg_taker.get("buy_sell_ratio"), 1.0) if cg_taker.get("available") else None

    score = 0
    if rv >= 1.25:
        score += 8
        confirmations.append("volumen relativo expandiéndose")
    if va >= 1.15:
        score += 8
        confirmations.append("volumen acelerando")

    if direction == "LONG":
        if futures_delta >= 0.06:
            score += 9
            confirmations.append("flujo de futuros comprador")
        elif futures_delta <= -0.08:
            conflicts.append("flujo de futuros vendedor")
        if spot_delta >= 0.04:
            score += 11
            confirmations.append("spot comprador")
        elif spot_delta <= -0.08:
            conflicts.append("spot vendedor")
        if book >= 0.05:
            score += 6
            confirmations.append("libro inclinado a compras")
        if taker_local >= 1.08:
            score += 7
            confirmations.append("taker local comprador")
        if taker_cg is not None and taker_cg >= 1.06:
            score += 7
            confirmations.append("taker agregado comprador")
        elif taker_cg is not None and taker_cg <= 0.94:
            conflicts.append("taker agregado vendedor")
        if oi_local >= 0.20 or oi_15m >= 0.20:
            score += 8
            confirmations.append("interés abierto creciendo")
        if btc != "BEARISH":
            score += 4
        else:
            conflicts.append("BTC contrario al LONG")
    else:
        if futures_delta <= -0.06:
            score += 9
            confirmations.append("flujo de futuros vendedor")
        elif futures_delta >= 0.08:
            conflicts.append("flujo de futuros comprador")
        if spot_delta <= -0.04:
            score += 11
            confirmations.append("spot vendedor")
        elif spot_delta >= 0.08:
            conflicts.append("spot comprador")
        if book <= -0.05:
            score += 6
            confirmations.append("libro inclinado a ventas")
        if taker_local <= 0.92:
            score += 7
            confirmations.append("taker local vendedor")
        if taker_cg is not None and taker_cg <= 0.94:
            score += 7
            confirmations.append("taker agregado vendedor")
        elif taker_cg is not None and taker_cg >= 1.06:
            conflicts.append("taker agregado comprador")
        if oi_local >= 0.20 or oi_15m >= 0.20:
            score += 8
            confirmations.append("interés abierto creciendo")
        if btc != "BULLISH":
            score += 4
        else:
            conflicts.append("BTC contrario al SHORT")

    return score, confirmations, conflicts


def build_pre_move_prediction(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify the phase BEFORE an expansion, bounce or rejection.

    This does not claim certainty. It builds an auditable pre-activation plan from
    sequencing: compression -> volume acceleration -> flow/OI response -> level
    pressure -> trigger.  The output is intentionally separate from setup_score.
    """

    coinglass = coinglass or {}
    metrics = dict(scored.get("metrics") or {})
    klines = snapshot.get("klines") or []
    if len(klines) < 20:
        return {
            "type": "SIN_SETUP",
            "direction": scored.get("direction", "LONG"),
            "phase": "SIN_DATOS",
            "preactivation_score": 0.0,
            "message": "No hay suficientes velas para anticipar el movimiento.",
        }

    opens = [_f(k[1]) for k in klines]
    highs = [_f(k[2]) for k in klines]
    lows = [_f(k[3]) for k in klines]
    closes = [_f(k[4]) for k in klines]
    current = closes[-1]
    atr_pct = max(_f(metrics.get("atr_pct"), 0.5), 0.05)
    atr_abs = current * atr_pct / 100

    prior_high = max(highs[-13:-1])
    prior_low = min(lows[-13:-1])
    high_48 = max(highs[-48:]) if len(highs) >= 48 else max(highs)
    low_48 = min(lows[-48:]) if len(lows) >= 48 else min(lows)
    dist_high_atr = (prior_high - current) / atr_abs if atr_abs else 99.0
    dist_low_atr = (current - prior_low) / atr_abs if atr_abs else 99.0

    low_blocks = _block_extrema(lows[:-1], mode="min")
    high_blocks = _block_extrema(highs[:-1], mode="max")
    higher_lows = _rising(low_blocks, tolerance=0.002)
    lower_highs = _falling(high_blocks, tolerance=0.002)

    last = klines[-1]
    prev = klines[-2]
    last_wick = _wick_features(last)
    prev_wick = _wick_features(prev)
    last_close = closes[-1]
    last_low = lows[-1]
    last_high = highs[-1]
    sweep_low = last_low < prior_low and last_close > prior_low
    sweep_high = last_high > prior_high and last_close < prior_high
    lower_rejection = last_wick["lower"] > max(last_wick["body"] * 1.4, atr_abs * 0.12)
    upper_rejection = last_wick["upper"] > max(last_wick["body"] * 1.4, atr_abs * 0.12)

    compressed = bool(metrics.get("compressed"))
    compression_ratio = _f(metrics.get("compression_ratio"), 1.0)
    volume_acceleration = _f(metrics.get("volume_acceleration"), 1.0)
    relative_volume = _f(metrics.get("relative_volume"), 1.0)
    futures_delta = _f(metrics.get("futures_delta_ratio"))
    spot_delta = _f(metrics.get("spot_delta_ratio"))
    book = _f(metrics.get("order_book_imbalance"))
    change_5m = _f(metrics.get("change_5m_pct"))
    change_15m = _f(metrics.get("change_15m_pct"))
    taker = _f(metrics.get("taker_avg_3"), 1.0)

    long_flow_score, long_conf, long_conflicts = _side_alignment(metrics, "LONG", coinglass)
    short_flow_score, short_conf, short_conflicts = _side_alignment(metrics, "SHORT", coinglass)

    # Continuation / expansion scores. The idea is to reward preparation BEFORE
    # the break, not a candle that is already several ATRs away from the level.
    long_breakout = long_flow_score
    short_breakdown = short_flow_score
    long_breakout_conf = list(long_conf)
    short_breakdown_conf = list(short_conf)

    if compressed or compression_ratio <= 0.62:
        long_breakout += 14
        short_breakdown += 14
        long_breakout_conf.append("volatilidad comprimida")
        short_breakdown_conf.append("volatilidad comprimida")
    if higher_lows:
        long_breakout += 12
        long_breakout_conf.append("mínimos crecientes contra resistencia")
    if lower_highs:
        short_breakdown += 12
        short_breakdown_conf.append("máximos decrecientes contra soporte")
    if -0.35 <= dist_high_atr <= 1.25:
        long_breakout += 10
        long_breakout_conf.append("presión cerca de resistencia")
    if -0.35 <= dist_low_atr <= 1.25:
        short_breakdown += 10
        short_breakdown_conf.append("presión cerca de soporte")
    if volume_acceleration >= 1.25:
        long_breakout += 5
        short_breakdown += 5
    if relative_volume >= 1.5:
        long_breakout += 5
        short_breakdown += 5

    # Exhaustion / reversal scores. Aggressive flow without price progress is
    # interpreted as possible absorption only when it happens at a swept level.
    bounce = 0.0
    bounce_conf: list[str] = []
    bounce_conflicts: list[str] = []
    rejection = 0.0
    rejection_conf: list[str] = []
    rejection_conflicts: list[str] = []

    if sweep_low:
        bounce += 24
        bounce_conf.append("barrido del mínimo y recuperación")
    if lower_rejection or prev_wick["lower"] > max(prev_wick["body"] * 1.6, atr_abs * 0.12):
        bounce += 12
        bounce_conf.append("mecha de rechazo inferior")
    sell_absorbed = (futures_delta <= -0.10 or taker <= 0.90) and change_5m > -0.12
    if sell_absorbed:
        bounce += 18
        bounce_conf.append("ventas agresivas sin continuación bajista")
    if spot_delta >= 0.03:
        bounce += 12
        bounce_conf.append("spot empieza a comprar")
    if book >= 0.04:
        bounce += 8
        bounce_conf.append("bids defendiendo")
    if prior_low <= current <= prior_low + atr_abs * 1.3:
        bounce += 8
    if change_15m < -2.5 and not sweep_low:
        bounce_conflicts.append("caída extendida sin reclaim confirmado")

    if sweep_high:
        rejection += 24
        rejection_conf.append("barrido del máximo y pérdida del nivel")
    if upper_rejection or prev_wick["upper"] > max(prev_wick["body"] * 1.6, atr_abs * 0.12):
        rejection += 12
        rejection_conf.append("mecha de rechazo superior")
    buy_absorbed = (futures_delta >= 0.10 or taker >= 1.10) and change_5m < 0.12
    if buy_absorbed:
        rejection += 18
        rejection_conf.append("compras agresivas sin continuación alcista")
    if spot_delta <= -0.03:
        rejection += 12
        rejection_conf.append("spot empieza a vender")
    if book <= -0.04:
        rejection += 8
        rejection_conf.append("asks defendiendo")
    if prior_high - atr_abs * 1.3 <= current <= prior_high:
        rejection += 8
    if change_15m > 2.5 and not sweep_high:
        rejection_conflicts.append("subida extendida sin rechazo confirmado")

    candidates = [
        (long_breakout, "IMPULSO_LONG", "LONG", long_breakout_conf, long_conflicts),
        (short_breakdown, "IMPULSO_SHORT", "SHORT", short_breakdown_conf, short_conflicts),
        (bounce, "REBOTE_LONG", "LONG", bounce_conf, bounce_conflicts),
        (rejection, "RECHAZO_SHORT", "SHORT", rejection_conf, rejection_conflicts),
    ]
    candidates.sort(key=lambda x: x[0], reverse=True)
    raw_score, kind, direction, confirmations, conflicts = candidates[0]
    pre_score = _clamp(raw_score)

    if kind == "IMPULSO_LONG":
        trigger = prior_high * 1.0003
        structural_invalidation = min(lows[-8:])
    elif kind == "IMPULSO_SHORT":
        trigger = prior_low * 0.9997
        structural_invalidation = max(highs[-8:])
    elif kind == "REBOTE_LONG":
        trigger = max(last_close, highs[-2])
        structural_invalidation = min(last_low, prior_low)
    else:
        trigger = min(last_close, lows[-2])
        structural_invalidation = max(last_high, prior_high)

    buffer = atr_abs * 0.30
    if direction == "LONG":
        invalidation = structural_invalidation - buffer
        stop = invalidation - atr_abs * 0.08
        entry_low = trigger
        entry_high = trigger + atr_abs * 0.22
        trigger_hit = current >= trigger
        chase_distance_atr = max(0.0, (current - trigger) / atr_abs) if atr_abs else 0.0
    else:
        invalidation = structural_invalidation + buffer
        stop = invalidation + atr_abs * 0.08
        entry_low = trigger - atr_abs * 0.22
        entry_high = trigger
        trigger_hit = current <= trigger
        chase_distance_atr = max(0.0, (trigger - current) / atr_abs) if atr_abs else 0.0

    risk_per_unit = max(abs(trigger - stop), current * 0.001)
    # Big continuation candidates are allowed more room for runners; reversal
    # setups use slightly closer objectives until their continuation is proven.
    strong_sequence = (
        pre_score >= 80
        and volume_acceleration >= 1.20
        and relative_volume >= 1.20
        and len(confirmations) >= 5
        and len(conflicts) <= 1
    )
    if kind.startswith("IMPULSO") and strong_sequence:
        magnitude = "EXPLOSIVO"
        r_targets = (1.5, 2.5, 4.0)
        duration_min, duration_max = (30, 360)
        time_stop = 45
    elif kind.startswith("IMPULSO"):
        magnitude = "NORMAL"
        r_targets = (1.25, 2.0, 3.0)
        duration_min, duration_max = (30, 240)
        time_stop = 40
    elif kind == "REBOTE_LONG":
        magnitude = "REBOTE"
        r_targets = (1.2, 2.0, 3.0)
        duration_min, duration_max = (20, 240)
        time_stop = 35
    else:
        magnitude = "RECHAZO"
        r_targets = (1.2, 2.0, 3.0)
        duration_min, duration_max = (20, 240)
        time_stop = 35

    # If 15m and 1h agree, allow the runner more time. This is still a maximum,
    # not an instruction to sit through invalidation.
    trend_15m = str(metrics.get("trend_15m", "NEUTRAL"))
    trend_1h = str(metrics.get("trend_1h", "NEUTRAL"))
    aligned_mtf = (direction == "LONG" and trend_15m == "BULLISH" and trend_1h == "BULLISH") or (
        direction == "SHORT" and trend_15m == "BEARISH" and trend_1h == "BEARISH"
    )
    if aligned_mtf:
        duration_max = min(720, duration_max * 2)
        confirmations.append("15m y 1h alineados")

    if direction == "LONG":
        tp1 = trigger + risk_per_unit * r_targets[0]
        tp2 = trigger + risk_per_unit * r_targets[1]
        tp3 = trigger + risk_per_unit * r_targets[2]
    else:
        tp1 = trigger - risk_per_unit * r_targets[0]
        tp2 = trigger - risk_per_unit * r_targets[1]
        tp3 = trigger - risk_per_unit * r_targets[2]

    # Do not call a late move a pre-activation. If it has already travelled more
    # than ~0.8 ATR beyond the trigger, the correct action is to wait for retest.
    chase_risk = trigger_hit and chase_distance_atr > 0.80
    if pre_score < 55:
        phase = "SIN_SETUP"
    elif pre_score < 70:
        phase = "VIGILAR"
    elif not trigger_hit:
        phase = "PREACTIVACION"
    elif chase_risk:
        phase = "ESPERAR_RETEST"
    elif pre_score >= 78 and len(conflicts) <= 1:
        phase = "ACTIVADO"
    else:
        phase = "VIGILAR_CONFIRMACION"

    if kind == "IMPULSO_LONG":
        title = f"LONG {magnitude} EN PREPARACIÓN"
    elif kind == "IMPULSO_SHORT":
        title = f"SHORT {magnitude} EN PREPARACIÓN"
    elif kind == "REBOTE_LONG":
        title = "POSIBLE REBOTE LONG"
    else:
        title = "POSIBLE RECHAZO / SHORT"

    # A critical conflict keeps the prediction informational even if the raw
    # preparation score is high.
    critical_conflict = any(
        text in " ".join(conflicts).lower()
        for text in ["spot vendedor", "spot comprador", "btc contrario"]
    ) and len(conflicts) >= 2
    if critical_conflict and phase in {"ACTIVADO", "PREACTIVACION"}:
        phase = "VIGILAR_CONFLICTOS"

    return {
        "type": kind,
        "magnitude": magnitude,
        "direction": direction,
        "phase": phase,
        "title": title,
        "preactivation_score": round(pre_score, 1),
        "score_is_probability": False,
        "trigger_price": round(trigger, 12),
        "trigger_hit": trigger_hit,
        "entry_low": round(entry_low, 12),
        "entry_high": round(entry_high, 12),
        "invalidation_price": round(invalidation, 12),
        "stop_loss": round(stop, 12),
        "tp1": round(tp1, 12),
        "tp2": round(tp2, 12),
        "tp3": round(tp3, 12),
        "expected_duration_min_minutes": duration_min,
        "expected_duration_max_minutes": duration_max,
        "time_stop_minutes": time_stop,
        "management": {
            "before_trigger": "No entrar. Esperar activación o reclaim/retest según el patrón.",
            "after_trigger": "Entrar solo si el precio sigue dentro de la zona planificada; no perseguir.",
            "tp1": "Al llegar a TP1, proteger: mover stop a break-even solo si la estructura sigue válida.",
            "tp2": "Tomar beneficio principal o cerrar según política paper mientras validamos la estrategia.",
            "tp3": "Runner opcional; usar trailing por estructura, nunca ampliar el stop.",
            "time_stop": f"Si en ~{time_stop} min no hay seguimiento y no alcanzó al menos 0.5R, reevaluar/salir.",
        },
        "confirmations": confirmations[:12],
        "conflicts": conflicts[:10],
        "sequence": {
            "compressed": compressed,
            "higher_lows": higher_lows,
            "lower_highs": lower_highs,
            "sweep_low": sweep_low,
            "sweep_high": sweep_high,
            "sell_absorption_rebound": sell_absorbed,
            "buy_absorption_rejection": buy_absorbed,
            "relative_volume": round(relative_volume, 3),
            "volume_acceleration": round(volume_acceleration, 3),
            "distance_to_high_atr": round(dist_high_atr, 3),
            "distance_to_low_atr": round(dist_low_atr, 3),
            "chase_distance_atr": round(chase_distance_atr, 3),
            "chase_risk": chase_risk,
            "range_high_48": round(high_48, 12),
            "range_low_48": round(low_48, 12),
        },
        "message": (
            "Predicción de fase previa basada en secuencia de estructura, volatilidad, volumen, flujo, OI y liquidez. "
            "No garantiza que aparezca una vela grande."
        ),
    }
