from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from statistics import median
from typing import Any

import httpx

from app.config import settings


class CoinGlassError(RuntimeError):
    pass


class CoinGlassRateLimitError(CoinGlassError):
    pass


class CoinGlassClient:
    """Small, defensive CoinGlass v4 client.

    The API key is read only from environment settings and is never returned by
    status/diagnostic endpoints.  The Hobbyist plan is rate limited, therefore
    shared endpoints are cached and the scanner only enriches its best local
    candidates instead of querying every symbol.
    """

    def __init__(self) -> None:
        self.base_url = settings.coinglass_base_url.rstrip("/")
        self._cache: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._request_times: deque[float] = deque()
        self.last_ok_at: str | None = None
        self.last_error: str | None = None
        self.last_http_status: int | None = None
        self.total_requests = 0
        self.total_cache_hits = 0

    @property
    def configured(self) -> bool:
        return bool(settings.coinglass_enabled and settings.coinglass_api_key.strip())

    @staticmethod
    def coin_from_symbol(symbol: str) -> str:
        value = symbol.upper().strip()
        return value[:-4] if value.endswith("USDT") else value

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any] | None) -> str:
        pairs = sorted((params or {}).items())
        return f"{path}?{pairs}"

    def _cached(self, key: str, ttl: int) -> Any | None:
        row = self._cache.get(key)
        if not row:
            return None
        created, value = row
        if time.monotonic() - created > ttl:
            self._cache.pop(key, None)
            return None
        self.total_cache_hits += 1
        return value

    def _consume_rate_slot(self) -> None:
        now = time.monotonic()
        while self._request_times and now - self._request_times[0] >= 60:
            self._request_times.popleft()
        if len(self._request_times) >= settings.coinglass_rate_limit_per_minute:
            raise CoinGlassRateLimitError(
                f"CoinGlass safety rate limit reached ({settings.coinglass_rate_limit_per_minute}/min)"
            )
        self._request_times.append(now)

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        ttl: int | None = None,
    ) -> Any:
        if not self.configured:
            raise CoinGlassError("CoinGlass API is not configured")

        cache_ttl = settings.coinglass_cache_ttl_seconds if ttl is None else ttl
        key = self._cache_key(path, params)
        cached = self._cached(key, cache_ttl)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cached(key, cache_ttl)
            if cached is not None:
                return cached

            self._consume_rate_slot()
            headers = {
                "CG-API-KEY": settings.coinglass_api_key.strip(),
                "Accept": "application/json",
                "User-Agent": "ExplodeX/0.9 CoinGlass confirmation engine",
            }
            try:
                async with httpx.AsyncClient(
                    timeout=settings.coinglass_timeout_seconds,
                    headers=headers,
                    follow_redirects=True,
                ) as client:
                    response = await client.get(f"{self.base_url}{path}", params=params)
                self.total_requests += 1
                self.last_http_status = response.status_code
                response.raise_for_status()
                payload = response.json()
                code = str(payload.get("code", "")) if isinstance(payload, dict) else ""
                if code not in {"0", "200", ""}:
                    raise CoinGlassError(
                        f"CoinGlass API code {code}: {str(payload.get('msg', 'unknown error'))[:250]}"
                    )
                value = payload.get("data") if isinstance(payload, dict) else payload
                self._cache[key] = (time.monotonic(), value)
                self.last_ok_at = datetime.now(timezone.utc).isoformat()
                self.last_error = None
                return value
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:600]}"
                raise CoinGlassError(self.last_error) from exc

    async def status_probe(self) -> dict[str, Any]:
        if not self.configured:
            return self.status()
        try:
            await self.open_interest("BTCUSDT")
        except Exception:
            pass
        return self.status()

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        while self._request_times and now - self._request_times[0] >= 60:
            self._request_times.popleft()
        return {
            "enabled": settings.coinglass_enabled,
            "configured": self.configured,
            "base_url": self.base_url,
            "plan": "HOBBYIST",
            "safe_rate_limit_per_minute": settings.coinglass_rate_limit_per_minute,
            "requests_last_60s": len(self._request_times),
            "total_requests_since_boot": self.total_requests,
            "total_cache_hits_since_boot": self.total_cache_hits,
            "last_ok_at": self.last_ok_at,
            "last_http_status": self.last_http_status,
            "last_error": self.last_error,
        }

    async def open_interest(self, symbol: str) -> dict[str, Any]:
        coin = self.coin_from_symbol(symbol)
        rows = await self._get(
            "/api/futures/open-interest/exchange-list",
            {"symbol": coin},
            ttl=20,
        )
        rows = rows if isinstance(rows, list) else []
        aggregate = next(
            (row for row in rows if str(row.get("exchange", "")).lower() == "all"),
            None,
        )
        if not aggregate:
            aggregate = rows[0] if rows else {}

        exchanges = []
        for row in rows:
            name = str(row.get("exchange", ""))
            if name.lower() == "all":
                continue
            exchanges.append({
                "exchange": name,
                "open_interest_usd": self._float(row.get("open_interest_usd")),
                "change_5m_pct": self._float(row.get("open_interest_change_percent_5m")),
                "change_15m_pct": self._float(row.get("open_interest_change_percent_15m")),
                "change_1h_pct": self._float(row.get("open_interest_change_percent_1h")),
            })
        exchanges.sort(key=lambda x: x["open_interest_usd"], reverse=True)

        return {
            "available": bool(aggregate),
            "symbol": coin,
            "open_interest_usd": self._float(aggregate.get("open_interest_usd")),
            "change_5m_pct": self._float(aggregate.get("open_interest_change_percent_5m")),
            "change_15m_pct": self._float(aggregate.get("open_interest_change_percent_15m")),
            "change_30m_pct": self._float(aggregate.get("open_interest_change_percent_30m")),
            "change_1h_pct": self._float(aggregate.get("open_interest_change_percent_1h")),
            "change_4h_pct": self._float(aggregate.get("open_interest_change_percent_4h")),
            "change_24h_pct": self._float(aggregate.get("open_interest_change_percent_24h")),
            "exchanges": exchanges[:10],
        }

    async def taker_buy_sell(self, symbol: str, range_value: str = "15m") -> dict[str, Any]:
        coin = self.coin_from_symbol(symbol)
        data = await self._get(
            "/api/futures/taker-buy-sell-volume/exchange-list",
            {"symbol": coin, "range": range_value},
            ttl=15,
        )
        data = data if isinstance(data, dict) else {}
        buy_ratio = self._float(data.get("buy_ratio"), 50.0)
        sell_ratio = self._float(data.get("sell_ratio"), 50.0)
        sell_vol = self._float(data.get("sell_vol_usd"))
        buy_vol = self._float(data.get("buy_vol_usd"))
        ratio = buy_vol / sell_vol if sell_vol > 0 else (9.99 if buy_vol > 0 else 1.0)
        return {
            "available": bool(data),
            "symbol": coin,
            "range": range_value,
            "buy_ratio_pct": buy_ratio,
            "sell_ratio_pct": sell_ratio,
            "buy_volume_usd": buy_vol,
            "sell_volume_usd": sell_vol,
            "buy_sell_ratio": ratio,
            "exchanges": data.get("exchange_list", [])[:10],
        }

    async def _funding_map(self) -> dict[str, Any]:
        rows = await self._get(
            "/api/futures/funding-rate/exchange-list",
            None,
            ttl=60,
        )
        rows = rows if isinstance(rows, list) else []
        return {str(row.get("symbol", "")).upper(): row for row in rows}

    async def funding(self, symbol: str) -> dict[str, Any]:
        coin = self.coin_from_symbol(symbol)
        row = (await self._funding_map()).get(coin, {})
        items = row.get("stablecoin_margin_list", []) if isinstance(row, dict) else []
        rates = [self._float(item.get("funding_rate")) for item in items]
        return {
            "available": bool(items),
            "symbol": coin,
            # CoinGlass exposes these values in percentage units in this endpoint.
            "median_rate_pct": median(rates) if rates else 0.0,
            "max_rate_pct": max(rates) if rates else 0.0,
            "min_rate_pct": min(rates) if rates else 0.0,
            "exchanges": items[:12],
        }

    async def _liquidation_map(self, exchange: str) -> dict[str, Any]:
        rows = await self._get(
            "/api/futures/liquidation/coin-list",
            {"exchange": exchange},
            ttl=60,
        )
        rows = rows if isinstance(rows, list) else []
        return {str(row.get("symbol", "")).upper(): row for row in rows}

    async def liquidations(self, symbol: str) -> dict[str, Any]:
        coin = self.coin_from_symbol(symbol)
        results = await asyncio.gather(
            self._liquidation_map("Binance"),
            self._liquidation_map("OKX"),
            return_exceptions=True,
        )
        totals = {
            "long_1h": 0.0,
            "short_1h": 0.0,
            "long_4h": 0.0,
            "short_4h": 0.0,
            "total_1h": 0.0,
            "total_4h": 0.0,
        }
        exchanges: list[dict[str, Any]] = []
        for exchange, result in zip(["Binance", "OKX"], results):
            if isinstance(result, Exception):
                continue
            row = result.get(coin, {})
            if not row:
                continue
            item = {
                "exchange": exchange,
                "long_1h": self._float(row.get("long_liquidation_usd_1h")),
                "short_1h": self._float(row.get("short_liquidation_usd_1h")),
                "long_4h": self._float(row.get("long_liquidation_usd_4h")),
                "short_4h": self._float(row.get("short_liquidation_usd_4h")),
                "total_1h": self._float(row.get("liquidation_usd_1h")),
                "total_4h": self._float(row.get("liquidation_usd_4h")),
            }
            exchanges.append(item)
            for key in totals:
                totals[key] += item[key]
        total_sides = totals["long_1h"] + totals["short_1h"]
        imbalance = (
            (totals["short_1h"] - totals["long_1h"]) / total_sides
            if total_sides > 0 else 0.0
        )
        return {
            "available": bool(exchanges),
            "symbol": coin,
            **totals,
            "short_minus_long_imbalance_1h": imbalance,
            "exchanges": exchanges,
        }

    async def heatmap_summary(self, symbol: str, range_value: str = "24h") -> dict[str, Any]:
        """Best-effort optional heatmap.

        Some CoinGlass heatmap models are not available to Hobbyist.  This method
        is intentionally NOT called by the automatic scanner; it is for manual
        analysis/diagnostics and safely reports plan/API errors without affecting
        trading decisions.
        """
        coin = self.coin_from_symbol(symbol)
        try:
            data = await self._get(
                "/api/futures/liquidation/aggregated-heatmap/model1",
                {"symbol": coin, "range": range_value},
                ttl=120,
            )
        except Exception as exc:
            return {"available": False, "symbol": coin, "error": str(exc)[:500]}
        data = data if isinstance(data, dict) else {}
        y_axis = data.get("y_axis", []) or []
        leverage = data.get("liquidation_leverage_data", []) or []
        candles = data.get("price_candlesticks", []) or []
        current_price = self._float(candles[-1][4]) if candles and len(candles[-1]) > 4 else 0.0
        strength_by_level: dict[int, float] = {}
        for point in leverage:
            if not isinstance(point, list) or len(point) < 3:
                continue
            idx = int(point[1])
            strength_by_level[idx] = strength_by_level.get(idx, 0.0) + self._float(point[2])
        levels = []
        for idx, strength in strength_by_level.items():
            if idx < 0 or idx >= len(y_axis):
                continue
            price = self._float(y_axis[idx])
            distance_pct = ((price - current_price) / current_price * 100) if current_price else 0.0
            levels.append({"price": price, "strength": strength, "distance_pct": distance_pct})
        levels.sort(key=lambda x: x["strength"], reverse=True)
        return {
            "available": bool(levels),
            "symbol": coin,
            "range": range_value,
            "current_price": current_price,
            "strongest_levels": levels[:12],
        }

    async def enrich_symbol(self, symbol: str) -> dict[str, Any]:
        if not self.configured:
            return {
                "available": False,
                "configured": False,
                "symbol": self.coin_from_symbol(symbol),
                "errors": ["CoinGlass API not configured"],
            }

        results = await asyncio.gather(
            self.open_interest(symbol),
            self.taker_buy_sell(symbol, "15m"),
            self.funding(symbol),
            self.liquidations(symbol),
            return_exceptions=True,
        )
        names = ["open_interest", "taker", "funding", "liquidations"]
        output: dict[str, Any] = {
            "available": False,
            "configured": True,
            "symbol": self.coin_from_symbol(symbol),
            "errors": [],
        }
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                output[name] = {"available": False}
                output["errors"].append(f"{name}: {str(result)[:300]}")
            else:
                output[name] = result
                if result.get("available"):
                    output["available"] = True
        critical = [
            bool(output.get("open_interest", {}).get("available")),
            bool(output.get("taker", {}).get("available")),
        ]
        output["critical_complete"] = all(critical)
        return output


coinglass_client = CoinGlassClient()
