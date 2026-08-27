from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out: list[float] = [values[0]]
    current = values[0]
    for value in values[1:]:
        current = value * alpha + current * (1.0 - alpha)
        out.append(current)
    return out


def _atr(rows: list[dict[str, float]], period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    trs: list[float] = []
    start = max(1, len(rows) - period)
    for index in range(start, len(rows)):
        row = rows[index]
        prev = rows[index - 1]
        tr = max(
            row["high"] - row["low"],
            abs(row["high"] - prev["close"]),
            abs(row["low"] - prev["close"]),
        )
        trs.append(max(0.0, tr))
    return sum(trs) / len(trs) if trs else 0.0


def _rows(raw: Any) -> list[dict[str, float]]:
    output: list[dict[str, float]] = []
    if not isinstance(raw, list):
        return output
    for item in raw:
        try:
            if isinstance(item, dict):
                output.append({
                    "open": _f(item.get("open")),
                    "high": _f(item.get("high")),
                    "low": _f(item.get("low")),
                    "close": _f(item.get("close")),
                    "volume": _f(item.get("volume") or item.get("quote_volume")),
                })
            elif isinstance(item, (list, tuple)) and len(item) >= 6:
                output.append({
                    "open": _f(item[1]),
                    "high": _f(item[2]),
                    "low": _f(item[3]),
                    "close": _f(item[4]),
                    "volume": _f(item[5]),
                })
        except Exception:
            continue
    return [row for row in output if row["close"] > 0 and row["high"] > 0 and row["low"] > 0]


def _return_pct(values: list[float], bars: int) -> float:
    if len(values) <= bars or values[-bars - 1] <= 0:
        return 0.0
    return (values[-1] - values[-bars - 1]) / values[-bars - 1] * 100.0


def _score_margin(scores: dict[str, float]) -> tuple[str, float, str, float]:
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    primary_name, primary_score = ordered[0]
    secondary_name, secondary_score = ordered[1] if len(ordered) > 1 else (primary_name, primary_score)
    return primary_name, primary_score, secondary_name, secondary_score


def build_forced_path_forecast(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    prediction: dict[str, Any],
) -> dict[str, Any]:
    """Always-on technical path hypothesis.

    This intentionally ALWAYS returns a primary path when enough price exists.
    It is a relative scenario-ranking engine, not a calibrated probability model
    and not a trade authorization layer.
    """

    rows = _rows(snapshot.get("klines") or snapshot.get("klines_5m") or [])
    closes = [row["close"] for row in rows]
    price = closes[-1] if closes else _f(snapshot.get("price") or scored.get("current_price") or prediction.get("current_price"))

    fusion = dict(prediction.get("verdict_fusion") or {})
    zone = dict(prediction.get("entry_zone_engine") or {})
    direction = str(prediction.get("direction") or scored.get("direction") or "LONG").upper()
    direction_long = direction != "SHORT"

    if price <= 0:
        return {
            "version": "forced_path_v1",
            "available": False,
            "always_ranks_path": True,
            "primary_path": "NO_PRICE",
            "label": "SIN PRECIO",
            "score_is_probability": False,
            "note": "No hay precio suficiente para construir una hipótesis de recorrido.",
        }

    ema9 = _ema(closes, 9) if closes else []
    ema21 = _ema(closes, 21) if closes else []
    ema9_now = ema9[-1] if ema9 else price
    ema21_now = ema21[-1] if ema21 else price
    ema21_prev = ema21[-4] if len(ema21) >= 4 else ema21_now
    atr = _atr(rows)
    atr_pct = (atr / price * 100.0) if atr > 0 and price > 0 else 0.35
    atr_safe = max(atr, price * 0.0025)

    r3 = _return_pct(closes, 3)
    r6 = _return_pct(closes, 6)
    r12 = _return_pct(closes, 12)
    ema_gap_pct = (ema9_now - ema21_now) / price * 100.0
    ema21_slope_pct = (ema21_now - ema21_prev) / price * 100.0
    distance_ema21_atr = (price - ema21_now) / atr_safe

    recent = rows[-12:] if rows else []
    support = min((row["low"] for row in recent), default=ema21_now)
    resistance = max((row["high"] for row in recent), default=ema21_now)
    latest = rows[-1] if rows else {"open": price, "high": price, "low": price, "close": price, "volume": 0.0}
    body = abs(latest["close"] - latest["open"])
    candle_range = max(latest["high"] - latest["low"], 1e-12)
    lower_wick = min(latest["open"], latest["close"]) - latest["low"]
    upper_wick = latest["high"] - max(latest["open"], latest["close"])
    bullish_rejection = lower_wick / candle_range >= 0.34 and latest["close"] >= latest["open"] - body * 0.25
    bearish_rejection = upper_wick / candle_range >= 0.34 and latest["close"] <= latest["open"] + body * 0.25

    vols = [row["volume"] for row in rows[-21:-1] if row["volume"] > 0]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    volume_ratio = latest["volume"] / avg_vol if avg_vol > 0 else 1.0

    mtf = _f(fusion.get("mtf_strength"), 50.0)
    flow = _f(fusion.get("flow_strength"), 50.0)
    trap_risk = _f(fusion.get("trap_risk"), 50.0)
    decay_risk = _f(fusion.get("decay_risk"), 50.0)
    acceleration = _f(fusion.get("acceleration_score"), 50.0)
    technical = _f(fusion.get("technical_confidence"), _f(prediction.get("preactivation_score"), 50.0))
    trap_safety = 100.0 - trap_risk
    momentum_quality = 100.0 - decay_risk

    trend_long = (
        (ema9_now >= ema21_now)
        + (ema21_slope_pct >= 0)
        + (r12 >= 0)
        + (mtf >= 50)
    )
    trend_short = 4 - trend_long
    broader_long = trend_long >= 3 or (direction_long and mtf >= 52)
    broader_short = trend_short >= 3 or ((not direction_long) and mtf >= 52)

    # Scores are intentionally relative technical utilities, not probabilities.
    up_direct = 50.0
    up_direct += _clamp(ema_gap_pct / max(atr_pct, 0.05) * 12.0, -18, 18)
    up_direct += _clamp(ema21_slope_pct / max(atr_pct, 0.05) * 14.0, -12, 12)
    up_direct += _clamp(r3 / max(atr_pct, 0.05) * 4.0, -14, 14)
    up_direct += (flow - 50.0) * 0.18 + (acceleration - 50.0) * 0.12 + (mtf - 50.0) * 0.12
    up_direct += (trap_safety - 50.0) * 0.07 + (momentum_quality - 50.0) * 0.07
    up_direct += 5.0 if volume_ratio >= 1.15 and r3 > 0 else 0.0
    up_direct -= 7.0 if distance_ema21_atr > 1.8 else 0.0

    down_direct = 100.0 - up_direct
    # Do not make the mirror exact; flow/trap can make both direct paths mediocre.
    down_direct += _clamp(-r6 / max(atr_pct, 0.05) * 2.0, -6, 6)

    down_then_up = 42.0
    down_then_up += 17.0 if broader_long else -12.0
    down_then_up += 10.0 if r3 < 0 else 4.0 if r3 < r6 else -5.0
    down_then_up += 9.0 if distance_ema21_atr > 0.35 else 5.0 if distance_ema21_atr > -0.35 else -8.0
    down_then_up += 7.0 if price > support else -12.0
    down_then_up += 8.0 if bullish_rejection else 0.0
    down_then_up += (flow - 50.0) * 0.10 + (mtf - 50.0) * 0.12 + (trap_safety - 50.0) * 0.08
    down_then_up += 4.0 if momentum_quality >= 45 else -7.0

    up_then_down = 42.0
    up_then_down += 17.0 if broader_short else -12.0
    up_then_down += 10.0 if r3 > 0 else 4.0 if r3 > r6 else -5.0
    up_then_down += 9.0 if distance_ema21_atr < -0.35 else 5.0 if distance_ema21_atr < 0.35 else -8.0
    up_then_down += 7.0 if price < resistance else -12.0
    up_then_down += 8.0 if bearish_rejection else 0.0
    up_then_down += ((100.0 - flow) - 50.0) * 0.10 + ((100.0 - mtf) - 50.0) * 0.12 + (trap_safety - 50.0) * 0.08
    up_then_down += 4.0 if momentum_quality >= 45 else -7.0

    range_up = 36.0
    range_down = 36.0
    compression = abs(ema_gap_pct) <= max(atr_pct * 0.22, 0.03) and abs(r6) <= max(atr_pct * 1.4, 0.2)
    if compression:
        range_up += 14.0
        range_down += 14.0
    range_up += (flow - 50.0) * 0.12 + (mtf - 50.0) * 0.08 + _clamp(r3 * 5.0, -8, 8)
    range_down += ((100.0 - flow) - 50.0) * 0.12 + ((100.0 - mtf) - 50.0) * 0.08 + _clamp(-r3 * 5.0, -8, 8)

    scores = {
        "UP_DIRECT": _clamp(up_direct),
        "DOWN_THEN_UP": _clamp(down_then_up),
        "DOWN_DIRECT": _clamp(down_direct),
        "UP_THEN_DOWN": _clamp(up_then_down),
        "RANGE_BREAK_UP": _clamp(range_up),
        "RANGE_BREAK_DOWN": _clamp(range_down),
    }
    primary, primary_score, secondary, secondary_score = _score_margin(scores)
    edge_gap = max(0.0, primary_score - secondary_score)

    labels = {
        "UP_DIRECT": "SUBE PRIMERO",
        "DOWN_THEN_UP": "RETROCEDE → REBOTA → SUBE",
        "DOWN_DIRECT": "BAJA PRIMERO",
        "UP_THEN_DOWN": "SUBE → RECHAZA → BAJA",
        "RANGE_BREAK_UP": "LATERAL → ROMPE ARRIBA",
        "RANGE_BREAK_DOWN": "LATERAL → ROMPE ABAJO",
    }
    first_move = "UP" if primary in {"UP_DIRECT", "UP_THEN_DOWN", "RANGE_BREAK_UP"} else "DOWN"
    final_bias = "LONG" if primary in {"UP_DIRECT", "DOWN_THEN_UP", "RANGE_BREAK_UP"} else "SHORT"
    contains_pullback = primary in {"DOWN_THEN_UP", "UP_THEN_DOWN"}

    if edge_gap >= 18 and primary_score >= 72:
        clarity = "CLEAR"
    elif edge_gap >= 9 and primary_score >= 62:
        clarity = "USABLE"
    else:
        clarity = "TIGHT_RACE"

    entry_low = _f(prediction.get("entry_low") or zone.get("entry_low"))
    entry_high = _f(prediction.get("entry_high") or zone.get("entry_high"))
    zone_min = min(entry_low, entry_high) if entry_low > 0 and entry_high > 0 else 0.0
    zone_max = max(entry_low, entry_high) if entry_low > 0 and entry_high > 0 else 0.0

    if primary == "DOWN_THEN_UP":
        pullback_low = max(support, min(ema21_now, price) - atr_safe * 0.20)
        pullback_high = min(price, max(ema21_now, support) + atr_safe * 0.35)
        if zone_min > 0:
            pullback_low = max(min(pullback_low, zone_max), min(zone_min, price))
            pullback_high = max(pullback_low, min(max(pullback_high, zone_min), price))
    elif primary == "UP_THEN_DOWN":
        pullback_low = max(price, min(ema21_now, resistance) - atr_safe * 0.35)
        pullback_high = min(resistance, max(ema21_now, price) + atr_safe * 0.20)
    else:
        pullback_low = 0.0
        pullback_high = 0.0

    if contains_pullback:
        trade_posture = "WAIT_PULLBACK_CONFIRMATION"
    elif final_bias == direction and clarity in {"CLEAR", "USABLE"} and technical >= 60:
        trade_posture = "FOLLOW_BIAS_IF_ENTRY_ZONE"
    elif final_bias != direction:
        trade_posture = "CONFLICT_WITH_CURRENT_PLAN"
    else:
        trade_posture = "OBSERVE"

    reasons: list[str] = []
    if broader_long:
        reasons.append("La estructura de fondo favorece LONG.")
    elif broader_short:
        reasons.append("La estructura de fondo favorece SHORT.")
    if r3 < 0 and broader_long:
        reasons.append("Hay retroceso corto dentro de un fondo alcista.")
    if r3 > 0 and broader_short:
        reasons.append("Hay rebote corto dentro de un fondo bajista.")
    if bullish_rejection:
        reasons.append("La última vela muestra rechazo comprador.")
    if bearish_rejection:
        reasons.append("La última vela muestra rechazo vendedor.")
    if flow >= 60:
        reasons.append(f"Flow favorable {flow:.0f}/100.")
    if trap_risk >= 65:
        reasons.append(f"Advertencia: trap risk {trap_risk:.0f}/100.")
    if distance_ema21_atr > 1.8:
        reasons.append("Precio extendido sobre EMA21: aumenta riesgo de retroceso antes de continuar.")
    elif distance_ema21_atr < -1.8:
        reasons.append("Precio extendido bajo EMA21: aumenta riesgo de rebote antes de continuar.")

    return {
        "version": "forced_path_v1",
        "available": True,
        "always_ranks_path": True,
        "primary_path": primary,
        "label": labels[primary],
        "primary_score": round(primary_score, 1),
        "score_is_probability": False,
        "secondary_path": secondary,
        "secondary_label": labels[secondary],
        "secondary_score": round(secondary_score, 1),
        "edge_gap": round(edge_gap, 1),
        "clarity": clarity,
        "first_move": first_move,
        "final_bias": final_bias,
        "contains_pullback": contains_pullback,
        "trade_posture": trade_posture,
        "pullback_zone_low": round(pullback_low, 10) if pullback_low > 0 else None,
        "pullback_zone_high": round(pullback_high, 10) if pullback_high > 0 else None,
        "technical_context": {
            "price": round(price, 10),
            "ema9": round(ema9_now, 10),
            "ema21": round(ema21_now, 10),
            "ema_gap_pct": round(ema_gap_pct, 4),
            "ema21_slope_pct": round(ema21_slope_pct, 4),
            "atr_pct": round(atr_pct, 4),
            "distance_to_ema21_atr": round(distance_ema21_atr, 3),
            "return_3bars_pct": round(r3, 4),
            "return_6bars_pct": round(r6, 4),
            "return_12bars_pct": round(r12, 4),
            "volume_ratio": round(volume_ratio, 3),
            "support": round(support, 10),
            "resistance": round(resistance, 10),
            "flow": round(flow, 1),
            "mtf": round(mtf, 1),
            "trap_risk": round(trap_risk, 1),
            "momentum_quality": round(momentum_quality, 1),
            "acceleration": round(acceleration, 1),
        },
        "scenario_scores": {name: round(value, 1) for name, value in scores.items()},
        "reasons": reasons[:6],
        "safety": {
            "can_create_entry": False,
            "can_change_leverage": False,
            "can_override_stop": False,
            "always_predicts_but_does_not_force_trade": True,
        },
        "note": (
            "Path Forecast always ranks a market path so ExplodeX does not stay silent. "
            "Its scores compare scenarios; they are not calibrated probabilities and cannot guarantee the next move."
        ),
    }
