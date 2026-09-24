from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.config import settings

VERSION = "fundamental_intelligence_v1_coingecko_shadow"

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = asyncio.Lock()


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _base_asset(symbol: str) -> str:
    symbol = str(symbol or "").upper().strip()
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "ExplodeX/1.0 fundamental research",
    }
    if settings.coingecko_demo_api_key:
        headers["x-cg-demo-api-key"] = settings.coingecko_demo_api_key
    return headers


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    base = settings.coingecko_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=settings.coingecko_timeout_seconds, headers=_headers(), follow_redirects=True) as client:
        response = await client.get(f"{base}{path}", params=params)
        response.raise_for_status()
        return response.json()


async def _resolve_coin(base_asset: str) -> dict[str, Any] | None:
    payload = await _get("/search", {"query": base_asset})
    rows = payload.get("coins") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    exact = [
        dict(row)
        for row in rows
        if str(row.get("symbol") or "").upper() == base_asset.upper()
    ]
    if not exact:
        return None

    def rank(row: dict[str, Any]) -> tuple[int, float]:
        market_rank = row.get("market_cap_rank")
        try:
            mr = int(market_rank) if market_rank is not None else 10_000_000
        except (TypeError, ValueError):
            mr = 10_000_000
        try:
            score = -float(row.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        return mr, score

    exact.sort(key=rank)
    return exact[0]


def _risk_layer(market: dict[str, Any]) -> dict[str, Any]:
    market_cap = _f(market.get("market_cap"))
    fdv = _f(market.get("fully_diluted_valuation"))
    volume = _f(market.get("total_volume"))
    circulating = _f(market.get("circulating_supply"))
    total_supply = _f(market.get("total_supply"))
    max_supply = _f(market.get("max_supply"))

    dilution_ratio = fdv / market_cap if market_cap > 0 and fdv > 0 else None
    float_ratio = circulating / total_supply if circulating > 0 and total_supply > 0 else None
    max_float_ratio = circulating / max_supply if circulating > 0 and max_supply > 0 else None
    turnover = volume / market_cap if market_cap > 0 and volume >= 0 else None

    risk = 0.0
    flags: list[str] = []

    if market_cap > 0 and market_cap < 50_000_000:
        risk += 18
        flags.append("microcap_market_cap")
    elif market_cap > 0 and market_cap < 250_000_000:
        risk += 8
        flags.append("smallcap_market_cap")

    if dilution_ratio is not None and dilution_ratio >= 3.0:
        risk += 28
        flags.append("very_high_fdv_to_market_cap")
    elif dilution_ratio is not None and dilution_ratio >= 2.0:
        risk += 16
        flags.append("high_fdv_to_market_cap")

    if float_ratio is not None and float_ratio < 0.35:
        risk += 24
        flags.append("low_circulating_to_total_supply")
    elif float_ratio is not None and float_ratio < 0.55:
        risk += 10
        flags.append("moderate_float_constraint")

    if turnover is not None and turnover < 0.01:
        risk += 15
        flags.append("low_turnover_vs_market_cap")

    risk = max(0.0, min(100.0, risk))
    if risk >= 60:
        state = "HIGH_TOKENOMICS_LIQUIDITY_RISK"
        risk_multiplier = 0.65
    elif risk >= 35:
        state = "CAUTION"
        risk_multiplier = 0.80
    elif risk >= 15:
        state = "WATCH"
        risk_multiplier = 0.92
    else:
        state = "NEUTRAL"
        risk_multiplier = 1.0

    return {
        "risk_score": round(risk, 1),
        "state": state,
        "risk_multiplier_cap": risk_multiplier,
        "flags": flags,
        "fdv_to_market_cap": round(dilution_ratio, 4) if dilution_ratio is not None else None,
        "circulating_to_total_supply": round(float_ratio, 4) if float_ratio is not None else None,
        "circulating_to_max_supply": round(max_float_ratio, 4) if max_float_ratio is not None else None,
        "volume_to_market_cap_24h": round(turnover, 4) if turnover is not None else None,
    }


async def fundamental_context_for_symbol(symbol: str) -> dict[str, Any]:
    """Market/tokenomics fundamentals for PAPER research.

    This deliberately does not pretend CoinGecko market metadata is complete due
    diligence. It provides market-cap/supply/dilution/liquidity context and names
    the missing on-chain/security/governance layers explicitly.
    """
    symbol = str(symbol or "").upper()
    base_asset = _base_asset(symbol)
    if not settings.fundamentals_enabled:
        return {
            "version": VERSION,
            "enabled": False,
            "symbol": symbol,
            "available": False,
            "can_create_entry": False,
            "can_raise_leverage": False,
        }

    now = time.monotonic()
    cached = _CACHE.get(symbol)
    if cached and now < cached[0]:
        return cached[1]

    async with _CACHE_LOCK:
        now = time.monotonic()
        cached = _CACHE.get(symbol)
        if cached and now < cached[0]:
            return cached[1]

        try:
            coin = await _resolve_coin(base_asset)
            if not coin:
                raise RuntimeError(f"CoinGecko symbol mapping not found for {base_asset}")
            coin_id = str(coin.get("id") or "")
            payload = await _get(
                "/coins/markets",
                {
                    "vs_currency": "usd",
                    "ids": coin_id,
                    "price_change_percentage": "1h,24h,7d",
                    "sparkline": "false",
                },
            )
            rows = payload if isinstance(payload, list) else []
            if not rows:
                raise RuntimeError(f"CoinGecko market data unavailable for {coin_id}")
            market = dict(rows[0])
            risk = _risk_layer(market)
            value = {
                "version": VERSION,
                "enabled": True,
                "available": True,
                "paper_only": True,
                "shadow_only": True,
                "source": "COINGECKO",
                "symbol": symbol,
                "base_asset": base_asset,
                "asset": {
                    "id": coin_id,
                    "name": market.get("name") or coin.get("name"),
                    "market_cap_rank": market.get("market_cap_rank") or coin.get("market_cap_rank"),
                },
                "market": {
                    "market_cap_usd": _f(market.get("market_cap")),
                    "fully_diluted_valuation_usd": _f(market.get("fully_diluted_valuation")),
                    "volume_24h_usd": _f(market.get("total_volume")),
                    "high_24h": _f(market.get("high_24h")),
                    "low_24h": _f(market.get("low_24h")),
                    "price_change_1h_pct": market.get("price_change_percentage_1h_in_currency"),
                    "price_change_24h_pct": market.get("price_change_percentage_24h"),
                    "price_change_7d_pct": market.get("price_change_percentage_7d_in_currency"),
                },
                "tokenomics": {
                    "circulating_supply": market.get("circulating_supply"),
                    "total_supply": market.get("total_supply"),
                    "max_supply": market.get("max_supply"),
                    "fdv_to_market_cap": risk.get("fdv_to_market_cap"),
                    "circulating_to_total_supply": risk.get("circulating_to_total_supply"),
                    "circulating_to_max_supply": risk.get("circulating_to_max_supply"),
                },
                "liquidity_proxy": {
                    "volume_to_market_cap_24h": risk.get("volume_to_market_cap_24h"),
                    "note": "This is not executable order-book depth; Binance/OKX depth remains the execution source.",
                },
                "risk": risk,
                "missing_layers": [
                    "point_in_time_unlock_schedule",
                    "wallet_concentration",
                    "exchange_inflows_outflows",
                    "whale_transfers",
                    "protocol_revenue_and_value_capture",
                    "security_admin_keys_bridges",
                    "governance_control",
                ],
                "can_create_entry": False,
                "can_change_direction": False,
                "can_raise_leverage": False,
                "can_reduce_risk": True,
                "note": (
                    "Fundamental/tokenomics context only. CoinGecko is used for market-cap/supply/dilution context; "
                    "execution still uses exchange-native data and missing on-chain/unlock/security layers are not fabricated."
                ),
            }
            _CACHE[symbol] = (time.monotonic() + settings.fundamentals_cache_ttl_seconds, value)
            return value
        except Exception as exc:
            value = {
                "version": VERSION,
                "enabled": True,
                "available": False,
                "paper_only": True,
                "shadow_only": True,
                "symbol": symbol,
                "base_asset": base_asset,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "risk": {
                    "risk_score": None,
                    "state": "UNAVAILABLE",
                    "risk_multiplier_cap": 1.0,
                    "flags": ["fundamental_data_unavailable"],
                },
                "can_create_entry": False,
                "can_change_direction": False,
                "can_raise_leverage": False,
                "can_reduce_risk": False,
            }
            _CACHE[symbol] = (time.monotonic() + 120.0, value)
            return value
