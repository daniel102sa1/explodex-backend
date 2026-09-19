from __future__ import annotations

import asyncio
from typing import Any

from app.services.binance import binance_client

REGIME_VERSION = "paper_regime_router_v2_btc_adaptive"


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _atr_pct(rows: list[list[Any]], period: int = 14) -> float:
    usable = [row for row in rows if len(row) >= 5]
    if len(usable) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = _f(usable[0][4])
    for row in usable[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    sample = trs[-period:]
    last = _f(usable[-1][4])
    if not sample or last <= 0:
        return 0.0
    return (sum(sample) / len(sample)) / last * 100.0


def _ret(rows: list[list[Any]], bars: int) -> float:
    usable = [row for row in rows if len(row) >= 5]
    if len(usable) <= bars:
        return 0.0
    start = _f(usable[-(bars + 1)][4])
    end = _f(usable[-1][4])
    if start <= 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def _percentile_rank(values: list[float], value: float) -> float:
    clean = sorted(v for v in values if v >= 0)
    if not clean:
        return 0.0
    below_or_equal = sum(1 for item in clean if item <= value)
    return below_or_equal / len(clean) * 100.0


def _rolling_atr_pct(rows: list[list[Any]], period: int = 14) -> list[float]:
    usable = [row for row in rows if len(row) >= 5]
    if len(usable) < period + 2:
        return []
    out: list[float] = []
    for end in range(period + 1, len(usable) + 1):
        sample = usable[:end]
        value = _atr_pct(sample, period)
        if value > 0:
            out.append(value)
    return out


def _direction_label(trend: float, ret_15m: float, ret_60m: float) -> str:
    signed = trend * 0.55 + (1.0 if ret_15m > 0 else -1.0 if ret_15m < 0 else 0.0) * 0.20 + (1.0 if ret_60m > 0 else -1.0 if ret_60m < 0 else 0.0) * 0.25
    if signed >= 0.35:
        return "BULLISH"
    if signed <= -0.35:
        return "BEARISH"
    return "NEUTRAL"


def btc_adaptive_overlay(btc_5m: list[list[Any]], btc_15m: list[list[Any]]) -> dict[str, Any]:
    btc = _asset_features(btc_5m, btc_15m)
    atr_series = _rolling_atr_pct(btc_5m, 14)
    current_atr = btc.get("atr_pct", 0.0)
    atr_percentile = _percentile_rank(atr_series[-180:-1] or atr_series[:-1], current_atr) if atr_series else 0.0

    ret_5m = abs(_ret(btc_5m, 1))
    ret_15m = abs(btc.get("ret_15m_pct", 0.0))
    ret_60m = abs(btc.get("ret_60m_pct", 0.0))
    direction = _direction_label(
        btc.get("trend", 0.0),
        btc.get("ret_15m_pct", 0.0),
        btc.get("ret_60m_pct", 0.0),
    )

    if ret_5m >= 1.20 or ret_15m >= 2.30 or ret_60m >= 4.20:
        stress = "SHOCK"
        risk_multiplier = 0.0
        stop_buffer_multiplier = 1.0
        countertrend_multiplier = 0.0
        force_defensive = True
        block_new_entries = True
        min_quality_bonus = 18.0
    elif atr_percentile >= 95 or ret_15m >= 1.65 or ret_60m >= 3.20:
        stress = "EXTREME"
        risk_multiplier = 0.30
        stop_buffer_multiplier = 1.30
        countertrend_multiplier = 0.0
        force_defensive = True
        block_new_entries = False
        min_quality_bonus = 10.0
    elif atr_percentile >= 85 or ret_15m >= 1.10 or ret_60m >= 2.20:
        stress = "HIGH"
        risk_multiplier = 0.50
        stop_buffer_multiplier = 1.18
        countertrend_multiplier = 0.35
        force_defensive = True
        block_new_entries = False
        min_quality_bonus = 6.0
    elif atr_percentile >= 70 or ret_15m >= 0.70 or ret_60m >= 1.40:
        stress = "ELEVATED"
        risk_multiplier = 0.75
        stop_buffer_multiplier = 1.08
        countertrend_multiplier = 0.70
        force_defensive = False
        block_new_entries = False
        min_quality_bonus = 3.0
    else:
        stress = "NORMAL"
        risk_multiplier = 1.0
        stop_buffer_multiplier = 1.0
        countertrend_multiplier = 1.0
        force_defensive = False
        block_new_entries = False
        min_quality_bonus = 0.0

    confirmation_minutes = 15 if stress in {"HIGH", "EXTREME", "SHOCK"} else 5
    return {
        "version": "btc_adaptive_overlay_v1",
        "stress": stress,
        "direction": direction,
        "atr_pct": round(current_atr, 4),
        "atr_percentile_recent": round(atr_percentile, 2),
        "abs_move_5m_pct": round(ret_5m, 4),
        "abs_move_15m_pct": round(ret_15m, 4),
        "abs_move_60m_pct": round(ret_60m, 4),
        "risk_multiplier": risk_multiplier,
        "stop_buffer_multiplier": stop_buffer_multiplier,
        "countertrend_multiplier": countertrend_multiplier,
        "force_defensive": force_defensive,
        "block_new_entries": block_new_entries,
        "min_quality_bonus": min_quality_bonus,
        "confirmation_minutes": confirmation_minutes,
        "stop_policy": "Wider structural buffer is allowed only BEFORE entry; position size must shrink from the final stop. Never widen after entry.",
        "note": "BTC stress is relative to recent BTC volatility plus absolute shock thresholds; not a probability forecast.",
    }


def _asset_features(rows_5m: list[list[Any]], rows_15m: list[list[Any]]) -> dict[str, float]:
    closes_5 = [_f(row[4]) for row in rows_5m if len(row) >= 5][-72:]
    closes_15 = [_f(row[4]) for row in rows_15m if len(row) >= 5][-48:]
    if len(closes_5) < 24 or len(closes_15) < 20:
        return {
            "trend": 0.0,
            "strength": 0.0,
            "atr_pct": 0.0,
            "ret_15m_pct": 0.0,
            "ret_60m_pct": 0.0,
        }

    ema9 = _ema(closes_5, 9)
    ema21 = _ema(closes_5, 21)
    ema15_9 = _ema(closes_15, 9)
    ema15_21 = _ema(closes_15, 21)
    last = closes_5[-1]

    spread_5 = (ema9[-1] - ema21[-1]) / last * 100.0 if last > 0 else 0.0
    slope_5 = (ema21[-1] - ema21[-7]) / last * 100.0 if len(ema21) >= 7 and last > 0 else 0.0
    last15 = closes_15[-1]
    spread_15 = (ema15_9[-1] - ema15_21[-1]) / last15 * 100.0 if last15 > 0 else 0.0

    signed = spread_5 * 0.45 + slope_5 * 0.25 + spread_15 * 0.30
    direction = 1.0 if signed > 0 else -1.0 if signed < 0 else 0.0
    strength = min(100.0, abs(signed) * 180.0)

    return {
        "trend": direction,
        "strength": strength,
        "atr_pct": _atr_pct(rows_5m),
        "ret_15m_pct": _ret(rows_5m, 3),
        "ret_60m_pct": _ret(rows_5m, 12),
    }


def classify_regime(
    btc_5m: list[list[Any]],
    btc_15m: list[list[Any]],
    eth_5m: list[list[Any]],
    eth_15m: list[list[Any]],
) -> dict[str, Any]:
    btc = _asset_features(btc_5m, btc_15m)
    eth = _asset_features(eth_5m, eth_15m)
    btc_overlay = btc_adaptive_overlay(btc_5m, btc_15m)

    trend_score = 0.62 * btc["trend"] * btc["strength"] + 0.38 * eth["trend"] * eth["strength"]
    trend_strength = abs(trend_score)
    avg_atr = 0.62 * btc["atr_pct"] + 0.38 * eth["atr_pct"]
    shock_move = max(abs(btc["ret_15m_pct"]), abs(eth["ret_15m_pct"]), abs(btc["ret_60m_pct"]), abs(eth["ret_60m_pct"]))
    aligned = btc["trend"] != 0 and btc["trend"] == eth["trend"]

    if btc_overlay["stress"] in {"HIGH", "EXTREME", "SHOCK"} or avg_atr >= 1.15 or shock_move >= 2.2:
        regime = "HIGH_VOLATILITY"
        label = "VOLATILIDAD ALTA BTC" if btc_overlay["stress"] != "SHOCK" else "SHOCK BTC"
    elif aligned and trend_strength >= 28:
        regime = "TREND_UP" if trend_score > 0 else "TREND_DOWN"
        label = "TENDENCIA ALCISTA" if trend_score > 0 else "TENDENCIA BAJISTA"
    elif trend_strength <= 14 and avg_atr <= 0.75:
        regime = "RANGE"
        label = "LATERAL / RANGO"
    else:
        regime = "TRANSITION"
        label = "TRANSICIÓN / MIXTO"

    policy = {
        "trend_premove": {"enabled": True, "risk_multiplier": 1.0},
        "range_micro": {"enabled": True, "risk_multiplier": 1.0},
        "micro_scalp": {"enabled": True, "risk_multiplier": 1.0},
    }
    if regime in {"TREND_UP", "TREND_DOWN"}:
        policy["range_micro"] = {"enabled": False, "risk_multiplier": 0.0}
        policy["micro_scalp"] = {"enabled": True, "risk_multiplier": 0.75}
    elif regime == "RANGE":
        policy["range_micro"] = {"enabled": True, "risk_multiplier": 1.0}
        policy["micro_scalp"] = {"enabled": True, "risk_multiplier": 0.85}
    elif regime == "HIGH_VOLATILITY":
        policy["range_micro"] = {"enabled": False, "risk_multiplier": 0.0}
        policy["micro_scalp"] = {"enabled": False, "risk_multiplier": 0.0}
        policy["trend_premove"] = {
            "enabled": not bool(btc_overlay.get("block_new_entries")),
            "risk_multiplier": min(0.55, float(btc_overlay.get("risk_multiplier") or 0.0)),
        }
    else:
        policy["range_micro"] = {"enabled": True, "risk_multiplier": 0.55}
        policy["micro_scalp"] = {"enabled": True, "risk_multiplier": 0.55}

    return {
        "version": REGIME_VERSION,
        "regime": regime,
        "label": label,
        "score_is_probability": False,
        "trend_score": round(trend_score, 2),
        "trend_strength": round(trend_strength, 2),
        "avg_atr_pct": round(avg_atr, 4),
        "shock_move_pct": round(shock_move, 4),
        "btc": {k: round(v, 4) for k, v in btc.items()},
        "eth": {k: round(v, 4) for k, v in eth.items()},
        "btc_overlay": btc_overlay,
        "policy": policy,
        "paper_only": True,
        "note": "Enruta estrategias PAPER según régimen; no crea entradas por sí solo.",
    }


async def current_paper_regime() -> dict[str, Any]:
    values = await asyncio.gather(
        binance_client.klines("BTCUSDT", "5m", 240),
        binance_client.klines("BTCUSDT", "15m", 160),
        binance_client.klines("ETHUSDT", "5m", 120),
        binance_client.klines("ETHUSDT", "15m", 80),
        return_exceptions=True,
    )

    rows = [([] if isinstance(value, Exception) else value) for value in values]
    if any(len(value) < 20 for value in rows):
        return {
            "version": REGIME_VERSION,
            "regime": "UNKNOWN",
            "label": "SIN DATOS SUFICIENTES",
            "paper_only": True,
            "policy": {
                "trend_premove": {"enabled": True, "risk_multiplier": 0.70},
                "range_micro": {"enabled": True, "risk_multiplier": 0.50},
                "micro_scalp": {"enabled": True, "risk_multiplier": 0.50},
            },
            "btc_overlay": {
                "version": "btc_adaptive_overlay_v1",
                "stress": "UNKNOWN",
                "direction": "NEUTRAL",
                "risk_multiplier": 0.70,
                "stop_buffer_multiplier": 1.0,
                "countertrend_multiplier": 0.70,
                "force_defensive": True,
                "block_new_entries": False,
                "min_quality_bonus": 5.0,
                "confirmation_minutes": 15,
            },
            "provider_source": binance_client.active_source,
        }
    result = classify_regime(rows[0], rows[1], rows[2], rows[3])
    result["provider_source"] = binance_client.active_source
    return result
