from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.config import settings


class BinancePublicClient:
    """Primary Binance Futures market-data client with a transparent public fallback.

    Railway can receive HTTP 451 from Binance depending on the egress region. In that
    case we stop retrying Binance for a cooldown period and use OKX public USDT swap
    market data instead. The fallback is clearly exposed through ``active_source`` so
    the UI never pretends fallback data is Binance data.
    """

    def __init__(self) -> None:
        primary = settings.binance_futures_base_url.rstrip("/")
        self.futures_bases = list(dict.fromkeys([
            primary,
            "https://fapi.binance.com",
            "https://fapi1.binance.com",
            "https://fapi2.binance.com",
            "https://fapi3.binance.com",
        ]))
        self.spot_bases = [
            "https://api.binance.com",
            "https://api-gcp.binance.com",
            "https://api1.binance.com",
            "https://api2.binance.com",
            "https://api3.binance.com",
            "https://api4.binance.com",
            "https://data-api.binance.vision",
        ]
        self.okx_bases = [
            "https://www.okx.com",
            "https://openapi.okx.com",
        ]
        self._preferred_futures = 0
        self._preferred_spot = 0
        self._preferred_okx = 0
        self._binance_blocked_until = 0.0
        self.active_source = "BINANCE_FUTURES"
        self.last_primary_error: str | None = None
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "ExplodeX/0.8 market-data client",
        }

    @staticmethod
    def _rotated(values: list[str], preferred: int) -> list[tuple[int, str]]:
        if not values:
            return []
        preferred = max(0, min(preferred, len(values) - 1))
        order = list(range(preferred, len(values))) + list(range(0, preferred))
        return [(index, values[index]) for index in order]

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _okx_swap_id(symbol: str) -> str:
        symbol = symbol.upper()
        if not symbol.endswith("USDT"):
            raise ValueError(f"Unsupported fallback symbol: {symbol}")
        return f"{symbol[:-4]}-USDT-SWAP"

    @staticmethod
    def _okx_spot_id(symbol: str) -> str:
        symbol = symbol.upper()
        if not symbol.endswith("USDT"):
            raise ValueError(f"Unsupported fallback symbol: {symbol}")
        return f"{symbol[:-4]}-USDT"

    @staticmethod
    def _okx_symbol(inst_id: str) -> str | None:
        if not inst_id.endswith("-USDT-SWAP"):
            return None
        return inst_id.removesuffix("-USDT-SWAP").replace("-", "") + "USDT"

    def _binance_available(self) -> bool:
        return time.monotonic() >= self._binance_blocked_until

    def _mark_binance_blocked(self, error: Exception) -> None:
        self.last_primary_error = str(error)[:1000]
        self._binance_blocked_until = time.monotonic() + 1800
        self.active_source = "OKX_FALLBACK"

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
                try:
                    response = await client.get(f"{base}{path}", params=params)
                    if response.status_code == 451:
                        raise RuntimeError(f"HTTP 451 regional restriction from {base}")
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except Exception as exc:
                        raise RuntimeError(
                            f"Non-JSON response from {base} ({response.status_code}): {response.text[:120]}"
                        ) from exc
                    setattr(self, preferred_attr, index)
                    return payload
                except Exception as exc:
                    errors.append(f"{base}: {type(exc).__name__}: {str(exc)[:180]}")

        raise RuntimeError(f"All endpoints failed for {path}. {' | '.join(errors[-5:])}")

    async def _binance_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request_with_failover(
            bases=self.futures_bases,
            preferred_attr="_preferred_futures",
            path=path,
            params=params,
            timeout=12.0,
        )

    async def _binance_spot_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request_with_failover(
            bases=self.spot_bases,
            preferred_attr="_preferred_spot",
            path=path,
            params=params,
            timeout=10.0,
        )

    async def _okx_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = await self._request_with_failover(
            bases=self.okx_bases,
            preferred_attr="_preferred_okx",
            path=path,
            params=params,
            timeout=12.0,
        )
        if not isinstance(payload, dict) or str(payload.get("code", "0")) != "0":
            raise RuntimeError(f"OKX error for {path}: {payload}")
        self.active_source = "OKX_FALLBACK"
        return payload

    async def _prefer_binance(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._binance_available():
            raise RuntimeError("Binance temporarily disabled after regional/provider failure")
        try:
            value = await self._binance_get(path, params)
            self.active_source = "BINANCE_FUTURES"
            return value
        except Exception as exc:
            self._mark_binance_blocked(exc)
            raise

    async def exchange_info(self) -> dict[str, Any]:
        if self._binance_available():
            try:
                return await self._prefer_binance("/fapi/v1/exchangeInfo")
            except Exception:
                pass
        payload = await self._okx_get("/api/v5/public/instruments", {"instType": "SWAP"})
        return {"symbols": payload.get("data", []), "source": "OKX_FALLBACK"}

    async def ticker_24h(self) -> list[dict[str, Any]]:
        if self._binance_available():
            try:
                return await self._prefer_binance("/fapi/v1/ticker/24hr")
            except Exception:
                pass

        payload = await self._okx_get("/api/v5/market/tickers", {"instType": "SWAP"})
        normalized: list[dict[str, Any]] = []
        for item in payload.get("data", []):
            symbol = self._okx_symbol(str(item.get("instId", "")))
            if not symbol:
                continue
            last = self._safe_float(item.get("last"))
            open24h = self._safe_float(item.get("open24h"))
            if last <= 0:
                continue
            change = ((last - open24h) / open24h * 100) if open24h > 0 else 0.0
            vol_ccy = self._safe_float(item.get("volCcy24h"))
            quote_volume = abs(vol_ccy * last)
            if quote_volume <= 0:
                quote_volume = abs(self._safe_float(item.get("vol24h")) * last)
            normalized.append({
                "symbol": symbol,
                "lastPrice": str(last),
                "priceChangePercent": str(change),
                "quoteVolume": str(quote_volume),
                "volume": str(vol_ccy),
                "source": "OKX_FALLBACK",
            })
        return normalized

    async def price(self, symbol: str) -> dict[str, Any]:
        if self._binance_available():
            try:
                return await self._prefer_binance("/fapi/v1/ticker/price", {"symbol": symbol.upper()})
            except Exception:
                pass
        payload = await self._okx_get("/api/v5/market/ticker", {"instId": self._okx_swap_id(symbol)})
        rows = payload.get("data", [])
        if not rows:
            raise RuntimeError(f"OKX ticker unavailable for {symbol}")
        return {"symbol": symbol.upper(), "price": rows[0].get("last"), "source": "OKX_FALLBACK"}

    async def klines(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[list[Any]]:
        if self._binance_available():
            try:
                return await self._prefer_binance(
                    "/fapi/v1/klines",
                    {"symbol": symbol.upper(), "interval": interval, "limit": limit},
                )
            except Exception:
                pass

        bar_map = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "2h": "2H", "4h": "4H", "1d": "1D"}
        bar = bar_map.get(interval, interval)
        payload = await self._okx_get(
            "/api/v5/market/candles",
            {"instId": self._okx_swap_id(symbol), "bar": bar, "limit": min(max(limit, 1), 300)},
        )
        rows = list(reversed(payload.get("data", [])))
        output: list[list[Any]] = []
        for row in rows:
            if len(row) < 5:
                continue
            ts = int(row[0])
            quote_volume = row[7] if len(row) > 7 else (row[6] if len(row) > 6 else "0")
            output.append([
                ts,
                row[1], row[2], row[3], row[4],
                row[5] if len(row) > 5 else "0",
                ts + 1,
                quote_volume,
                0, 0, 0, 0,
            ])
        return output

    async def order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        if self._binance_available():
            try:
                safe_limit = limit if limit in {5, 10, 20, 50, 100, 500, 1000} else 20
                return await self._prefer_binance("/fapi/v1/depth", {"symbol": symbol.upper(), "limit": safe_limit})
            except Exception:
                pass
        payload = await self._okx_get(
            "/api/v5/market/books",
            {"instId": self._okx_swap_id(symbol), "sz": min(max(limit, 1), 400)},
        )
        rows = payload.get("data", [])
        row = rows[0] if rows else {}
        return {
            "bids": [[x[0], x[1]] for x in row.get("bids", []) if len(x) >= 2],
            "asks": [[x[0], x[1]] for x in row.get("asks", []) if len(x) >= 2],
            "source": "OKX_FALLBACK",
        }

    async def agg_trades(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        if self._binance_available():
            try:
                return await self._prefer_binance(
                    "/fapi/v1/aggTrades",
                    {"symbol": symbol.upper(), "limit": max(20, min(limit, 1000))},
                )
            except Exception:
                pass
        payload = await self._okx_get(
            "/api/v5/market/trades",
            {"instId": self._okx_swap_id(symbol), "limit": min(max(limit, 20), 500)},
        )
        return [
            {
                "p": item.get("px", "0"),
                "q": item.get("sz", "0"),
                "m": str(item.get("side", "")).lower() == "sell",
                "T": item.get("ts"),
            }
            for item in payload.get("data", [])
        ]

    async def spot_agg_trades(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        if self._binance_available():
            try:
                return await self._binance_spot_get(
                    "/api/v3/aggTrades",
                    {"symbol": symbol.upper(), "limit": max(20, min(limit, 1000))},
                )
            except Exception:
                pass
        try:
            payload = await self._okx_get(
                "/api/v5/market/trades",
                {"instId": self._okx_spot_id(symbol), "limit": min(max(limit, 20), 500)},
            )
        except Exception:
            return []
        return [
            {
                "p": item.get("px", "0"),
                "q": item.get("sz", "0"),
                "m": str(item.get("side", "")).lower() == "sell",
                "T": item.get("ts"),
            }
            for item in payload.get("data", [])
        ]

    async def open_interest(self, symbol: str) -> dict[str, Any]:
        if self._binance_available():
            try:
                return await self._prefer_binance("/fapi/v1/openInterest", {"symbol": symbol.upper()})
            except Exception:
                pass
        payload = await self._okx_get(
            "/api/v5/public/open-interest",
            {"instType": "SWAP", "instId": self._okx_swap_id(symbol)},
        )
        rows = payload.get("data", [])
        value = 0.0
        if rows:
            value = self._safe_float(rows[0].get("oiCcy")) or self._safe_float(rows[0].get("oi"))
        return {"symbol": symbol.upper(), "openInterest": str(value), "source": "OKX_FALLBACK"}

    async def open_interest_history(self, symbol: str, period: str = "5m", limit: int = 12) -> list[dict[str, Any]]:
        if self._binance_available():
            try:
                return await self._prefer_binance(
                    "/futures/data/openInterestHist",
                    {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
                )
            except Exception:
                pass
        # OKX's basic public OI endpoint is current-state. Do not fabricate history.
        return []

    async def taker_ratio(self, symbol: str, period: str = "5m", limit: int = 8) -> list[dict[str, Any]]:
        if self._binance_available():
            try:
                return await self._prefer_binance(
                    "/futures/data/takerlongshortRatio",
                    {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
                )
            except Exception:
                pass
        trades = await self.agg_trades(symbol, 250)
        buy = 0.0
        sell = 0.0
        for trade in trades:
            notional = self._safe_float(trade.get("p")) * self._safe_float(trade.get("q"))
            if bool(trade.get("m", False)):
                sell += notional
            else:
                buy += notional
        ratio = buy / sell if sell > 0 else (9.99 if buy > 0 else 1.0)
        return [{"buySellRatio": str(ratio), "source": "OKX_FALLBACK"}]

    async def premium_index(self, symbol: str) -> dict[str, Any]:
        if self._binance_available():
            try:
                return await self._prefer_binance("/fapi/v1/premiumIndex", {"symbol": symbol.upper()})
            except Exception:
                pass
        payload = await self._okx_get("/api/v5/public/funding-rate", {"instId": self._okx_swap_id(symbol)})
        rows = payload.get("data", [])
        funding = rows[0].get("fundingRate", "0") if rows else "0"
        return {"symbol": symbol.upper(), "lastFundingRate": funding, "source": "OKX_FALLBACK"}

    async def long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 8) -> list[dict[str, Any]]:
        if self._binance_available():
            try:
                return await self._prefer_binance(
                    "/futures/data/globalLongShortAccountRatio",
                    {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
                )
            except Exception:
                pass
        return []

    async def top_long_short_account_ratio(self, symbol: str, period: str = "5m", limit: int = 8) -> list[dict[str, Any]]:
        if self._binance_available():
            try:
                return await self._prefer_binance(
                    "/futures/data/topLongShortAccountRatio",
                    {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
                )
            except Exception:
                pass
        return []

    async def top_long_short_position_ratio(self, symbol: str, period: str = "5m", limit: int = 8) -> list[dict[str, Any]]:
        if self._binance_available():
            try:
                return await self._prefer_binance(
                    "/futures/data/topLongShortPositionRatio",
                    {"symbol": symbol.upper(), "period": period, "limit": max(2, min(limit, 500))},
                )
            except Exception:
                pass
        return []

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
            "source": self.active_source,
            "provider_warning": self.last_primary_error if self.active_source != "BINANCE_FUTURES" else None,
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
