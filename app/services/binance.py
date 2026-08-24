from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import settings


class BinancePublicClient:
    def __init__(self) -> None:
        primary = settings.binance_futures_base_url.rstrip("/")
        futures_candidates = [
            primary,
            "https://fapi.binance.com",
            "https://fapi1.binance.com",
            "https://fapi2.binance.com",
            "https://fapi3.binance.com",
        ]
        self.futures_bases = list(dict.fromkeys(futures_candidates))
        self.spot_bases = [
            "https://api.binance.com",
            "https://api-gcp.binance.com",
            "https://api1.binance.com",
            "https://api2.binance.com",
            "https://api3.binance.com",
            "https://api4.binance.com",
            "https://data-api.binance.vision",
        ]
        self._preferred_futures = 0
        self._preferred_spot = 0
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "ExplodeX/0.7 market-data client",
        }

    @staticmethod
    def _rotated(values: list[str], preferred: int) -> list[tuple[int, str]]:
        if not values:
            return []
        preferred = max(0, min(preferred, len(values) - 1))
        order = list(range(preferred, len(values))) + list(range(0, preferred))
        return [(index, values[index]) for index in order]

    async def _request_with_failover(
        self,
        *,
        bases: list[str],
        preferred_attr: str,
        path: str,
        params: dict[str, Any] | None,
        timeout: float,
    ) -> Any:
        preferred = int(getattr(self, preferred_attr))
        errors: list[str] = []

        async with httpx.AsyncClient(timeout=timeout, headers=self.headers, follow_redirects=True) as client:
            for index, base in self._rotated(bases, preferred):
                url = f"{base}{path}"
                try:
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    setattr(self, preferred_attr, index)
                    return response.json()
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    detail = f"{base}"
                    if status is not None:
                        detail += f" HTTP {status}"
                    detail += f" {type(exc).__name__}: {str(exc)[:140]}"
                    errors.append(detail)

        joined = " | ".join(errors[-5:])
        raise RuntimeError(f"Binance endpoints unavailable for {path}. {joined}")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request_with_failover(
            bases=self.futures_bases,
            preferred_attr="_preferred_futures",
            path=path,
            params=params,
            timeout=12.0,
        )

    async def _get_spot(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request_with_failover(
            bases=self.spot_bases,
            preferred_attr="_preferred_spot",
            path=path,
            params=params,
            timeout=10.0,
        )

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

        klines, oi, oi_hist, taker, premium, long_short = await asyncio.gather(
            self.klines(symbol, "5m", 120),
            self.open_interest(symbol),
            self.open_interest_history(symbol, "5m", 12),
            self.taker_ratio(symbol, "5m", 8),
            self.premium_index(symbol),
            self.long_short_ratio(symbol, "5m", 8),
        )

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
