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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _trend_from_klines(klines: list[list[Any]]) -> dict[str, Any]:
    try:
        closes = [_safe_float(k[4]) for k in klines if len(k) > 4]
    except Exception:
        closes = []

    if len(closes) < 13:
        return {"change_15m_pct": 0.0, "change_1h_pct": 0.0, "trend": "NEUTRAL", "available": False}

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
        "available": True,
    }


def _liquid_usdt_tickers(tickers: list[dict[str, Any]], min_quote_volume: float = 5_000_000) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for ticker in tickers:
        try:
            symbol = str(ticker.get("symbol", ""))
            if not symbol.endswith("USDT") or "_" in symbol:
                continue
            quote_volume = _safe_float(ticker.get("quoteVolume"))
            if quote_volume < min_quote_volume:
                continue
            result.append(ticker)
        except Exception:
            continue
    return result


def _neutral_context(errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "degraded": True,
        "regime": "MIXED",
        "liquid_symbols": 0,
        "positive_symbols": 0,
        "negative_symbols": 0,
        "positive_breadth_pct": 0.0,
        "net_breadth_pct": 0.0,
        "median_24h_change_pct": 0.0,
        "strong_up_count": 0,
        "strong_down_count": 0,
        "btc": {"trend": "NEUTRAL", "change_15m_pct": 0.0, "change_1h_pct": 0.0, "available": False},
        "eth": {"trend": "NEUTRAL", "change_15m_pct": 0.0, "change_1h_pct": 0.0, "available": False},
        "risk_on_points": 0,
        "risk_off_points": 0,
        "long_score_adjustment": 0.0,
        "short_score_adjustment": 0.0,
        "errors": errors or [],
        "note": "Market context is temporarily degraded; no directional adjustment is applied.",
    }


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

        results = await asyncio.gather(
            binance_client.ticker_24h(),
            binance_client.klines("BTCUSDT", interval="5m", limit=120),
            binance_client.klines("ETHUSDT", interval="5m", limit=120),
            return_exceptions=True,
        )

        tickers_raw, btc_raw, eth_raw = results
        errors: list[str] = []

        if isinstance(tickers_raw, Exception):
            errors.append(f"tickers: {str(tickers_raw)[:180]}")
            tickers: list[dict[str, Any]] = []
        else:
            tickers = tickers_raw if isinstance(tickers_raw, list) else []

        if isinstance(btc_raw, Exception):
            errors.append(f"BTC: {str(btc_raw)[:180]}")
            btc_klines: list[list[Any]] = []
        else:
            btc_klines = btc_raw if isinstance(btc_raw, list) else []

        if isinstance(eth_raw, Exception):
            errors.append(f"ETH: {str(eth_raw)[:180]}")
            eth_klines: list[list[Any]] = []
        else:
            eth_klines = eth_raw if isinstance(eth_raw, list) else []

        # If Binance is entirely unavailable, return a safe neutral context instead of 502.
        if not tickers and not btc_klines and not eth_klines:
            value = _neutral_context(errors)
            _CACHE["value"] = value
            _CACHE["expires_at"] = time.monotonic() + 30
            return value

        liquid = _liquid_usdt_tickers(tickers)
        changes = [_safe_float(t.get("priceChangePercent")) for t in liquid]
        positive = sum(1 for x in changes if x > 0)
        negative = sum(1 for x in changes if x < 0)
        strong_up = sum(1 for x in changes if x >= 3)
        strong_down = sum(1 for x in changes if x <= -3)
        total = len(changes)

        breadth_pct = ((positive - negative) / total) * 100 if total else 0.0
        positive_pct = (positive / total) * 100 if total else 0.0
        if total:
            ordered = sorted(changes)
            mid = total // 2
            median_proxy = ordered[mid] if total % 2 else mean(ordered[mid - 1:mid + 1])
        else:
            median_proxy = 0.0

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

        if total:
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
            "available": bool(total or btc.get("available") or eth.get("available")),
            "degraded": bool(errors),
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
            "errors": errors,
            "note": "Market context is a filter/adjustment, not a standalone trade signal.",
        }

        _CACHE["value"] = value
        _CACHE["expires_at"] = time.monotonic() + max(30, cache_ttl_seconds)
        return value
