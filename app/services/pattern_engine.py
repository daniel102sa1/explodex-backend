from __future__ import annotations

from typing import Any


VERSION = "pattern_engine_v1"


def _closes(candles: list[Any]) -> list[float]:
    out: list[float] = []
    for c in candles or []:
        try:
            if isinstance(c, dict):
                out.append(float(c.get("close")))
            elif isinstance(c, (list, tuple)):
                out.append(float(c[4]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _highs(candles: list[Any]) -> list[float]:
    out: list[float] = []
    for c in candles or []:
        try:
            out.append(float(c.get("high")) if isinstance(c, dict) else float(c[2]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _lows(candles: list[Any]) -> list[float]:
    out: list[float] = []
    for c in candles or []:
        try:
            out.append(float(c.get("low")) if isinstance(c, dict) else float(c[3]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def detect_patterns(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Technical pattern sensor for Terra.

    This is intentionally a sensor, not an entry trigger. Terra combines this
    with order flow, derivatives, fundamentals and risk analysis.
    """
    candles = snapshot.get("klines_1h") or snapshot.get("klines") or []
    closes = _closes(candles)
    highs = _highs(candles)
    lows = _lows(candles)

    result = {
        "version": VERSION,
        "patterns": [],
        "score": 0,
        "signals": [],
    }

    if len(closes) < 20:
        return result

    recent_highs = highs[-20:]
    recent_lows = lows[-20:]

    high_range = max(recent_highs) - min(recent_highs)
    low_slope = recent_lows[-1] - recent_lows[0]

    # Basic ascending triangle detector: repeated highs + rising lows.
    if high_range > 0 and low_slope > 0:
        compression = 1 - min(high_range / max(closes[-20:]), 1)
        if compression > 0:
            result["patterns"].append("ASCENDING_TRIANGLE")
            result["score"] += 15
            result["signals"].append("higher_lows_with_resistance_compression")

    # Momentum continuation sensor.
    if closes[-1] > sum(closes[-10:]) / 10:
        result["patterns"].append("SHORT_TERM_UPTREND")
        result["score"] += 10

    result["score"] = min(result["score"], 100)
    return result
