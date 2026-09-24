from __future__ import annotations

from statistics import mean, pstdev
from typing import Any

from app.services.price_action_pattern_vision import detect_price_action_patterns

VERSION = "professional_confluence_arsenal_v2_price_action_vision"
POLICY = {
    "indicators_are_confirmation_not_standalone_triggers": True,
    "can_create_entry_by_itself": False,
    "can_raise_leverage_by_itself": False,
    "score_is_probability": False,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _pct(a: float, b: float) -> float:
    return ((b - a) / a) * 100.0 if abs(a) > 1e-12 else 0.0


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _true_ranges(rows: list[list[Any]]) -> list[float]:
    if len(rows) < 2:
        return []
    out: list[float] = []
    prev_close = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return out


def _atr(rows: list[list[Any]], period: int = 14) -> float:
    trs = _true_ranges(rows)
    sample = trs[-period:] if trs else []
    return mean(sample) if sample else 0.0


def _rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = [max(0.0, b - a) for a, b in zip(closes, closes[1:])]
    losses = [max(0.0, a - b) for a, b in zip(closes, closes[1:])]
    avg_gain = mean(gains[:period])
    avg_loss = mean(losses[:period])

    def value(g: float, l: float) -> float:
        if l <= 1e-12:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = ((period - 1.0) * avg_gain + gains[i]) / period
        avg_loss = ((period - 1.0) * avg_loss + losses[i]) / period
        out[i + 1] = value(avg_gain, avg_loss)
    return out


def _vwap_context(rows: list[list[Any]], lookback: int = 60) -> dict[str, Any]:
    work = rows[-lookback:]
    numerator = 0.0
    denominator = 0.0
    for row in work:
        typical = (_f(row[2]) + _f(row[3]) + _f(row[4])) / 3.0
        base_volume = max(0.0, _f(row[5]))
        numerator += typical * base_volume
        denominator += base_volume
    if denominator <= 1e-12:
        return {"available": False}
    vwap = numerator / denominator
    current = _f(work[-1][4])
    atr = max(_atr(work), current * 0.0005, 1e-12)
    prev = _f(work[-2][4]) if len(work) >= 2 else current
    prev_dist = abs(prev - vwap)
    current_dist = abs(current - vwap)
    reverting = current_dist < prev_dist
    bias = "SHORT" if current > vwap else "LONG" if current < vwap else "NEUTRAL"
    return {
        "available": True,
        "method": f"ROLLING_{len(work)}_BAR_TYPICAL_PRICE_VOLUME_WEIGHTED",
        "vwap": round(vwap, 12),
        "current_vs_vwap_pct": round(_pct(vwap, current), 4),
        "distance_atr": round((current - vwap) / atr, 4),
        "mean_reversion_bias": bias if abs(current - vwap) >= atr * 1.15 and reverting else "NEUTRAL",
        "reverting_toward_vwap": reverting,
    }


def _ema_context(rows: list[list[Any]]) -> dict[str, Any]:
    closes = [_f(r[4]) for r in rows]
    current = closes[-1] if closes else 0.0
    result: dict[str, Any] = {"available": bool(closes)}
    for period in (20, 50, 200):
        if len(closes) >= period:
            result[f"ema{period}"] = round(_ema_series(closes, period)[-1], 12)
        else:
            result[f"ema{period}"] = None
    e20, e50, e200 = result.get("ema20"), result.get("ema50"), result.get("ema200")
    if e20 and e50 and e20 > e50 and (not e200 or current >= e200):
        bias = "LONG"
    elif e20 and e50 and e20 < e50 and (not e200 or current <= e200):
        bias = "SHORT"
    else:
        bias = "NEUTRAL"
    result["context_bias"] = bias
    result["ema200_available"] = e200 is not None
    return result


def _bollinger_context(rows: list[list[Any]], period: int = 20) -> dict[str, Any]:
    closes = [_f(r[4]) for r in rows]
    if len(closes) < period + 20:
        return {"available": False}

    def width_at(end: int) -> float:
        sample = closes[end - period:end]
        mid = mean(sample)
        sd = pstdev(sample)
        return ((4.0 * sd) / mid) * 100.0 if mid > 0 else 0.0

    current_width = width_at(len(closes))
    previous_widths = [width_at(i) for i in range(period, len(closes) - 1)]
    baseline = mean(previous_widths[-40:]) if previous_widths else current_width
    ratio = current_width / baseline if baseline > 1e-12 else 1.0
    compressed = ratio <= 0.72
    expanding = ratio >= 1.20
    return {
        "available": True,
        "bandwidth_pct": round(current_width, 4),
        "baseline_bandwidth_pct": round(baseline, 4),
        "width_ratio": round(ratio, 4),
        "compressed": compressed,
        "expanding": expanding,
    }


def _adx_context(rows: list[list[Any]], period: int = 14) -> dict[str, Any]:
    if len(rows) < period * 2 + 2:
        return {"available": False}
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for prev, cur in zip(rows, rows[1:]):
        ph, pl, pc = _f(prev[2]), _f(prev[3]), _f(prev[4])
        ch, cl = _f(cur[2]), _f(cur[3])
        up = ch - ph
        down = pl - cl
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(ch - cl, abs(ch - pc), abs(cl - pc)))

    dx_values: list[float] = []
    for i in range(period, len(trs) + 1):
        tr = sum(trs[i - period:i])
        if tr <= 1e-12:
            continue
        pdi = 100.0 * sum(plus_dm[i - period:i]) / tr
        mdi = 100.0 * sum(minus_dm[i - period:i]) / tr
        denom = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / denom if denom > 1e-12 else 0.0
        dx_values.append(dx)
    adx = mean(dx_values[-period:]) if dx_values else 0.0
    last_tr = sum(trs[-period:])
    plus_di = 100.0 * sum(plus_dm[-period:]) / last_tr if last_tr > 0 else 0.0
    minus_di = 100.0 * sum(minus_dm[-period:]) / last_tr if last_tr > 0 else 0.0
    bias = "LONG" if plus_di > minus_di else "SHORT" if minus_di > plus_di else "NEUTRAL"
    strength = "STRONG" if adx >= 25 else "DEVELOPING" if adx >= 18 else "WEAK"
    return {
        "available": True,
        "adx": round(adx, 3),
        "plus_di": round(plus_di, 3),
        "minus_di": round(minus_di, 3),
        "bias": bias,
        "trend_strength": strength,
    }


def _mfi_context(rows: list[list[Any]], period: int = 14) -> dict[str, Any]:
    if len(rows) < period + 1:
        return {"available": False}
    work = rows[-(period + 1):]
    typical = [(_f(r[2]) + _f(r[3]) + _f(r[4])) / 3.0 for r in work]
    raw_money = [typical[i] * max(0.0, _f(work[i][5])) for i in range(len(work))]
    pos = 0.0
    neg = 0.0
    for i in range(1, len(work)):
        if typical[i] > typical[i - 1]:
            pos += raw_money[i]
        elif typical[i] < typical[i - 1]:
            neg += raw_money[i]
    if neg <= 1e-12:
        mfi = 100.0 if pos > 0 else 50.0
    else:
        ratio = pos / neg
        mfi = 100.0 - 100.0 / (1.0 + ratio)
    state = "OVERBOUGHT" if mfi >= 80 else "OVERSOLD" if mfi <= 20 else "NEUTRAL"
    return {"available": True, "mfi": round(mfi, 3), "state": state}


def _stoch_rsi_context(rows: list[list[Any]], period: int = 14) -> dict[str, Any]:
    closes = [_f(r[4]) for r in rows]
    rsi = [x for x in _rsi_series(closes, period) if x is not None]
    if len(rsi) < period:
        return {"available": False}
    sample = rsi[-period:]
    lo, hi = min(sample), max(sample)
    value = 50.0 if hi - lo <= 1e-12 else ((sample[-1] - lo) / (hi - lo)) * 100.0
    state = "OVERBOUGHT" if value >= 80 else "OVERSOLD" if value <= 20 else "NEUTRAL"
    return {"available": True, "stoch_rsi": round(value, 3), "state": state}


def _trade_cvd(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"available": False}
    signed: list[float] = []
    prices: list[float] = []
    ordered = sorted(
        [dict(x) for x in trades if isinstance(x, dict)],
        key=lambda x: _f(x.get("T") or x.get("time") or x.get("timestamp")),
    )
    for trade in ordered:
        price = _f(trade.get("p") or trade.get("price"))
        qty = _f(trade.get("q") or trade.get("qty"))
        notional = price * qty
        if notional <= 0:
            continue
        signed.append(-notional if bool(trade.get("m", False)) else notional)
        prices.append(price)
    if not signed:
        return {"available": False}
    total_abs = sum(abs(x) for x in signed)
    net = sum(signed)
    normalized = net / total_abs if total_abs > 1e-12 else 0.0
    midpoint = max(1, len(signed) // 2)
    first = sum(signed[:midpoint])
    second = sum(signed[midpoint:])
    acceleration = (second - first) / total_abs if total_abs > 1e-12 else 0.0
    price_change = _pct(prices[0], prices[-1]) if len(prices) >= 2 else 0.0
    divergence = "NONE"
    if price_change <= -0.10 and normalized >= 0.06:
        divergence = "BULLISH_CVD_DIVERGENCE"
    elif price_change >= 0.10 and normalized <= -0.06:
        divergence = "BEARISH_CVD_DIVERGENCE"
    return {
        "available": True,
        "net_signed_notional": round(net, 6),
        "normalized_delta": round(normalized, 5),
        "delta_acceleration": round(acceleration, 5),
        "price_change_pct": round(price_change, 4),
        "divergence": divergence,
        "proxy_note": "Aggressive-trade cumulative delta proxy over the fetched trade window, not exchange-wide session CVD.",
    }


def _basis_context(premium: dict[str, Any]) -> dict[str, Any]:
    mark = _f(premium.get("markPrice"))
    index = _f(premium.get("indexPrice"))
    if mark <= 0 or index <= 0:
        return {"available": False}
    basis_pct = _pct(index, mark)
    state = "POSITIVE" if basis_pct > 0.02 else "NEGATIVE" if basis_pct < -0.02 else "FLAT"
    return {
        "available": True,
        "mark_price": mark,
        "index_price": index,
        "basis_pct": round(basis_pct, 5),
        "state": state,
    }


def _range_patterns(rows: list[list[Any]]) -> dict[str, Any]:
    work = rows[-48:]
    if len(work) < 24:
        return {"available": False}
    atr = max(_atr(work), _f(work[-1][4]) * 0.0005, 1e-12)
    base = work[-24:-4]
    recent = work[-4:]
    base_high = max(_f(r[2]) for r in base)
    base_low = min(_f(r[3]) for r in base)
    buffer = max(atr * 0.08, _f(work[-1][4]) * 0.0002)
    last_close = _f(recent[-1][4])

    bull_break_idx = next((i for i, r in enumerate(recent) if _f(r[4]) > base_high + buffer), None)
    bear_break_idx = next((i for i, r in enumerate(recent) if _f(r[4]) < base_low - buffer), None)
    bull_retest = False
    bear_retest = False
    if bull_break_idx is not None:
        for r in recent[bull_break_idx + 1:]:
            if _f(r[3]) <= base_high + atr * 0.18 and _f(r[4]) >= base_high:
                bull_retest = True
    if bear_break_idx is not None:
        for r in recent[bear_break_idx + 1:]:
            if _f(r[2]) >= base_low - atr * 0.18 and _f(r[4]) <= base_low:
                bear_retest = True

    high_deviation = max(_f(r[2]) for r in recent) > base_high + buffer and last_close < base_high
    low_deviation = min(_f(r[3]) for r in recent) < base_low - buffer and last_close > base_low

    patterns: list[dict[str, Any]] = []
    if bull_retest:
        patterns.append({"name": "BREAKOUT_RETEST_LONG", "bias": "LONG", "quality": 80.0})
    if bear_retest:
        patterns.append({"name": "BREAKDOWN_RETEST_SHORT", "bias": "SHORT", "quality": 80.0})
    if high_deviation:
        patterns.append({"name": "FAILED_BREAKOUT_RANGE_DEVIATION", "bias": "SHORT", "quality": 76.0})
    if low_deviation:
        patterns.append({"name": "FAILED_BREAKDOWN_RANGE_DEVIATION", "bias": "LONG", "quality": 76.0})

    return {
        "available": True,
        "base_high": round(base_high, 12),
        "base_low": round(base_low, 12),
        "patterns": patterns,
        "bullish_breakout": bull_break_idx is not None,
        "bearish_breakdown": bear_break_idx is not None,
        "bullish_retest": bull_retest,
        "bearish_retest": bear_retest,
        "high_range_deviation": high_deviation,
        "low_range_deviation": low_deviation,
    }


def _derivatives_context(metrics: dict[str, Any], premium: dict[str, Any], coinglass: dict[str, Any]) -> dict[str, Any]:
    price_change = _f(metrics.get("change_15m_pct"))
    oi_change = _f(metrics.get("oi_change_pct"))
    funding = _f(metrics.get("funding_rate"))
    basis = _basis_context(premium)
    state = "NEUTRAL"
    notes: list[str] = []

    if price_change > 0.10 and oi_change > 0.25:
        state = "NEW_LONG_PARTICIPATION"
        notes.append("price_up_oi_up")
    elif price_change < -0.10 and oi_change > 0.25:
        state = "NEW_SHORT_PARTICIPATION"
        notes.append("price_down_oi_up")
    elif price_change > 0.10 and oi_change < -0.25:
        state = "SHORT_COVERING"
        notes.append("price_up_oi_down")
    elif price_change < -0.10 and oi_change < -0.25:
        state = "LONG_DELEVERAGING"
        notes.append("price_down_oi_down")

    crowding = "NONE"
    if price_change > 0.10 and oi_change > 0.25 and funding >= 0.0005:
        crowding = "LONGS_CROWDED"
    elif price_change < -0.10 and oi_change > 0.25 and funding <= -0.0005:
        crowding = "SHORTS_CROWDED"

    liq = dict(coinglass.get("liquidations") or {}) if isinstance(coinglass, dict) else {}
    imbalance = _f(liq.get("short_minus_long_imbalance_1h"))
    squeeze = "NONE"
    if bool(liq.get("available")):
        if imbalance >= 0.25:
            squeeze = "SHORT_SQUEEZE_PRESSURE"
        elif imbalance <= -0.25:
            squeeze = "LONG_SQUEEZE_PRESSURE"

    return {
        "state": state,
        "oi_change_pct": round(oi_change, 4),
        "funding_rate": funding,
        "crowding": crowding,
        "liquidation_squeeze": squeeze,
        "liquidation_imbalance_1h": round(imbalance, 4),
        "basis": basis,
        "notes": notes,
    }


def _absorption_exhaustion(
    metrics: dict[str, Any],
    futures_cvd: dict[str, Any],
    spot_cvd: dict[str, Any],
) -> dict[str, Any]:
    change_5m = _f(metrics.get("change_5m_pct"))
    change_15m = _f(metrics.get("change_15m_pct"))
    atr_pct = max(_f(metrics.get("atr_pct")), 0.05)
    oi = _f(metrics.get("oi_change_pct"))
    rvol = _f(metrics.get("relative_volume"), 1.0)
    accel = _f(metrics.get("volume_acceleration"), 1.0)
    fdelta = _f(futures_cvd.get("normalized_delta"))
    sdelta = _f(spot_cvd.get("normalized_delta"))
    book = _f(metrics.get("order_book_imbalance"))

    absorption = "NONE"
    if fdelta >= 0.12 and change_5m <= 0.06 and book <= 0.03:
        absorption = "BUYS_ABSORBED"
    elif fdelta <= -0.12 and change_5m >= -0.06 and book >= -0.03:
        absorption = "SELLS_ABSORBED"

    exhaustion = "NONE"
    extended = max(1.0, atr_pct * 1.25)
    if change_15m >= extended and (sdelta <= 0.0 or fdelta <= 0.02) and (oi <= 0.0 or accel < 1.0):
        exhaustion = "UPSIDE_EXHAUSTION"
    elif change_15m <= -extended and (sdelta >= 0.0 or fdelta >= -0.02) and (oi <= 0.0 or accel < 1.0):
        exhaustion = "DOWNSIDE_EXHAUSTION"

    return {
        "absorption": absorption,
        "exhaustion": exhaustion,
        "relative_volume": round(rvol, 4),
        "volume_acceleration": round(accel, 4),
    }


def _layer_scores(
    metrics: dict[str, Any],
    range_patterns: dict[str, Any],
    vwap: dict[str, Any],
    ema: dict[str, Any],
    adx: dict[str, Any],
    bb: dict[str, Any],
    futures_cvd: dict[str, Any],
    spot_cvd: dict[str, Any],
    derivatives: dict[str, Any],
    absorption: dict[str, Any],
) -> dict[str, Any]:
    long_score = 0.0
    short_score = 0.0
    evidence: list[str] = []

    btc = str(metrics.get("btc_trend") or "NEUTRAL")
    if btc == "BULLISH":
        long_score += 6
    elif btc == "BEARISH":
        short_score += 6

    ema_bias = str(ema.get("context_bias") or "NEUTRAL")
    if ema_bias == "LONG":
        long_score += 8
    elif ema_bias == "SHORT":
        short_score += 8

    for p in range_patterns.get("patterns") or []:
        if p.get("bias") == "LONG":
            long_score += 12
            evidence.append(str(p.get("name")))
        elif p.get("bias") == "SHORT":
            short_score += 12
            evidence.append(str(p.get("name")))

    rvol = _f(metrics.get("relative_volume"), 1.0)
    accel = _f(metrics.get("volume_acceleration"), 1.0)
    if rvol >= 1.35:
        long_score += 4
        short_score += 4
        evidence.append("RVOL_HIGH")
    if accel >= 1.20:
        long_score += 3
        short_score += 3
        evidence.append("VOLUME_ACCELERATION")

    fdelta = _f(futures_cvd.get("normalized_delta"))
    sdelta = _f(spot_cvd.get("normalized_delta"))
    if fdelta >= 0.06:
        long_score += 7
    elif fdelta <= -0.06:
        short_score += 7
    if sdelta >= 0.05:
        long_score += 9
    elif sdelta <= -0.05:
        short_score += 9

    dstate = str(derivatives.get("state") or "")
    if dstate == "NEW_LONG_PARTICIPATION":
        long_score += 8
    elif dstate == "NEW_SHORT_PARTICIPATION":
        short_score += 8
    elif dstate == "SHORT_COVERING":
        long_score += 3
    elif dstate == "LONG_DELEVERAGING":
        short_score += 3

    if str(derivatives.get("crowding")) == "LONGS_CROWDED":
        long_score -= 8
        evidence.append("LONG_CROWDING_RISK")
    elif str(derivatives.get("crowding")) == "SHORTS_CROWDED":
        short_score -= 8
        evidence.append("SHORT_CROWDING_RISK")

    if bool(bb.get("compressed")):
        long_score += 2
        short_score += 2
    if bool(bb.get("expanding")) and rvol >= 1.35:
        if fdelta > 0:
            long_score += 5
        elif fdelta < 0:
            short_score += 5

    if str(adx.get("trend_strength")) == "STRONG":
        if adx.get("bias") == "LONG":
            long_score += 5
        elif adx.get("bias") == "SHORT":
            short_score += 5

    vwap_bias = str(vwap.get("mean_reversion_bias") or "NEUTRAL")
    if vwap_bias == "LONG":
        long_score += 3
    elif vwap_bias == "SHORT":
        short_score += 3

    if futures_cvd.get("divergence") == "BULLISH_CVD_DIVERGENCE" or spot_cvd.get("divergence") == "BULLISH_CVD_DIVERGENCE":
        long_score += 6
        evidence.append("BULLISH_CVD_DIVERGENCE")
    if futures_cvd.get("divergence") == "BEARISH_CVD_DIVERGENCE" or spot_cvd.get("divergence") == "BEARISH_CVD_DIVERGENCE":
        short_score += 6
        evidence.append("BEARISH_CVD_DIVERGENCE")

    if absorption.get("absorption") == "SELLS_ABSORBED":
        long_score += 5
    elif absorption.get("absorption") == "BUYS_ABSORBED":
        short_score += 5
    if absorption.get("exhaustion") == "DOWNSIDE_EXHAUSTION":
        long_score += 5
    elif absorption.get("exhaustion") == "UPSIDE_EXHAUSTION":
        short_score += 5

    edge = long_score - short_score
    bias = "LONG" if edge >= 8 else "SHORT" if edge <= -8 else "NEUTRAL"
    strength = _clip(50.0 + abs(edge) * 2.0)
    return {
        "long_points": round(long_score, 2),
        "short_points": round(short_score, 2),
        "aggregate_bias": bias,
        "evidence_strength_score": round(strength, 2),
        "evidence": evidence[:16],
        "score_is_probability": False,
    }


def _pump_confluence(
    metrics: dict[str, Any],
    range_patterns: dict[str, Any],
    bb: dict[str, Any],
    futures_cvd: dict[str, Any],
    spot_cvd: dict[str, Any],
    derivatives: dict[str, Any],
) -> dict[str, Any]:
    scores = {"LONG": 0.0, "SHORT": 0.0}
    reasons = {"LONG": [], "SHORT": []}
    rvol = _f(metrics.get("relative_volume"), 1.0)
    accel = _f(metrics.get("volume_acceleration"), 1.0)
    oi = _f(metrics.get("oi_change_pct"))
    funding = _f(metrics.get("funding_rate"))
    btc = str(metrics.get("btc_trend") or "NEUTRAL")
    fdelta = _f(futures_cvd.get("normalized_delta"))
    sdelta = _f(spot_cvd.get("normalized_delta"))

    if rvol >= 1.5:
        scores["LONG"] += 14; scores["SHORT"] += 14
        reasons["LONG"].append("rvol"); reasons["SHORT"].append("rvol")
    if accel >= 1.25:
        scores["LONG"] += 10; scores["SHORT"] += 10
        reasons["LONG"].append("volume_acceleration"); reasons["SHORT"].append("volume_acceleration")
    if oi >= 0.30:
        scores["LONG"] += 10; scores["SHORT"] += 10
    if fdelta >= 0.06:
        scores["LONG"] += 12; reasons["LONG"].append("futures_delta")
    elif fdelta <= -0.06:
        scores["SHORT"] += 12; reasons["SHORT"].append("futures_delta")
    if sdelta >= 0.05:
        scores["LONG"] += 16; reasons["LONG"].append("spot_delta")
    elif sdelta <= -0.05:
        scores["SHORT"] += 16; reasons["SHORT"].append("spot_delta")

    for p in range_patterns.get("patterns") or []:
        if p.get("name") == "BREAKOUT_RETEST_LONG":
            scores["LONG"] += 18; reasons["LONG"].append("breakout_retest")
        elif p.get("name") == "BREAKDOWN_RETEST_SHORT":
            scores["SHORT"] += 18; reasons["SHORT"].append("breakdown_retest")

    if bool(bb.get("compressed")):
        scores["LONG"] += 4; scores["SHORT"] += 4
    if bool(bb.get("expanding")) and rvol >= 1.35:
        if fdelta > 0:
            scores["LONG"] += 8; reasons["LONG"].append("volatility_expansion")
        elif fdelta < 0:
            scores["SHORT"] += 8; reasons["SHORT"].append("volatility_expansion")

    if funding < 0.0005:
        scores["LONG"] += 4
    if funding > -0.0005:
        scores["SHORT"] += 4
    if btc != "BEARISH":
        scores["LONG"] += 5
    if btc != "BULLISH":
        scores["SHORT"] += 5

    squeeze = str(derivatives.get("liquidation_squeeze") or "NONE")
    if squeeze == "SHORT_SQUEEZE_PRESSURE":
        scores["LONG"] += 5; reasons["LONG"].append("short_liquidations")
    elif squeeze == "LONG_SQUEEZE_PRESSURE":
        scores["SHORT"] += 5; reasons["SHORT"].append("long_liquidations")

    crowding = str(derivatives.get("crowding") or "NONE")
    if crowding == "LONGS_CROWDED":
        scores["LONG"] -= 12; reasons["LONG"].append("crowding_penalty")
    elif crowding == "SHORTS_CROWDED":
        scores["SHORT"] -= 12; reasons["SHORT"].append("crowding_penalty")

    for side in scores:
        scores[side] = _clip(scores[side])

    side = "LONG" if scores["LONG"] > scores["SHORT"] else "SHORT" if scores["SHORT"] > scores["LONG"] else "NEUTRAL"
    top = max(scores.values())
    state = "STRONG" if top >= 72 else "ARMED" if top >= 55 else "WATCH" if top >= 38 else "QUIET"
    return {
        "bias": side,
        "state": state,
        "long_score": round(scores["LONG"], 1),
        "short_score": round(scores["SHORT"], 1),
        "score_is_probability": False,
        "reasons": reasons.get(side, [])[:12] if side in reasons else [],
        "can_create_entry": False,
    }


def build_professional_arsenal_context(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [r for r in (snapshot.get("klines") or []) if isinstance(r, list) and len(r) >= 6]
    if len(rows) < 40:
        return {
            "version": VERSION,
            "available": False,
            "reason": "insufficient_klines",
            "policy": POLICY,
        }

    metrics = dict(scored.get("metrics") or {})
    cg = dict(coinglass or {})
    vwap = _vwap_context(rows)
    ema = _ema_context(rows)
    bb = _bollinger_context(rows)
    adx = _adx_context(rows)
    mfi = _mfi_context(rows)
    stoch_rsi = _stoch_rsi_context(rows)
    futures_cvd = _trade_cvd(snapshot.get("agg_trades") or [])
    spot_cvd = _trade_cvd(snapshot.get("spot_agg_trades") or [])
    range_patterns = _range_patterns(rows)
    price_action = detect_price_action_patterns(rows)
    derivatives = _derivatives_context(metrics, snapshot.get("premium") or {}, cg)
    absorption = _absorption_exhaustion(metrics, futures_cvd, spot_cvd)
    layers = _layer_scores(
        metrics, range_patterns, vwap, ema, adx, bb, futures_cvd, spot_cvd, derivatives, absorption
    )
    pump = _pump_confluence(metrics, range_patterns, bb, futures_cvd, spot_cvd, derivatives)

    spot_vs_futures = "NEUTRAL"
    fd = _f(futures_cvd.get("normalized_delta"))
    sd = _f(spot_cvd.get("normalized_delta"))
    if sd >= 0.05 and fd <= -0.05:
        spot_vs_futures = "SPOT_BUYING_FUTURES_SELLING"
    elif sd <= -0.05 and fd >= 0.05:
        spot_vs_futures = "SPOT_SELLING_FUTURES_BUYING"

    return {
        "version": VERSION,
        "available": True,
        "research_only": False,
        "score_is_probability": False,
        "policy": POLICY,
        "patterns": range_patterns,
        "price_action_pattern_vision": price_action,
        "vwap": vwap,
        "ema_context": ema,
        "volume_profile_note": "ExplodeX technical_arsenal already computes approximate POC/value area; not double-counted here.",
        "rvol": {
            "relative_volume": _f(metrics.get("relative_volume"), 1.0),
            "volume_acceleration": _f(metrics.get("volume_acceleration"), 1.0),
        },
        "atr": {
            "atr_pct": _f(metrics.get("atr_pct")),
            "used_for_dynamic_risk_geometry_elsewhere": True,
        },
        "momentum": {
            "adx": adx,
            "mfi": mfi,
            "stoch_rsi": stoch_rsi,
            "rsi_macd_note": "RSI/MACD already exist elsewhere in ExplodeX and remain confirmation, not trigger.",
        },
        "volatility": {
            "bollinger_bandwidth": bb,
            "compression_ratio": _f(metrics.get("compression_ratio"), 1.0),
        },
        "flow": {
            "futures_cvd_proxy": futures_cvd,
            "spot_cvd_proxy": spot_cvd,
            "spot_vs_futures": spot_vs_futures,
            "order_book_imbalance": _f(metrics.get("order_book_imbalance")),
        },
        "derivatives": derivatives,
        "absorption_exhaustion": absorption,
        "layers": {
            "market": {
                "btc_trend": metrics.get("btc_trend"),
                "btc_change_15m_pct": metrics.get("btc_change_15m_pct"),
                "btc_change_1h_pct": metrics.get("btc_change_1h_pct"),
            },
            "structure": {
                "range_patterns": range_patterns,
                "price_action_pattern_vision": price_action,
            },
            "liquidity": {
                "order_book_imbalance": metrics.get("order_book_imbalance"),
                "spread_bps": metrics.get("order_book_spread_bps"),
                "liquidation_squeeze": derivatives.get("liquidation_squeeze"),
            },
            "volume": {
                "relative_volume": metrics.get("relative_volume"),
                "volume_acceleration": metrics.get("volume_acceleration"),
                "vwap": vwap,
            },
            "derivatives": derivatives,
            "momentum": {"adx": adx, "mfi": mfi, "stoch_rsi": stoch_rsi},
            "volatility": {"atr_pct": metrics.get("atr_pct"), "bollinger": bb},
        },
        "aggregate": layers,
        "pump_hunter": pump,
        "note": (
            "Professional confluence layer: price/structure/liquidity/volume/derivatives first; "
            "geometric chart/candlestick/harmonic patterns are shadow evidence until confirmed; "
            "oscillators are confirmation only. No single indicator or pattern can authorize a trade."
        ),
    }
