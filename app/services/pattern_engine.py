"""
Terra Pattern Engine V1
Sensor técnico para ExplodeX.
No ejecuta operaciones; entrega evidencia estructural al cerebro Terra.
"""
from __future__ import annotations

from typing import Any


def _closes(candles: list[dict[str, Any]]) -> list[float]:
    return [float(c.get("close", c.get("c", 0)) or 0) for c in candles]


def detect_patterns(candles: list[dict[str, Any]]) -> dict[str, Any]:
    closes = _closes(candles[-80:])
    if len(closes) < 20:
        return {"patterns": [], "score": 0, "signals": []}

    patterns = []
    signals = []
    score = 0

    recent = closes[-20:]
    first_half = recent[:10]
    second_half = recent[10:]

    # Mínimos crecientes: base de triángulos ascendentes / acumulación.
    if min(second_half) >= min(first_half) * 0.995:
        patterns.append("HIGHER_LOWS")
        signals.append("buyers_defending_higher_levels")
        score += 10

    # Compresión de rango.
    old_range = max(first_half) - min(first_half)
    new_range = max(second_half) - min(second_half)
    if old_range > 0 and new_range < old_range * 0.75:
        patterns.append("PRICE_COMPRESSION")
        signals.append("volatility_contracting")
        score += 10

    # Tendencia corta.
    if second_half[-1] > second_half[0]:
        patterns.append("SHORT_TERM_UPTREND")
        signals.append("short_term_momentum_positive")
        score += 5
    elif second_half[-1] < second_half[0]:
        patterns.append("SHORT_TERM_DOWNTREND")
        signals.append("short_term_momentum_negative")
        score += 5

    return {
        "patterns": patterns,
        "score": min(score, 100),
        "signals": signals,
        "engine": "Terra Pattern Engine V1",
    }


def analyze_pattern_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Recibe snapshot multi temporal y devuelve evidencia para Terra."""
    return {
        "1m": detect_patterns(snapshot.get("klines_1m", [])),
        "15m": detect_patterns(snapshot.get("klines_15m", [])),
        "1h": detect_patterns(snapshot.get("klines_1h", [])),
        "4h": detect_patterns(snapshot.get("klines_4h", [])),
        "1d": detect_patterns(snapshot.get("klines_1d", [])),
    }
