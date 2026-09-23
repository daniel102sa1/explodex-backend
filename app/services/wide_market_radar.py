from __future__ import annotations

from statistics import mean
from typing import Any

VERSION = "wide_market_radar_v1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _pct(a: float, b: float) -> float:
    return ((b - a) / a) * 100.0 if abs(a) > 1e-12 else 0.0


def _atr(rows: list[list[Any]], period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    sample = trs[-period:]
    return mean(sample) if sample else 0.0


def score_radar_candidate(ticker: dict[str, Any], rows: list[list[Any]]) -> dict[str, Any]:
    """Cheap first-pass radar used before the expensive deep snapshot.

    It is intentionally a ranking layer, not an entry engine. The goal is to stop
    choosing the deep-scan universe almost entirely by 24h liquidity and instead
    prioritize symbols already showing early 5m participation/pressure.
    """
    work = [r for r in rows if isinstance(r, list) and len(r) >= 8][-36:]
    symbol = str(ticker.get("symbol") or "")
    if len(work) < 24:
        return {
            "version": VERSION,
            "symbol": symbol,
            "available": False,
            "priority_score": 0.0,
            "bias": "NEUTRAL",
            "state": "NO_DATA",
            "can_create_entry": False,
            "score_is_probability": False,
        }

    closes = [_f(r[4]) for r in work]
    highs = [_f(r[2]) for r in work]
    lows = [_f(r[3]) for r in work]
    qvol = [_f(r[7]) for r in work]
    current = closes[-1]
    atr = max(_atr(work), current * 0.0005, 1e-12)

    change_5m = _pct(closes[-2], closes[-1])
    change_15m = _pct(closes[-4], closes[-1])
    change_30m = _pct(closes[-7], closes[-1])
    change_24h = _f(ticker.get("priceChangePercent"))

    recent_vol = mean(qvol[-3:])
    base_vol = mean(qvol[-24:-3]) if qvol[-24:-3] else mean(qvol)
    previous_vol = mean(qvol[-6:-3]) if qvol[-6:-3] else base_vol
    rvol = recent_vol / base_vol if base_vol > 1e-12 else 1.0
    accel = recent_vol / previous_vol if previous_vol > 1e-12 else 1.0

    prior_high = max(highs[-21:-1])
    prior_low = min(lows[-21:-1])
    dist_high_atr = max(0.0, prior_high - current) / atr
    dist_low_atr = max(0.0, current - prior_low) / atr

    short_range = max(highs[-6:]) - min(lows[-6:])
    broad_range = max(highs[-24:]) - min(lows[-24:])
    compression_ratio = short_range / broad_range if broad_range > 1e-12 else 1.0
    compressed = compression_ratio <= 0.62

    long_score = 0.0
    short_score = 0.0
    long_reasons: list[str] = []
    short_reasons: list[str] = []

    if rvol >= 2.0:
        long_score += 20; short_score += 20
        long_reasons.append("rvol>=2x"); short_reasons.append("rvol>=2x")
    elif rvol >= 1.5:
        long_score += 14; short_score += 14
        long_reasons.append("rvol>=1.5x"); short_reasons.append("rvol>=1.5x")
    elif rvol >= 1.2:
        long_score += 8; short_score += 8

    if accel >= 1.5:
        long_score += 14; short_score += 14
        long_reasons.append("volume_acceleration"); short_reasons.append("volume_acceleration")
    elif accel >= 1.2:
        long_score += 8; short_score += 8

    if compressed:
        long_score += 10; short_score += 10
        long_reasons.append("compression"); short_reasons.append("compression")

    if 0.10 <= change_15m <= 2.6:
        long_score += 14
        long_reasons.append("early_up_momentum")
    elif change_15m > 3.5:
        long_score -= 12
        long_reasons.append("already_extended")
    if -2.6 <= change_15m <= -0.10:
        short_score += 14
        short_reasons.append("early_down_momentum")
    elif change_15m < -3.5:
        short_score -= 12
        short_reasons.append("already_extended")

    if change_5m > 0 and change_30m > 0:
        long_score += 7
    if change_5m < 0 and change_30m < 0:
        short_score += 7

    if dist_high_atr <= 0.65:
        long_score += 15
        long_reasons.append("pressing_20bar_high")
    if dist_low_atr <= 0.65:
        short_score += 15
        short_reasons.append("pressing_20bar_low")

    # Avoid making the wide radar chase coins that already had a huge 24h move.
    if abs(change_24h) > 8.0:
        long_score -= 15
        short_score -= 15
    elif abs(change_24h) <= 6.0:
        long_score += 4
        short_score += 4

    long_score = max(0.0, min(100.0, long_score))
    short_score = max(0.0, min(100.0, short_score))
    priority = max(long_score, short_score)
    bias = "LONG" if long_score > short_score else "SHORT" if short_score > long_score else "NEUTRAL"
    state = "HOT" if priority >= 70 else "ARMED" if priority >= 52 else "WATCH" if priority >= 34 else "QUIET"

    return {
        "version": VERSION,
        "symbol": symbol,
        "available": True,
        "priority_score": round(priority, 2),
        "long_score": round(long_score, 2),
        "short_score": round(short_score, 2),
        "bias": bias,
        "state": state,
        "score_is_probability": False,
        "can_create_entry": False,
        "change_5m_pct": round(change_5m, 4),
        "change_15m_pct": round(change_15m, 4),
        "change_30m_pct": round(change_30m, 4),
        "change_24h_pct": round(change_24h, 4),
        "relative_volume": round(rvol, 4),
        "volume_acceleration": round(accel, 4),
        "compression_ratio": round(compression_ratio, 4),
        "compressed": compressed,
        "distance_to_20bar_high_atr": round(dist_high_atr, 4),
        "distance_to_20bar_low_atr": round(dist_low_atr, 4),
        "reasons": (long_reasons if bias == "LONG" else short_reasons if bias == "SHORT" else [])[:10],
    }
