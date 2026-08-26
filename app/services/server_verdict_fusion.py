from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _candle(row: list[Any]) -> dict[str, float] | None:
    if not isinstance(row, list) or len(row) < 6:
        return None
    try:
        return {
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
    except (TypeError, ValueError):
        return None


def _candles(rows: Any) -> list[dict[str, float]]:
    output: list[dict[str, float]] = []
    for row in rows or []:
        parsed = _candle(row)
        if parsed:
            output.append(parsed)
    return output


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    current = values[0]
    for value in values[1:]:
        current = value * alpha + current * (1.0 - alpha)
    return current


def _atr(candles: list[dict[str, float]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    rows = candles[-(period + 1):]
    values: list[float] = []
    for index in range(1, len(rows)):
        current = rows[index]
        previous = rows[index - 1]
        values.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))
    return _avg(values)


def _relative_volume(candles: list[dict[str, float]]) -> float:
    if len(candles) < 14:
        return 1.0
    prior = _avg([x["volume"] for x in candles[-12:-6]]) or 1.0
    recent = _avg([x["volume"] for x in candles[-6:]])
    return recent / prior


def _body_efficiency(candles: list[dict[str, float]], direction: str) -> float:
    rows = candles[-5:]
    values: list[float] = []
    for candle in rows:
        range_ = max(candle["high"] - candle["low"], 1e-12)
        body = candle["close"] - candle["open"] if direction == "LONG" else candle["open"] - candle["close"]
        values.append(body / range_)
    return _avg(values)


def _opposing_wick(candles: list[dict[str, float]], direction: str) -> float:
    rows = candles[-4:]
    values: list[float] = []
    for candle in rows:
        range_ = max(candle["high"] - candle["low"], 1e-12)
        wick = (
            candle["high"] - max(candle["open"], candle["close"])
            if direction == "LONG"
            else min(candle["open"], candle["close"]) - candle["low"]
        )
        values.append(_clamp(wick / range_, 0.0, 1.0))
    return _avg(values)


def _trend_aligned(candles: list[dict[str, float]], direction: str) -> bool:
    if len(candles) < 25:
        return False
    closes = [x["close"] for x in candles]
    price = closes[-1]
    e9 = _ema(closes, 9)
    e21 = _ema(closes, 21)
    return price > e9 > e21 if direction == "LONG" else price < e9 < e21


def _breakout_trap_risk(candles: list[dict[str, float]], direction: str) -> float:
    if len(candles) < 30:
        return 30.0
    base = candles[-27:-3]
    recent = candles[-3:]
    high = max(x["high"] for x in base)
    low = min(x["low"] for x in base)
    unit = _atr(candles, 14) or max(recent[-1]["close"] * 0.001, 1e-12)
    latest = recent[-1]
    broke_up = any(x["high"] > high + unit * 0.08 for x in recent)
    broke_down = any(x["low"] < low - unit * 0.08 for x in recent)
    accepted_up = all(x["close"] > high for x in recent[-2:])
    accepted_down = all(x["close"] < low for x in recent[-2:])
    back_inside_up = broke_up and latest["close"] < high
    back_inside_down = broke_down and latest["close"] > low
    risk = 15.0
    if direction == "LONG":
        if back_inside_up:
            risk += 40
        if broke_up and not accepted_up:
            risk += 15
        if broke_down and latest["close"] > low:
            risk -= 8
    else:
        if back_inside_down:
            risk += 40
        if broke_down and not accepted_down:
            risk += 15
        if broke_up and latest["close"] < high:
            risk -= 8
    return _clamp(risk)


def _acceleration_burst_score(candles: list[dict[str, float]], direction: str, flow_strength: float) -> float:
    if len(candles) < 18:
        return 0.0
    side = 1.0 if direction == "LONG" else -1.0
    unit = _atr(candles, 14) or max(candles[-1]["close"] * 0.001, 1e-12)
    recent = candles[-4:]
    prior = candles[-8:-4]
    recent_move = (recent[-1]["close"] - recent[0]["open"]) * side / unit if len(recent) > 1 else 0.0
    prior_move = (prior[-1]["close"] - prior[0]["open"]) * side / unit if len(prior) > 1 else 0.0
    acceleration = recent_move - prior_move
    recent_body = _body_efficiency(recent, direction)
    prior_body = _body_efficiency(prior, direction)
    recent_volume = _avg([x["volume"] for x in recent])
    prior_volume = _avg([x["volume"] for x in prior]) or 1.0
    volume_expansion = recent_volume / prior_volume
    score = 0.0
    score += 22 if recent_move >= 0.35 else 12 if recent_move >= 0.18 else 0
    score += 22 if acceleration >= 0.20 else 12 if acceleration >= 0.08 else 0
    score += 20 if volume_expansion >= 1.35 else 10 if volume_expansion >= 1.10 else 0
    score += 16 if recent_body >= 0.28 and recent_body > prior_body + 0.08 else 8 if recent_body >= 0.18 else 0
    score += 14 if flow_strength >= 65 else 8 if flow_strength >= 55 else 0
    if _opposing_wick(recent, direction) <= 0.22:
        score += 6
    return _clamp(score)


def build_server_verdict_fusion(scored: dict[str, Any], snapshot: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    """Server port of web/lib/verdictFusion.ts thresholds and candle math.

    The frontend has fields called ready_checks/data_quality. On the backend those
    same safety facts come from decision_guard/sequence plus actual candle presence.
    No missing value is promoted to a pass.
    """
    m1 = _candles(snapshot.get("klines_1m"))
    m5 = _candles(snapshot.get("klines"))
    m15 = _candles(snapshot.get("klines_15m"))
    direction = str(prediction.get("direction") or scored.get("direction") or "LONG").upper()
    if direction not in {"LONG", "SHORT"}:
        direction = "LONG"
    side = 1.0 if direction == "LONG" else -1.0
    phase = str(prediction.get("phase") or "SIN_SETUP")
    price = _f(scored.get("current_price"), m1[-1]["close"] if m1 else 0.0)
    entry_low = min(_f(scored.get("entry_low")), _f(scored.get("entry_high")))
    entry_high = max(_f(scored.get("entry_low")), _f(scored.get("entry_high")))
    stop = _f(scored.get("stop_loss"))
    tp1 = _f(scored.get("tp1"))
    invalidation = _f(prediction.get("invalidation_price"), stop)

    decision_guard = dict(prediction.get("decision_guard") or {})
    sequence = dict(prediction.get("sequence") or {})
    risk_guard_pass = bool(decision_guard.get("risk_guard_pass", sequence.get("risk_guard_pass", False)))
    direction_match = bool(decision_guard.get("direction_match", prediction.get("direction") == scored.get("direction")))
    chase = bool(sequence.get("chase_risk"))
    data_limited = len(m1) < 30 or len(m5) < 25 or len(m15) < 25
    invalidated = price <= invalidation if direction == "LONG" else price >= invalidation
    in_zone = entry_low > 0 and entry_high > 0 and entry_low <= price <= entry_high
    atr1 = _atr(m1, 14) or max(price * 0.001, 1e-12)
    distance_to_zone = entry_low - price if price < entry_low else price - entry_high if price > entry_high else 0.0
    near_zone = distance_to_zone <= atr1 * 0.35

    setup = _f(scored.get("setup_score"))
    prep = _f(prediction.get("preactivation_score"))
    edge = abs(_f(scored.get("long_score")) - _f(scored.get("short_score")))
    core = scored.get("state") == "READY" and phase == "ACTIVADO" and risk_guard_pass and direction_match and not data_limited and not invalidated

    mtf_strength = 0.0
    if _trend_aligned(m5, direction):
        mtf_strength += 45
    if _trend_aligned(m15, direction):
        mtf_strength += 35
    mtf_strength += 20 if edge >= 12 else 12 if edge >= 6 else 0
    mtf_strength = _clamp(mtf_strength)
    mtf = mtf_strength >= 55

    metrics = dict(scored.get("metrics") or {})
    spot = _f(metrics.get("spot_delta_ratio"), _f(sequence.get("spot_delta_ratio")))
    futures = _f(metrics.get("futures_delta_ratio"), _f(sequence.get("futures_delta_ratio")))
    oi = _f(metrics.get("oi_change_pct"), _f(sequence.get("oi_change_pct")))
    coinglass = dict(scored.get("coinglass") or {})
    taker = dict(coinglass.get("taker") or {})
    taker_available = bool(taker.get("available"))
    taker_ratio = _f(taker.get("buy_sell_ratio"), 1.0)
    spot_available = abs(spot) > 1e-9
    futures_available = abs(futures) > 1e-9
    flow_strength = 50.0
    if spot_available:
        flow_strength += 18 if spot * side > 0.03 else -22 if spot * side < -0.03 else 0
    if futures_available:
        flow_strength += 16 if futures * side > 0.03 else -20 if futures * side < -0.03 else 0
    if oi < -0.35 and ((spot_available and spot * side <= 0) or (futures_available and futures * side <= 0)):
        flow_strength -= 10
    if taker_available:
        aligned = taker_ratio >= 1.02 if direction == "LONG" else taker_ratio <= 0.98
        flow_strength += 10 if aligned else -10
    flow_strength = _clamp(flow_strength)
    flow = flow_strength >= 48

    trap_risk = _breakout_trap_risk(m1, direction)
    wick = _opposing_wick(m1, direction)
    rvol = _relative_volume(m1)
    if wick >= 0.35:
        trap_risk += 12
    if rvol < 0.72:
        trap_risk += 8
    if flow_strength < 38:
        trap_risk += 15
    trap_risk = _clamp(trap_risk)
    trap = trap_risk < 60

    recent = m1[-6:]
    prior = m1[-12:-6]
    move_recent = (recent[-1]["close"] - recent[0]["open"]) * side / atr1 if len(recent) > 1 else 0.0
    move_prior = (prior[-1]["close"] - prior[0]["open"]) * side / atr1 if len(prior) > 1 else 0.0
    velocity_ratio = move_recent / abs(move_prior) if abs(move_prior) > 0.10 else 1.0
    body = _body_efficiency(m1, direction)
    closes5 = [x["close"] for x in m5]
    ema21_5 = _ema(closes5, 21)
    atr5 = _atr(m5, 14) or atr1 * 2
    extension_atr = abs(price - ema21_5) / atr5 if atr5 > 0 else 0.0
    decay_risk = 12.0
    if velocity_ratio < 0.60:
        decay_risk += 22
    if rvol < 0.78:
        decay_risk += 14
    if body < 0.08:
        decay_risk += 12
    if wick >= 0.35:
        decay_risk += 14
    if extension_atr >= 1.35:
        decay_risk += 18
    if chase:
        decay_risk += 20
    decay_risk = _clamp(decay_risk)

    acceleration_score = _acceleration_burst_score(m1, direction, flow_strength)
    burst_detected = acceleration_score >= 72 and trap_risk <= 45 and decay_risk <= 55
    if burst_detected:
        decay_risk = _clamp(decay_risk - 8)
    momentum = decay_risk < 65

    midpoint = (entry_low + entry_high) / 2.0
    risk_unit = abs(midpoint - stop)
    rr1 = abs(tp1 - midpoint) / risk_unit if risk_unit > 0 else 0.0
    entry_quality = 78.0 if in_zone else 58.0 if near_zone else 28.0
    if not chase:
        entry_quality += 10
    if rr1 >= 1.15:
        entry_quality += 8
    elif rr1 < 0.9:
        entry_quality -= 12
    if trap_risk >= 60:
        entry_quality -= 12
    if decay_risk >= 65:
        entry_quality -= 12
    if burst_detected and (in_zone or near_zone):
        entry_quality += 6
    entry_quality = _clamp(entry_quality)
    entry = in_zone and not chase and entry_quality >= 72

    locks = {"core": core, "mtf": mtf, "flow": flow, "trap": trap, "momentum": momentum, "entry": entry}
    pass_count = sum(1 for value in locks.values() if value)
    hard_block = False
    hard_block_reason = None
    if invalidated:
        hard_block, hard_block_reason = True, "invalidation_crossed"
    elif not risk_guard_pass:
        hard_block, hard_block_reason = True, "risk_guard"
    elif not direction_match:
        hard_block, hard_block_reason = True, "direction_conflict"
    elif data_limited:
        hard_block, hard_block_reason = True, "data_limited"

    technical_confidence = _clamp(
        setup * 0.20 + prep * 0.11 + mtf_strength * 0.17 + flow_strength * 0.13
        + (100 - trap_risk) * 0.13 + (100 - decay_risk) * 0.09
        + entry_quality * 0.10 + acceleration_score * 0.07
    )
    candidate_enter = not hard_block and core and entry and trap and momentum and pass_count >= 5
    fast_track = candidate_enter and pass_count == 6 and technical_confidence >= 84 and trap_risk <= 38 and decay_risk <= 46 and (burst_detected or acceleration_score >= 58)

    return {
        "version": "server_parity_v1",
        "direction": direction,
        "locks": locks,
        "pass_count": pass_count,
        "hard_block": hard_block,
        "hard_block_reason": hard_block_reason,
        "candidate_enter": candidate_enter,
        "fast_track": fast_track,
        "trap_risk": round(trap_risk, 2),
        "decay_risk": round(decay_risk, 2),
        "acceleration_score": round(acceleration_score, 2),
        "burst_detected": burst_detected,
        "mtf_strength": round(mtf_strength, 2),
        "flow_strength": round(flow_strength, 2),
        "entry_quality": round(entry_quality, 2),
        "technical_confidence": round(technical_confidence, 2),
        "in_zone": in_zone,
        "near_zone": near_zone,
        "chase": chase,
        "invalidated": invalidated,
        "price": price,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "tp1": tp1,
        "rr1": round(rr1, 4),
        "data_limited": data_limited,
        "candle_counts": {"1m": len(m1), "5m": len(m5), "15m": len(m15)},
        "probability_note": "technical_confidence is a weighted technical score, not next-trade probability.",
    }
