from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import settings


class BinancePublicClient:
    def __init__(self) -> None:
        self.base_url = settings.binance_futures_base_url.rstrip("/")
        self.spot_base_url = "https://api.binance.com"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            return response.json()

    async def _get_spot(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self.spot_base_url}{path}", params=params)
            response.raise_for_status()
            return response.json()

    async def exchange_info(self) -> dict[str, Any]:
        return await self._get("/fapi/v1/exchangeInfo")

    async def ticker_24h(self) -> list[dict[str, Any]]:
        return await self._get("/fapi/v1/ticker/24hr")

    async def price(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/ticker/price", {"symbol": symbol.upper()})

    async def klines(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[list[Any]]:
        return await self._get(
            "/fapi/v1/klines",
            {"symbol": symbol.upper(), "interval": interval, "limit": limit},
        )

    async def order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        safe_limit = limit if limit in {5, 10, 20, 50, 100, 500, 1000} else 20
        return await self._get(
            "/fapi/v1/depth",
            {"symbol": symbol.upper(), "limit": safe_limit},
        )

    async def agg_trades(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        return await self._get(
            "/fapi/v1/aggTrades",
            {"symbol": symbol.upper(), "limit": max(20, min(limit, 1000))},
        )

    async def spot_agg_trades(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        return await self._get_spot(
            "/api/v3/aggTrades",
            {"symbol": symbol.upper(), "limit": max(20, min(limit, 1000))},
        )

    async def open_interest(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/openInterest", {"symbol": symbol.upper()})

    async def open_interest_history(self, symbol: str, period: str = "5m", limit: int = 12) -> list[dict[str, Any]]:
        return await self._get(
            "/futures/data/openInterestHist",
            {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
        )

    async def taker_ratio(self, symbol: str, period: str = "5m", limit: int = 8) -> list[dict[str, Any]]:
        return await self._get(
            "/futures/data/takerlongshortRatio",
            {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
        )

    async def premium_index(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/premiumIndex", {"symbol": symbol.upper()})

    async def long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 8) -> list[dict[str, Any]]:
        return await self._get(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
        )

    async def top_long_short_account_ratio(self, symbol: str, period: str = "5m", limit: int = 8) -> list[dict[str, Any]]:
        return await self._get(
            "/futures/data/topLongShortAccountRatio",
            {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
        )

    async def top_long_short_position_ratio(self, symbol: str, period: str = "5m", limit: int = 8) -> list[dict[str, Any]]:
        return await self._get(
            "/futures/data/topLongShortPositionRatio",
            {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
        )

    @staticmethod
    def _optional_value(value: Any, fallback: Any) -> Any:
        return fallback if isinstance(value, Exception) else value

    async def deep_snapshot(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()

        # Core inputs are required for a valid signal.
        klines, oi, oi_hist, taker, premium, long_short = await asyncio.gather(
            self.klines(symbol, "5m", 120),
            self.open_interest(symbol),
            self.open_interest_history(symbol, "5m", 12),
            self.taker_ratio(symbol, "5m", 8),
            self.premium_index(symbol),
            self.long_short_ratio(symbol, "5m", 8),
        )

        # Enrichment is intentionally optional: one provider endpoint must not kill a scan.
        extras = await asyncio.gather(
            self.klines(symbol, "15m", 80),
            self.klines(symbol, "1h", 80),
            self.order_book(symbol, 20),
            self.agg_trades(symbol, 250),
            self.top_long_short_account_ratio(symbol, "5m", 8),
            self.top_long_short_position_ratio(symbol, "5m", 8),
            self.spot_agg_trades(symbol, 250),
            return_exceptions=True,
        )
        klines_15m, klines_1h, order_book, agg_trades, top_accounts, top_positions, spot_agg_trades = extras

        return {
            "symbol": symbol,
            "klines": klines,
            "klines_15m": self._optional_value(klines_15m, []),
            "klines_1h": self._optional_value(klines_1h, []),
            "open_interest": oi,
            "open_interest_history": oi_hist,
            "taker": taker,
            "premium": premium,
            "long_short": long_short,
            "order_book": self._optional_value(order_book, {}),
            "agg_trades": self._optional_value(agg_trades, []),
            "top_long_short_accounts": self._optional_value(top_accounts, []),
            "top_long_short_positions": self._optional_value(top_positions, []),
            "spot_agg_trades": self._optional_value(spot_agg_trades, []),
        }


binance_client = BinancePublicClient()
