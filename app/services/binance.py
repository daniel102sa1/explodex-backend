from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import settings


class BinancePublicClient:
    def __init__(self) -> None:
        self.base_url = settings.binance_futures_base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            return response.json()

    async def ticker_24h(self) -> list[dict[str, Any]]:
        return await self._get("/fapi/v1/ticker/24hr")

    async def price(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/ticker/price", {"symbol": symbol.upper()})

    async def klines(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[list[Any]]:
        return await self._get(
            "/fapi/v1/klines",
            {"symbol": symbol.upper(), "interval": interval, "limit": limit},
        )

    async def open_interest(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/openInterest", {"symbol": symbol.upper()})

    async def open_interest_history(self, symbol: str, period: str = "5m", limit: int = 12) -> list[dict[str, Any]]:
        return await self._get(
            "/futures/data/openInterestHist",
            {"symbol": symbol.upper(), "period": period, "limit": limit},
        )

    async def taker_ratio(self, symbol: str, period: str = "5m", limit: int = 8) -> list[dict[str, Any]]:
        return await self._get(
            "/futures/data/takerlongshortRatio",
            {"symbol": symbol.upper(), "period": period, "limit": limit},
        )

    async def premium_index(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/premiumIndex", {"symbol": symbol.upper()})

    async def long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 5) -> list[dict[str, Any]]:
        return await self._get(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol.upper(), "period": period, "limit": limit},
        )

    async def deep_snapshot(self, symbol: str) -> dict[str, Any]:
        klines, oi, oi_hist, taker, premium, long_short = await asyncio.gather(
            self.klines(symbol),
            self.open_interest(symbol),
            self.open_interest_history(symbol),
            self.taker_ratio(symbol),
            self.premium_index(symbol),
            self.long_short_ratio(symbol),
        )
        return {
            "symbol": symbol.upper(),
            "klines": klines,
            "open_interest": oi,
            "open_interest_history": oi_hist,
            "taker": taker,
            "premium": premium,
            "long_short": long_short,
        }


binance_client = BinancePublicClient()
