from __future__ import annotations

from statistics import mean
from typing import Any


def _pct_change(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return ((b - a) / a) * 100


def score_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    klines = snapshot["klines"]
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    volumes = [float(k[7]) for k in klines]  # quote asset volume

    current_price = closes[-1]
    change_15m = _pct_change(closes[-4], closes[-1]) if len(closes) >= 4 else 0.0
    change_1h = _pct_change(closes[-13], closes[-1]) if len(closes) >= 13 else 0.0

    recent_vol = mean(volumes[-3:]) if len(volumes) >= 3 else volumes[-1]
    base_vol = mean(volumes[-24:-3]) if len(volumes) >= 24 else mean(volumes[:-3] or volumes)
    relative_volume = recent_vol / base_vol if base_vol > 0 else 1.0

    oi_hist = snapshot.get("open_interest_history", [])
    oi_values = [float(x.get("sumOpenInterest", 0)) for x in oi_hist if float(x.get("sumOpenInterest", 0)) > 0]
    oi_change_pct = _pct_change(oi_values[0], oi_values[-1]) if len(oi_values) >= 2 else 0.0

    taker = snapshot.get("taker", [])
    taker_values = [float(x.get("buySellRatio", 1)) for x in taker]
    taker_latest = taker_values[-1] if taker_values else 1.0
    taker_avg = mean(taker_values[-3:]) if taker_values else 1.0

    funding = float(snapshot.get("premium", {}).get("lastFundingRate", 0) or 0)

    range_20 = max(highs[-20:]) - min(lows[-20:]) if len(highs) >= 20 else max(highs) - min(lows)
    compression_pct = (range_20 / current_price) * 100 if current_price else 0.0

    long_score = 50.0
    short_score = 50.0

    if oi_change_pct > 0.25:
        long_score += 8 if change_15m >= 0 else -6
        short_score += 8 if change_15m < 0 else -6
    elif oi_change_pct < -0.25:
        long_score -= 4
        short_score -= 4

    if taker_avg >= 1.15:
        long_score += 10
        short_score -= 8
    elif taker_avg <= 0.87:
        short_score += 10
        long_score -= 8

    if relative_volume >= 1.5:
        long_score += 7 if change_15m >= 0 else -2
        short_score += 7 if change_15m < 0 else -2

    if 0 < compression_pct <= 2.8:
        long_score += 6
        short_score += 6

    # Penalize absorption: aggressive side fails to move price.
    absorption_conflict = False
    if taker_avg >= 1.25 and change_15m <= 0:
        long_score -= 18
        absorption_conflict = True
    if taker_avg <= 0.80 and change_15m >= 0:
        short_score -= 18
        absorption_conflict = True

    # Avoid chasing already accelerated moves on this early detector.
    if change_1h > 5:
        long_score -= 15
    if change_1h < -5:
        short_score -= 15

    # Crowded funding penalty.
    if funding > 0.0005:
        long_score -= 5
    if funding < -0.0005:
        short_score -= 5

    long_score = max(0.0, min(100.0, long_score))
    short_score = max(0.0, min(100.0, short_score))

    direction = "LONG" if long_score >= short_score else "SHORT"
    setup_score = max(long_score, short_score)

    risk_score = 20.0
    if abs(change_1h) > 4:
        risk_score += 20
    if relative_volume > 3.5:
        risk_score += 10
    if absorption_conflict:
        risk_score += 25
    if abs(funding) > 0.0005:
        risk_score += 10
    risk_score = min(100.0, risk_score)

    if setup_score >= 85 and risk_score <= 35:
        state = "READY"
    elif setup_score >= 75 and risk_score <= 50:
        state = "PREPARING"
    elif setup_score >= 65:
        state = "WATCH"
    else:
        state = "NO_TRADE"

    recent_low = min(lows[-12:])
    recent_high = max(highs[-12:])
    stop = recent_low * 0.997 if direction == "LONG" else recent_high * 1.003
    risk_per_unit = abs(current_price - stop)
    if direction == "LONG":
        tp1 = current_price + risk_per_unit * 1.5
        tp2 = current_price + risk_per_unit * 2.5
        tp3 = current_price + risk_per_unit * 4.0
    else:
        tp1 = current_price - risk_per_unit * 1.5
        tp2 = current_price - risk_per_unit * 2.5
        tp3 = current_price - risk_per_unit * 4.0

    return {
        "direction": direction,
        "state": state,
        "setup_score": round(setup_score, 2),
        "long_score": round(long_score, 2),
        "short_score": round(short_score, 2),
        "risk_score": round(risk_score, 2),
        "current_price": current_price,
        "entry_low": round(current_price * 0.999, 12),
        "entry_high": round(current_price * 1.001, 12),
        "stop_loss": round(stop, 12),
        "tp1": round(tp1, 12),
        "tp2": round(tp2, 12),
        "tp3": round(tp3, 12),
        "metrics": {
            "change_15m_pct": round(change_15m, 4),
            "change_1h_pct": round(change_1h, 4),
            "relative_volume": round(relative_volume, 4),
            "oi_change_pct": round(oi_change_pct, 4),
            "taker_latest": round(taker_latest, 4),
            "taker_avg_3": round(taker_avg, 4),
            "funding_rate": funding,
            "compression_pct": round(compression_pct, 4),
            "absorption_conflict": absorption_conflict,
        },
    }
