from __future__ import annotations

import asyncio
import math
from statistics import median
from typing import Any

from app.services.binance import binance_client


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _candles(rows: list[list[Any]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for row in rows:
        if len(row) < 6:
            continue
        out.append(
            {
                "time": _f(row[0]),
                "open": _f(row[1]),
                "high": _f(row[2]),
                "low": _f(row[3]),
                "close": _f(row[4]),
                "volume": _f(row[7] if len(row) > 7 else row[5]),
            }
        )
    return out


def _sma(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    use = values[-min(period, len(values)):]
    return sum(use) / len(use)


def _sma_series(values: list[float], period: int) -> list[float]:
    result: list[float] = []
    for index in range(len(values)):
        start = max(0, index - period + 1)
        window = values[start : index + 1]
        result.append(sum(window) / len(window))
    return result


def _rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _true_ranges(candles: list[dict[str, float]]) -> list[float]:
    values: list[float] = []
    for i, candle in enumerate(candles):
        if i == 0:
            values.append(candle["high"] - candle["low"])
            continue
        prev = candles[i - 1]["close"]
        values.append(
            max(
                candle["high"] - candle["low"],
                abs(candle["high"] - prev),
                abs(candle["low"] - prev),
            )
        )
    return values


def _atr(candles: list[dict[str, float]], period: int = 14) -> float:
    trs = _true_ranges(candles)
    if not trs:
        return 0.0
    if len(trs) <= period:
        return sum(trs) / len(trs)
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def _atr_percentile(candles: list[dict[str, float]], period: int = 14, sample: int = 60) -> float:
    if len(candles) < period + 5:
        return 50.0
    values: list[float] = []
    start = max(period + 1, len(candles) - sample)
    for end in range(start, len(candles) + 1):
        atr = _atr(candles[:end], period)
        close = candles[end - 1]["close"]
        if close > 0:
            values.append(atr / close * 100)
    if not values:
        return 50.0
    current = values[-1]
    below = sum(1 for value in values if value <= current)
    return below / len(values) * 100


def _slope_pct(series: list[float], lookback: int = 5) -> float:
    if len(series) <= lookback or series[-lookback - 1] == 0:
        return 0.0
    return (series[-1] - series[-lookback - 1]) / abs(series[-lookback - 1]) * 100


def _structure(candles: list[dict[str, float]], window: int = 6) -> str:
    if len(candles) < window * 2 + 2:
        return "NEUTRAL"
    older = candles[-window * 2 : -window]
    recent = candles[-window:]
    older_high = max(x["high"] for x in older)
    older_low = min(x["low"] for x in older)
    recent_high = max(x["high"] for x in recent)
    recent_low = min(x["low"] for x in recent)
    if recent_high > older_high and recent_low > older_low:
        return "HH_HL"
    if recent_high < older_high and recent_low < older_low:
        return "LH_LL"
    return "MIXED"


def _trend(candles: list[dict[str, float]]) -> dict[str, Any]:
    closes = [x["close"] for x in candles]
    ma7_series = _sma_series(closes, 7)
    ma25_series = _sma_series(closes, 25)
    ma99_series = _sma_series(closes, 99)
    price = closes[-1] if closes else 0.0
    ma7 = ma7_series[-1] if ma7_series else 0.0
    ma25 = ma25_series[-1] if ma25_series else 0.0
    ma99 = ma99_series[-1] if ma99_series else 0.0
    slope25 = _slope_pct(ma25_series, 5)
    structure = _structure(candles)
    bullish_votes = int(price > ma25) + int(ma7 > ma25) + int(ma25 > ma99) + int(slope25 > 0) + int(structure == "HH_HL")
    bearish_votes = int(price < ma25) + int(ma7 < ma25) + int(ma25 < ma99) + int(slope25 < 0) + int(structure == "LH_LL")
    if bullish_votes >= 4 and bullish_votes >= bearish_votes + 2:
        label = "BULLISH"
    elif bearish_votes >= 4 and bearish_votes >= bullish_votes + 2:
        label = "BEARISH"
    else:
        label = "NEUTRAL"
    return {
        "trend": label,
        "price": price,
        "ma7": ma7,
        "ma25": ma25,
        "ma99": ma99,
        "ma25_slope_pct": slope25,
        "structure": structure,
        "bullish_votes": bullish_votes,
        "bearish_votes": bearish_votes,
    }


def _daily_regime(candles: list[dict[str, float]]) -> dict[str, Any]:
    trend = _trend(candles)
    recent = candles[-30:] if len(candles) >= 30 else candles
    high = max((x["high"] for x in recent), default=0.0)
    low = min((x["low"] for x in recent), default=0.0)
    price = trend["price"]
    width_pct = ((high - low) / price * 100) if price > 0 else 0.0
    position = ((price - low) / (high - low) * 100) if high > low else 50.0
    flat_ma = abs(_f(trend["ma25_slope_pct"])) < 1.2
    if flat_ma and trend["structure"] == "MIXED":
        regime = "RANGE"
    elif trend["trend"] == "BULLISH":
        regime = "BULLISH"
    elif trend["trend"] == "BEARISH":
        regime = "BEARISH"
    else:
        regime = "TRANSITION"
    return {
        **trend,
        "regime": regime,
        "range_high": high,
        "range_low": low,
        "range_width_pct": width_pct,
        "range_position_pct": position,
    }


def _volume_ratio(candles: list[dict[str, float]], period: int = 20) -> float:
    if len(candles) < 2:
        return 1.0
    vols = [x["volume"] for x in candles]
    baseline = _sma(vols[:-1], period) or 1.0
    return vols[-1] / baseline


def _technical_score(direction: str, daily: dict[str, Any], h4: dict[str, Any], rsi15: float, atr_percentile: float, volume_ratio: float) -> tuple[float, list[str], list[str], str]:
    score = 45.0
    positives: list[str] = []
    warnings: list[str] = []
    same_h4 = h4["trend"] == ("BULLISH" if direction == "LONG" else "BEARISH")
    structure_ok = h4["structure"] == ("HH_HL" if direction == "LONG" else "LH_LL")
    daily_same = daily["regime"] == ("BULLISH" if direction == "LONG" else "BEARISH")
    daily_opposite = daily["regime"] == ("BEARISH" if direction == "LONG" else "BULLISH")

    if same_h4:
        score += 22
        positives.append(f"4H mantiene sesgo {direction}")
    if structure_ok:
        score += 10
        positives.append("estructura 4H acompaña")
    if daily_same:
        score += 10
        positives.append("1D acompaña la dirección")
    elif daily["regime"] == "RANGE":
        score += 5
        positives.append("1D está en rango; 4H puede mandar el tramo local")
    elif daily_opposite:
        score -= 12
        warnings.append("operación contra el marco diario; tratar como scalp, no reversión mayor")

    if direction == "LONG":
        if 45 <= rsi15 <= 68:
            score += 8
            positives.append(f"RSI 15m {rsi15:.1f}: margen antes de sobrecompra")
        elif rsi15 > 75:
            score -= 10
            warnings.append(f"RSI 15m {rsi15:.1f}: entrada LONG extendida")
        elif rsi15 < 35:
            score -= 4
            warnings.append(f"RSI 15m {rsi15:.1f}: momentum todavía débil")
    else:
        if 32 <= rsi15 <= 55:
            score += 8
            positives.append(f"RSI 15m {rsi15:.1f}: presión bajista sin agotamiento extremo")
        elif rsi15 < 25:
            score -= 10
            warnings.append(f"RSI 15m {rsi15:.1f}: SHORT demasiado extendido")
        elif rsi15 > 65:
            score -= 4
            warnings.append(f"RSI 15m {rsi15:.1f}: impulso bajista aún no domina")

    if atr_percentile <= 35:
        score += 6
        positives.append("ATR 15m comprimido: posible expansión próxima")
    elif atr_percentile >= 85:
        score -= 5
        warnings.append("ATR 15m ya está muy expandido; peor momento para perseguir")

    if volume_ratio >= 1.15:
        score += 5
        positives.append(f"volumen 15m {volume_ratio:.2f}x sobre promedio")
    elif volume_ratio < 0.65:
        score -= 4
        warnings.append("volumen 15m débil")

    if daily_opposite:
        setup_style = "SCALP_CONTRA_1D"
        score = min(score, 74.0)
    elif daily["regime"] == "RANGE":
        setup_style = "SEGUIMIENTO_4H_EN_RANGO_1D"
    else:
        setup_style = "SEGUIMIENTO_TENDENCIA"

    return max(0.0, min(100.0, score)), positives, warnings, setup_style


async def build_multitimeframe_thesis(symbol: str) -> dict[str, Any]:
    rows15, rows4h, rows1d = await asyncio.gather(
        binance_client.klines(symbol, interval="15m", limit=160),
        binance_client.klines(symbol, interval="4h", limit=140),
        binance_client.klines(symbol, interval="1d", limit=140),
    )
    c15 = _candles(rows15)
    c4h = _candles(rows4h)
    c1d = _candles(rows1d)
    if len(c15) < 30 or len(c4h) < 30 or len(c1d) < 30:
        return {"symbol": symbol, "available": False, "reason": "insufficient_candles"}

    daily = _daily_regime(c1d)
    h4 = _trend(c4h)
    closes15 = [x["close"] for x in c15]
    rsi15 = _rsi(closes15, 14)
    atr15 = _atr(c15, 14)
    price = closes15[-1]
    atr15_pct = atr15 / price * 100 if price > 0 else 0.0
    atr_percentile = _atr_percentile(c15, 14, 60)
    volume_ratio = _volume_ratio(c15, 20)

    if h4["trend"] == "BULLISH":
        direction = "LONG"
    elif h4["trend"] == "BEARISH":
        direction = "SHORT"
    else:
        direction = "NO_TRADE"

    if direction == "NO_TRADE":
        score = 35.0
        positives: list[str] = []
        warnings = ["4H no tiene dirección suficientemente clara"]
        style = "NO_TRADE"
    else:
        score, positives, warnings, style = _technical_score(direction, daily, h4, rsi15, atr_percentile, volume_ratio)

    if score < 60:
        verdict = "NO_TRADE"
    elif score < 72:
        verdict = "VIGILAR"
    elif score < 82:
        verdict = "SETUP_BUENO"
    else:
        verdict = "SETUP_FUERTE"

    if direction == "LONG":
        why_now = (
            f"4H conserva sesgo alcista. RSI 15m está en {rsi15:.1f} y el ATR 15m se encuentra "
            f"en percentil {atr_percentile:.0f}; se busca continuación solo si la entrada protegida de ExplodeX permanece válida."
        )
        debate = "¿La ruptura tiene aceptación real o primero barrerá liquidez de longs tardíos?"
    elif direction == "SHORT":
        why_now = (
            f"4H conserva sesgo bajista. RSI 15m está en {rsi15:.1f} y el ATR 15m se encuentra "
            f"en percentil {atr_percentile:.0f}; se busca continuación solo si el rechazo/pérdida de estructura se confirma."
        )
        debate = "¿La caída tiene continuación real o es solo un barrido antes de recuperar estructura?"
    else:
        why_now = "4H está mezclado; no hay una tesis direccional limpia que justifique entrada ahora."
        debate = "¿Qué lado logra primero estructura 4H clara y aceptación con volumen?"

    return {
        "symbol": symbol,
        "available": True,
        "source": binance_client.active_source,
        "direction": direction,
        "technical_confidence_score": round(score, 1),
        "score_is_probability": False,
        "verdict": verdict,
        "setup_style": style,
        "daily": daily,
        "h4": h4,
        "m15": {
            "rsi14": round(rsi15, 2),
            "atr14": round(atr15, 12),
            "atr_pct": round(atr15_pct, 3),
            "atr_percentile_60": round(atr_percentile, 1),
            "volume_ratio_20": round(volume_ratio, 2),
        },
        "positives": positives,
        "warnings": warnings,
        "why_now": why_now,
        "debate": debate,
        "interpretation": {
            "daily_role": "contexto / jaula / tendencia mayor",
            "h4_role": "dirección principal de la tesis",
            "m15_role": "timing de entrada, RSI, ATR y volumen",
        },
        "note": "La confianza técnica es un score de alineación, no una probabilidad calibrada de ganancia.",
    }
