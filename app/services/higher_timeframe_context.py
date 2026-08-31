from __future__ import annotations

import asyncio
import time
from typing import Any

from app.services.binance import binance_client

VERSION = "higher_timeframe_context_v1"
CACHE_SECONDS = 300.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _frame(rows: list[list[Any]], candles_back: int) -> dict[str, Any]:
    closes = [_f(row[4]) for row in rows if len(row) > 4 and _f(row[4]) > 0]
    if len(closes) < 22:
        return {"trend": "UNKNOWN", "change_pct": 0.0, "ema_gap_pct": 0.0, "available": False}
    current = closes[-1]
    lookback_index = max(0, len(closes) - max(2, candles_back) - 1)
    reference = closes[lookback_index]
    change = ((current - reference) / reference * 100.0) if reference else 0.0
    ema9 = _ema(closes[-40:], 9)
    ema21 = _ema(closes[-60:], 21)
    gap = ((ema9 - ema21) / current * 100.0) if current else 0.0
    if ema9 > ema21 and change > -0.5:
        trend = "BULLISH"
    elif ema9 < ema21 and change < 0.5:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"
    return {
        "available": True,
        "trend": trend,
        "change_pct": round(change, 4),
        "ema_gap_pct": round(gap, 4),
        "last": round(current, 12),
    }


async def higher_timeframe_context(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    now = time.monotonic()
    cached = _cache.get(symbol)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]

    results = await asyncio.gather(
        binance_client.klines(symbol, interval="4h", limit=70),
        binance_client.klines(symbol, interval="6h", limit=70),
        binance_client.klines(symbol, interval="1d", limit=70),
        return_exceptions=True,
    )
    values: list[list[list[Any]]] = []
    errors: list[str] = []
    for name, result in zip(("4h", "6h", "1d"), results):
        if isinstance(result, Exception):
            values.append([])
            errors.append(f"{name}:{type(result).__name__}:{str(result)[:120]}")
        else:
            values.append(result)
    rows4, rows6, rows1d = values
    frames = {
        "4h": _frame(rows4, 3),
        "6h": _frame(rows6, 3),
        "1d": _frame(rows1d, 3),
    }
    bull = sum(1 for item in frames.values() if item.get("trend") == "BULLISH")
    bear = sum(1 for item in frames.values() if item.get("trend") == "BEARISH")
    if bull >= 2:
        bias = "BULLISH"
    elif bear >= 2:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    payload = {
        "version": VERSION,
        "symbol": symbol,
        "frames": frames,
        "bias": bias,
        "bullish_frames": bull,
        "bearish_frames": bear,
        "errors": errors,
        "role": "directional context and continuation evidence; never an automatic entry by itself",
    }
    _cache[symbol] = (now, payload)
    return payload


async def batch_higher_timeframe_context(symbols: list[str], concurrency: int = 4) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(str(s).upper() for s in symbols if s))
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 8)))
    output: dict[str, dict[str, Any]] = {}

    async def one(symbol: str) -> None:
        async with semaphore:
            try:
                output[symbol] = await higher_timeframe_context(symbol)
            except Exception as exc:
                output[symbol] = {
                    "version": VERSION,
                    "symbol": symbol,
                    "bias": "UNKNOWN",
                    "frames": {},
                    "errors": [f"{type(exc).__name__}:{str(exc)[:160]}"],
                }

    await asyncio.gather(*(one(symbol) for symbol in unique))
    return output


def alignment(direction: str, context: dict[str, Any]) -> dict[str, Any]:
    direction = str(direction or "").upper()
    frames = context.get("frames") if isinstance(context.get("frames"), dict) else {}
    wanted = "BULLISH" if direction == "LONG" else "BEARISH"
    opposite = "BEARISH" if direction == "LONG" else "BULLISH"
    aligned = sum(1 for item in frames.values() if isinstance(item, dict) and item.get("trend") == wanted)
    conflicting = sum(1 for item in frames.values() if isinstance(item, dict) and item.get("trend") == opposite)
    return {
        "direction": direction,
        "aligned_frames": aligned,
        "conflicting_frames": conflicting,
        "strong_alignment": aligned >= 2,
        "strong_conflict": conflicting == 3,
        "bias": context.get("bias"),
    }
