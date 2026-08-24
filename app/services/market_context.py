from __future__ import annotations

import asyncio
import time
from statistics import mean
from typing import Any

from app.services.binance import binance_client


_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": None}
_CACHE_LOCK = asyncio.Lock()


def _pct_change(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return ((b - a) / a) * 100


def _trend_from_klines(klines: list[list[Any]]) -> dict[str, Any]:
    closes = [float(k[4]) for k in klines]
    if len(closes) < 13:
        return {"change_15m_pct": 0.0, "change_1h_pct": 0.0, "trend": "NEUTRAL"}

    change_15m = _pct_change(closes[-4], closes[-1])
    change_1h = _pct_change(closes[-13], closes[-1])

    if change_1h >= 1.5 or change_15m >= 0.8:
        trend = "BULLISH"
    elif change_1h <= -1.5 or change_15m <= -0.8:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    return {
        "change_15m_pct": round(change_15m, 4),
        "change_1h_pct": round(change_1h, 4),
        "trend": trend,
    }


def _liquid_usdt_tickers(tickers: list[dict[str, Any]], min_quote_volume: float = 5_000_000) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for ticker in tickers:
        symbol = str(ticker.get("symbol", ""))
        if not symbol.endswith("USDT") or "_" in symbol:
            continue
        quote_volume = float(ticker.get("quoteVolume", 0) or 0)
        if quote_volume < min_quote_volume:
            continue
        result.append(ticker)
    return result


async def market_context(cache_ttl_seconds: int = 120) -> dict[str, Any]:
    now = time.monotonic()
    cached = _CACHE.get("value")
    if cached is not None and now < float(_CACHE.get("expires_at", 0)):
        return cached

    async with _CACHE_LOCK:
        now = time.monotonic()
        cached = _CACHE.get("value")
        if cached is not None and now < float(_CACHE.get("expires_at", 0)):
            return cached

        tickers, btc_klines, eth_klines = await asyncio.gather(
            binance_client.ticker_24h(),
            binance_client.klines("BTCUSDT", interval="5m", limit=120),
            binance_client.klines("ETHUSDT", interval="5m", limit=120),
        )

        liquid = _liquid_usdt_tickers(tickers)
        changes = [float(t.get("priceChangePercent", 0) or 0) for t in liquid]
        positive = sum(1 for x in changes if x > 0)
        negative = sum(1 for x in changes if x < 0)
        strong_up = sum(1 for x in changes if x >= 3)
        strong_down = sum(1 for x in changes if x <= -3)
        total = len(changes)

        breadth_pct = ((positive - negative) / total) * 100 if total else 0.0
        positive_pct = (positive / total) * 100 if total else 0.0
        median_proxy = mean(sorted(changes)[max(0, total // 2 - 1): total // 2 + 1]) if total else 0.0

        btc = _trend_from_klines(btc_klines)
        eth = _trend_from_klines(eth_klines)

        risk_on_points = 0
        risk_off_points = 0

        if btc["trend"] == "BULLISH":
            risk_on_points += 2
        elif btc["trend"] == "BEARISH":
            risk_off_points += 2

        if eth["trend"] == "BULLISH":
            risk_on_points += 1
        elif eth["trend"] == "BEARISH":
            risk_off_points += 1

        if positive_pct >= 62:
            risk_on_points += 2
        elif positive_pct <= 38:
            risk_off_points += 2

        if breadth_pct >= 25:
            risk_on_points += 1
        elif breadth_pct <= -25:
            risk_off_points += 1

        if strong_up >= max(5, strong_down * 2):
            risk_on_points += 1
        elif strong_down >= max(5, strong_up * 2):
            risk_off_points += 1

        if risk_on_points - risk_off_points >= 3:
            regime = "RISK_ON"
        elif risk_off_points - risk_on_points >= 3:
            regime = "RISK_OFF"
        else:
            regime = "MIXED"

        long_adjustment = max(-6.0, min(6.0, (risk_on_points - risk_off_points) * 1.5))
        short_adjustment = -long_adjustment

        value = {
            "regime": regime,
            "liquid_symbols": total,
            "positive_symbols": positive,
            "negative_symbols": negative,
            "positive_breadth_pct": round(positive_pct, 2),
            "net_breadth_pct": round(breadth_pct, 2),
            "median_24h_change_pct": round(median_proxy, 3),
            "strong_up_count": strong_up,
            "strong_down_count": strong_down,
            "btc": btc,
            "eth": eth,
            "risk_on_points": risk_on_points,
            "risk_off_points": risk_off_points,
            "long_score_adjustment": round(long_adjustment, 2),
            "short_score_adjustment": round(short_adjustment, 2),
            "note": "Market context is a filter/adjustment, not a standalone trade signal.",
        }

        _CACHE["value"] = value
        _CACHE["expires_at"] = time.monotonic() + max(30, cache_ttl_seconds)
        return value
