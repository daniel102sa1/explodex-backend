from __future__ import annotations

from statistics import mean
from typing import Any


def _pct_change(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return ((b - a) / a) * 100


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _atr_pct(klines: list[list[Any]], period: int = 14) -> float:
    if len(klines) < 2:
        return 0.0
    trs: list[float] = []
    start = max(1, len(klines) - period)
    for i in range(start, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i - 1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    current = float(klines[-1][4])
    return (mean(trs) / current) * 100 if trs and current else 0.0


def _range_pct(highs: list[float], lows: list[float], lookback: int, current: float) -> float:
    if not highs or not lows or current == 0:
        return 0.0
    h = max(highs[-lookback:])
    l = min(lows[-lookback:])
    return ((h - l) / current) * 100


def build_btc_context(klines: list[list[Any]]) -> dict[str, float | str]:
    closes = [float(k[4]) for k in klines]
    if len(closes) < 13:
        return {"change_15m_pct": 0.0, "change_1h_pct": 0.0, "trend": "NEUTRAL"}

    change_15m = _pct_change(closes[-4], closes[-1])
    change_1h = _pct_change(closes[-13], closes[-1])
    ema9 = _ema(closes[-30:], 9)
    ema21 = _ema(closes[-40:], 21)

    if change_1h <= -1.5 or (ema9 < ema21 and change_15m <= -0.7):
        trend = "BEARISH"
    elif change_1h >= 1.5 or (ema9 > ema21 and change_15m >= 0.7):
        trend = "BULLISH"
    else:
        trend = "NEUTRAL"

    return {
        "change_15m_pct": round(change_15m, 4),
        "change_1h_pct": round(change_1h, 4),
        "trend": trend,
    }


def score_snapshot(
    snapshot: dict[str, Any],
    btc_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    klines = snapshot["klines"]
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    quote_volumes = [float(k[7]) for k in klines]

    current_price = closes[-1]
    change_5m = _pct_change(closes[-2], closes[-1]) if len(closes) >= 2 else 0.0
    change_15m = _pct_change(closes[-4], closes[-1]) if len(closes) >= 4 else 0.0
    change_1h = _pct_change(closes[-13], closes[-1]) if len(closes) >= 13 else 0.0

    ema9 = _ema(closes[-40:], 9)
    ema21 = _ema(closes[-60:], 21)
    ema_gap_pct = ((ema9 - ema21) / current_price) * 100 if current_price else 0.0
    atr_pct = _atr_pct(klines, 14)

    recent_vol = mean(quote_volumes[-3:]) if len(quote_volumes) >= 3 else quote_volumes[-1]
    base_slice = quote_volumes[-24:-3] if len(quote_volumes) >= 24 else quote_volumes[:-3]
    base_vol = mean(base_slice or quote_volumes)
    relative_volume = recent_vol / base_vol if base_vol > 0 else 1.0

    previous_vol_slice = quote_volumes[-6:-3]
    previous_vol = mean(previous_vol_slice) if previous_vol_slice else base_vol
    volume_acceleration = recent_vol / previous_vol if previous_vol > 0 else 1.0

    oi_hist = snapshot.get("open_interest_history", [])
    oi_values = [
        float(x.get("sumOpenInterest", 0))
        for x in oi_hist
        if float(x.get("sumOpenInterest", 0) or 0) > 0
    ]
    oi_change_pct = _pct_change(oi_values[0], oi_values[-1]) if len(oi_values) >= 2 else 0.0

    taker = snapshot.get("taker", [])
    taker_values = [float(x.get("buySellRatio", 1) or 1) for x in taker]
    taker_latest = taker_values[-1] if taker_values else 1.0
    taker_avg_3 = mean(taker_values[-3:]) if taker_values else 1.0
    taker_prev_3 = mean(taker_values[-6:-3]) if len(taker_values) >= 6 else 1.0
    taker_strengthening_long = taker_avg_3 > taker_prev_3 and taker_avg_3 >= 1.05
    taker_strengthening_short = taker_avg_3 < taker_prev_3 and taker_avg_3 <= 0.95

    funding = float(snapshot.get("premium", {}).get("lastFundingRate", 0) or 0)

    range_12_pct = _range_pct(highs, lows, 12, current_price)
    range_48_pct = _range_pct(highs, lows, 48, current_price)
    compression_ratio = range_12_pct / range_48_pct if range_48_pct > 0 else 1.0
    compressed = compression_ratio <= 0.55 and range_12_pct <= 3.5

    recent_high = max(highs[-24:])
    recent_low = min(lows[-24:])
    distance_to_high_pct = ((recent_high - current_price) / current_price) * 100 if current_price else 0.0
    distance_to_low_pct = ((current_price - recent_low) / current_price) * 100 if current_price else 0.0

    components_long = {
        "structure": 10.0,
        "oi": 10.0,
        "taker": 10.0,
        "volume": 10.0,
        "compression": 10.0,
        "btc": 10.0,
        "funding": 10.0,
        "response": 10.0,
    }
    components_short = dict(components_long)

    # Structure / trend alignment.
    if ema9 > ema21 and change_15m >= -0.15:
        components_long["structure"] += 8
        components_short["structure"] -= 5
    elif ema9 < ema21 and change_15m <= 0.15:
        components_short["structure"] += 8
        components_long["structure"] -= 5

    # OI must agree with price direction; rising OI against direction is a warning.
    if oi_change_pct >= 0.30:
        if change_15m > 0.10:
            components_long["oi"] += 10
            components_short["oi"] -= 5
        elif change_15m < -0.10:
            components_short["oi"] += 10
            components_long["oi"] -= 5
    elif oi_change_pct <= -0.50:
        components_long["oi"] -= 5
        components_short["oi"] -= 5

    # Aggressive flow; repeated/strengthening flow scores more than a single spike.
    if taker_avg_3 >= 1.15:
        components_long["taker"] += 8
        components_short["taker"] -= 6
        if taker_strengthening_long:
            components_long["taker"] += 4
    elif taker_avg_3 <= 0.87:
        components_short["taker"] += 8
        components_long["taker"] -= 6
        if taker_strengthening_short:
            components_short["taker"] += 4

    # Volume expansion before/around the break.
    if relative_volume >= 1.35:
        if change_15m >= 0:
            components_long["volume"] += 6
        if change_15m <= 0:
            components_short["volume"] += 6
    if volume_acceleration >= 1.25:
        components_long["volume"] += 2
        components_short["volume"] += 2

    # Compression is useful for both sides; proximity decides which side has room to trigger first.
    if compressed:
        components_long["compression"] += 7
        components_short["compression"] += 7
    if 0 <= distance_to_high_pct <= max(0.40, atr_pct * 1.2):
        components_long["compression"] += 3
    if 0 <= distance_to_low_pct <= max(0.40, atr_pct * 1.2):
        components_short["compression"] += 3

    # Funding crowding filter.
    if funding > 0.0005:
        components_long["funding"] -= 6
    elif funding < -0.0005:
        components_short["funding"] -= 6

    # BTC regime filter. This is deliberately asymmetric: don't fight a strong BTC impulse.
    btc_context = btc_context or {"trend": "NEUTRAL", "change_15m_pct": 0.0, "change_1h_pct": 0.0}
    btc_trend = str(btc_context.get("trend", "NEUTRAL"))
    if btc_trend == "BEARISH":
        components_long["btc"] -= 10
        components_short["btc"] += 4
    elif btc_trend == "BULLISH":
        components_short["btc"] -= 10
        components_long["btc"] += 4

    # Price response / absorption detection.
    long_absorption_conflict = taker_avg_3 >= 1.20 and change_15m <= 0.05
    short_absorption_conflict = taker_avg_3 <= 0.83 and change_15m >= -0.05
    if long_absorption_conflict:
        components_long["response"] -= 14
    elif taker_avg_3 >= 1.10 and change_15m >= 0.15:
        components_long["response"] += 6

    if short_absorption_conflict:
        components_short["response"] -= 14
    elif taker_avg_3 <= 0.90 and change_15m <= -0.15:
        components_short["response"] += 6

    # Don't chase a move that already expanded too much in the last hour.
    if change_1h >= 4.5:
        components_long["structure"] -= 10
    if change_1h <= -4.5:
        components_short["structure"] -= 10

    long_score = sum(max(0.0, min(20.0, v)) for v in components_long.values()) / 1.6
    short_score = sum(max(0.0, min(20.0, v)) for v in components_short.values()) / 1.6
    long_score = max(0.0, min(100.0, long_score))
    short_score = max(0.0, min(100.0, short_score))

    direction = "LONG" if long_score >= short_score else "SHORT"
    setup_score = max(long_score, short_score)
    selected_components = components_long if direction == "LONG" else components_short

    absorption_conflict = long_absorption_conflict if direction == "LONG" else short_absorption_conflict
    direction_against_btc = (direction == "LONG" and btc_trend == "BEARISH") or (
        direction == "SHORT" and btc_trend == "BULLISH"
    )

    risk_score = 15.0
    if abs(change_1h) > 4:
        risk_score += 15
    if relative_volume > 4.0:
        risk_score += 8
    if absorption_conflict:
        risk_score += 30
    if direction_against_btc:
        risk_score += 20
    if abs(funding) > 0.0005:
        risk_score += 8
    if oi_change_pct < -0.75:
        risk_score += 8
    if atr_pct > 2.5:
        risk_score += 10
    risk_score = min(100.0, risk_score)

    if setup_score >= 84 and risk_score <= 35:
        state = "READY"
    elif setup_score >= 74 and risk_score <= 50:
        state = "PREPARING"
    elif setup_score >= 64 and risk_score <= 65:
        state = "WATCH"
    else:
        state = "NO_TRADE"

    # Structure-based invalidation with an ATR buffer. Avoid microscopic stops in noisy coins.
    atr_buffer_pct = max(0.20, min(1.25, atr_pct * 0.45)) / 100
    if direction == "LONG":
        stop = min(lows[-12:]) * (1 - atr_buffer_pct)
    else:
        stop = max(highs[-12:]) * (1 + atr_buffer_pct)

    risk_per_unit = max(abs(current_price - stop), current_price * 0.001)
    if direction == "LONG":
        tp1 = current_price + risk_per_unit * 1.5
        tp2 = current_price + risk_per_unit * 2.5
        tp3 = current_price + risk_per_unit * 4.0
    else:
        tp1 = current_price - risk_per_unit * 1.5
        tp2 = current_price - risk_per_unit * 2.5
        tp3 = current_price - risk_per_unit * 4.0

    risk_pct_from_entry = (risk_per_unit / current_price) * 100 if current_price else 0.0
    expected_move_min_pct = max(2.0, risk_pct_from_entry * 1.5)
    expected_move_max_pct = max(expected_move_min_pct + 1.0, risk_pct_from_entry * 4.0)

    # We estimate a holding horizon, not a certainty. Compressed setups generally need longer to develop.
    if compressed and atr_pct < 1.0:
        duration_min, duration_max = 360, 2160  # 6h to 36h
    elif compressed:
        duration_min, duration_max = 180, 1440  # 3h to 24h
    else:
        duration_min, duration_max = 60, 720  # 1h to 12h

    component_scores = {
        key: round(max(0.0, min(20.0, value)), 2)
        for key, value in selected_components.items()
    }

    return {
        "direction": direction,
        "state": state,
        "setup_score": round(setup_score, 2),
        "long_score": round(long_score, 2),
        "short_score": round(short_score, 2),
        "risk_score": round(risk_score, 2),
        # Do not pretend this is a calibrated probability until we have enough backtest/paper-trade data.
        "confidence_pct": None,
        "current_price": current_price,
        "entry_low": round(current_price * 0.999, 12),
        "entry_high": round(current_price * 1.001, 12),
        "stop_loss": round(stop, 12),
        "tp1": round(tp1, 12),
        "tp2": round(tp2, 12),
        "tp3": round(tp3, 12),
        "expected_move_min_pct": round(expected_move_min_pct, 3),
        "expected_move_max_pct": round(expected_move_max_pct, 3),
        "expected_duration_min_minutes": duration_min,
        "expected_duration_max_minutes": duration_max,
        "components": component_scores,
        "metrics": {
            "change_5m_pct": round(change_5m, 4),
            "change_15m_pct": round(change_15m, 4),
            "change_1h_pct": round(change_1h, 4),
            "ema9": round(ema9, 12),
            "ema21": round(ema21, 12),
            "ema_gap_pct": round(ema_gap_pct, 4),
            "atr_pct": round(atr_pct, 4),
            "relative_volume": round(relative_volume, 4),
            "volume_acceleration": round(volume_acceleration, 4),
            "oi_change_pct": round(oi_change_pct, 4),
            "taker_latest": round(taker_latest, 4),
            "taker_avg_3": round(taker_avg_3, 4),
            "taker_prev_3": round(taker_prev_3, 4),
            "taker_strengthening_long": taker_strengthening_long,
            "taker_strengthening_short": taker_strengthening_short,
            "funding_rate": funding,
            "range_12_pct": round(range_12_pct, 4),
            "range_48_pct": round(range_48_pct, 4),
            "compression_ratio": round(compression_ratio, 4),
            "compressed": compressed,
            "distance_to_high_pct": round(distance_to_high_pct, 4),
            "distance_to_low_pct": round(distance_to_low_pct, 4),
            "long_absorption_conflict": long_absorption_conflict,
            "short_absorption_conflict": short_absorption_conflict,
            "absorption_conflict": absorption_conflict,
            "btc_trend": btc_trend,
            "btc_change_15m_pct": btc_context.get("change_15m_pct", 0.0),
            "btc_change_1h_pct": btc_context.get("change_1h_pct", 0.0),
        },
    }
