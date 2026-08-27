from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings


class BinanceUserApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class BinanceUserClient:
    """Read-only signed client for the user's Binance USD-M Futures account.

    This class intentionally exposes GET-only methods. It cannot place, cancel,
    amend, transfer, or withdraw anything even if the API key is later given
    broader permissions by mistake.
    """

    def __init__(self) -> None:
        self.base_url = settings.binance_futures_base_url.rstrip("/")
        self.api_key = settings.binance_user_api_key.strip()
        self.api_secret = settings.binance_user_api_secret.strip()
        self.read_only = bool(settings.binance_user_api_read_only)
        self.recv_window = 5000
        self.timeout = 12.0
        self._server_offset_ms = 0
        self._offset_updated_at = 0.0
        self.last_ok_at: float | None = None
        self.last_error: str | None = None
        self.last_http_status: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.read_only)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "read_only": self.read_only,
            "base_url": self.base_url,
            "last_ok_at_ms": int(self.last_ok_at * 1000) if self.last_ok_at else None,
            "last_http_status": self.last_http_status,
            "last_error": self.last_error,
            "capabilities": {
                "read_positions": True,
                "read_open_orders": True,
                "read_user_trades": True,
                "place_orders": False,
                "cancel_orders": False,
                "withdraw": False,
                "transfer": False,
            },
        }

    async def _refresh_time_offset(self) -> None:
        now = time.monotonic()
        if now - self._offset_updated_at < 30:
            return
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/fapi/v1/time")
        response.raise_for_status()
        payload = response.json()
        server_time = int(payload.get("serverTime") or 0)
        if server_time > 0:
            self._server_offset_ms = server_time - int(time.time() * 1000)
            self._offset_updated_at = now

    async def _signed_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.configured:
            raise BinanceUserApiError(
                "Binance user API is not configured in read-only mode.",
                status_code=503,
            )
        if not path.startswith("/fapi/"):
            raise BinanceUserApiError("Blocked non-Futures path.", status_code=400)

        await self._refresh_time_offset()
        query_params: dict[str, Any] = dict(params or {})
        query_params["recvWindow"] = self.recv_window
        query_params["timestamp"] = int(time.time() * 1000) + self._server_offset_ms
        query = urlencode(query_params, doseq=True)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signed_query = f"{query}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}{path}?{signed_query}", headers=headers)
            self.last_http_status = response.status_code
            payload = response.json()
            if response.status_code >= 400:
                code = payload.get("code") if isinstance(payload, dict) else None
                message = payload.get("msg") if isinstance(payload, dict) else response.text[:300]
                self.last_error = f"Binance {response.status_code}: {message}"
                raise BinanceUserApiError(
                    str(message or "Binance private API error"),
                    status_code=response.status_code,
                    code=int(code) if isinstance(code, int) else None,
                )
            self.last_ok_at = time.time()
            self.last_error = None
            return payload
        except BinanceUserApiError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            raise BinanceUserApiError(self.last_error, status_code=502) from exc

    async def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        payload = await self._signed_get("/fapi/v3/positionRisk", params)
        rows = payload if isinstance(payload, list) else []
        output: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                amount = float(row.get("positionAmt") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            if abs(amount) <= 0:
                continue
            position_side = str(row.get("positionSide") or "BOTH").upper()
            direction = position_side if position_side in {"LONG", "SHORT"} else ("LONG" if amount > 0 else "SHORT")
            output.append({
                "symbol": str(row.get("symbol") or ""),
                "direction": direction,
                "position_side": position_side,
                "position_amount": abs(amount),
                "signed_position_amount": amount,
                "entry_price": _number(row.get("entryPrice")),
                "break_even_price": _number(row.get("breakEvenPrice")),
                "mark_price": _number(row.get("markPrice")),
                "unrealized_pnl": _number(row.get("unRealizedProfit")),
                "liquidation_price": _number(row.get("liquidationPrice")),
                "notional": abs(_number(row.get("notional"))),
                "leverage": int(_number(row.get("leverage"))),
                "margin_type": str(row.get("marginType") or "").upper(),
                "isolated_margin": _number(row.get("isolatedMargin")),
                "isolated_wallet": _number(row.get("isolatedWallet")),
                "update_time": int(_number(row.get("updateTime"))),
            })
        return output

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        payload = await self._signed_get("/fapi/v1/openOrders", params)
        rows = payload if isinstance(payload, list) else []
        return [_normalize_order(row) for row in rows if isinstance(row, dict)]

    async def user_trades(self, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        payload = await self._signed_get(
            "/fapi/v1/userTrades",
            {"symbol": symbol, "limit": safe_limit},
        )
        rows = payload if isinstance(payload, list) else []
        output: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            output.append({
                "symbol": str(row.get("symbol") or symbol),
                "trade_id": row.get("id"),
                "order_id": row.get("orderId"),
                "side": str(row.get("side") or ""),
                "position_side": str(row.get("positionSide") or ""),
                "price": _number(row.get("price")),
                "quantity": _number(row.get("qty")),
                "quote_quantity": _number(row.get("quoteQty")),
                "realized_pnl": _number(row.get("realizedPnl")),
                "commission": _number(row.get("commission")),
                "commission_asset": str(row.get("commissionAsset") or ""),
                "time": int(_number(row.get("time"))),
                "buyer": bool(row.get("buyer")),
                "maker": bool(row.get("maker")),
            })
        return output


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _normalize_order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row.get("symbol") or ""),
        "order_id": row.get("orderId"),
        "client_order_id": row.get("clientOrderId"),
        "side": str(row.get("side") or ""),
        "position_side": str(row.get("positionSide") or ""),
        "type": str(row.get("type") or ""),
        "status": str(row.get("status") or ""),
        "price": _number(row.get("price")),
        "stop_price": _number(row.get("stopPrice")),
        "orig_quantity": _number(row.get("origQty")),
        "executed_quantity": _number(row.get("executedQty")),
        "reduce_only": bool(row.get("reduceOnly")),
        "close_position": bool(row.get("closePosition")),
        "time_in_force": str(row.get("timeInForce") or ""),
        "update_time": int(_number(row.get("updateTime") or row.get("time"))),
    }


binance_user_client = BinanceUserClient()
