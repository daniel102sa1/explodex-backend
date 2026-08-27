from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BINANCE_BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")
DEFAULT_INTERVAL_SECONDS = 8


def _json_request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None) -> Any:
    request = Request(url=url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"message": raw[:500]}
        message = payload.get("msg") or payload.get("message") or raw[:500]
        raise RuntimeError(f"HTTP {exc.code}: {message}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def _server_offset_ms() -> int:
    payload = _json_request(f"{BINANCE_BASE_URL}/fapi/v1/time")
    server_time = int((payload or {}).get("serverTime") or 0)
    if server_time <= 0:
        raise RuntimeError("Binance did not return server time")
    return server_time - int(time.time() * 1000)


def _signed_get(path: str, api_key: str, api_secret: str, *, offset_ms: int, params: dict[str, Any] | None = None) -> Any:
    query_params = dict(params or {})
    query_params["recvWindow"] = 5000
    query_params["timestamp"] = int(time.time() * 1000) + offset_ms
    query = urlencode(query_params, doseq=True)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": api_key}
    return _json_request(f"{BINANCE_BASE_URL}{path}?{query}&signature={signature}", headers=headers)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _normalize_positions(rows: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        amount = _number(row.get("positionAmt"))
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


def _normalize_orders(rows: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        output.append({
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
        })
    return output


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _bridge_signature(timestamp_ms: int, payload: dict[str, Any], secret: str) -> str:
    message = str(timestamp_ms).encode("utf-8") + b"." + _canonical(payload)
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _push_snapshot(backend_url: str, api_key: str, api_secret: str, positions: list[dict[str, Any]], orders: list[dict[str, Any]]) -> dict[str, Any]:
    timestamp_ms = int(time.time() * 1000)
    payload = {"positions": positions, "open_orders": orders}
    body = _canonical(payload)
    headers = {
        "Content-Type": "application/json",
        "X-ExplodeX-Api-Key": api_key,
        "X-ExplodeX-Timestamp": str(timestamp_ms),
        "X-ExplodeX-Signature": _bridge_signature(timestamp_ms, payload, api_secret),
    }
    response = _json_request(
        f"{backend_url.rstrip('/')}/api/v1/binance-user/bridge/push",
        method="POST",
        headers=headers,
        body=body,
    )
    return response if isinstance(response, dict) else {}


def run(backend_url: str, api_key: str, api_secret: str, interval_seconds: int) -> None:
    print("ExplodeX Binance Bridge · READ ONLY")
    print("No puede abrir, cerrar, cancelar ni modificar órdenes.")
    print(f"Backend: {backend_url}")
    print("Ctrl+C para detener.\n")

    offset_ms = _server_offset_ms()
    cycle = 0
    while True:
        try:
            cycle += 1
            if cycle % 30 == 0:
                offset_ms = _server_offset_ms()
            positions_raw = _signed_get("/fapi/v3/positionRisk", api_key, api_secret, offset_ms=offset_ms)
            orders_raw = _signed_get("/fapi/v1/openOrders", api_key, api_secret, offset_ms=offset_ms)
            positions = _normalize_positions(positions_raw)
            orders = _normalize_orders(orders_raw)
            pushed = _push_snapshot(backend_url, api_key, api_secret, positions, orders)
            now = time.strftime("%H:%M:%S")
            print(f"[{now}] OK · posiciones={len(positions)} · órdenes={len(orders)} · backend={pushed.get('ok', False)}")
        except KeyboardInterrupt:
            print("\nBridge detenido.")
            return
        except Exception as exc:
            print(f"[{time.strftime('%H:%M:%S')}] ERROR · {exc}", file=sys.stderr)
        time.sleep(max(5, interval_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="ExplodeX read-only Binance Futures local bridge")
    parser.add_argument("--backend", default=os.getenv("EXPLODEX_BACKEND_URL", ""), help="ExplodeX backend public URL")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Seconds between snapshots (minimum 5)")
    args = parser.parse_args()

    api_key = os.getenv("BINANCE_USER_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_USER_API_SECRET", "").strip()
    backend_url = str(args.backend or "").strip()

    if not backend_url:
        backend_url = input("URL pública del backend Railway (https://...): ").strip()
    if not api_key:
        api_key = input("Binance API Key: ").strip()
    if not api_secret:
        api_secret = getpass.getpass("Binance Secret Key (oculta): ").strip()

    if not api_key or not api_secret:
        raise SystemExit("API Key / Secret are required.")
    if not backend_url.startswith("https://") and not backend_url.startswith("http://localhost"):
        raise SystemExit("La URL del backend debe empezar con https:// (o localhost en desarrollo).")

    run(backend_url, api_key, api_secret, max(5, int(args.interval)))


if __name__ == "__main__":
    main()
