from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.services.binance_bridge_cache import binance_bridge_cache, verify_bridge_request
from app.services.binance_position_coach import build_position_coach
from app.services.binance_user import BinanceUserApiError, binance_user_client


router = APIRouter(prefix="/api/v1/binance-user", tags=["binance-user-read-only"])


def _safe_symbol(symbol: str) -> str:
    value = symbol.upper().strip()
    if not value.endswith("USDT"):
        value = f"{value}USDT"
    if not value.replace("USDT", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid symbol")
    return value


def _api_error(exc: BinanceUserApiError) -> HTTPException:
    status = exc.status_code if exc.status_code and 400 <= exc.status_code < 600 else 502
    detail = {
        "message": str(exc),
        "binance_code": exc.code,
        "read_only": True,
        "bridge_available": binance_bridge_cache.fresh,
        "hint": (
            "If Railway is blocked by Binance 451, run ExplodeX Bridge from a device/network where your Binance Futures account is normally available. "
            "Do not use VPN/proxy to evade Binance regional restrictions."
        ),
    }
    return HTTPException(status_code=status, detail=detail)


def _bridge_payload() -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    if not binance_bridge_cache.fresh:
        return None
    return list(binance_bridge_cache.positions), list(binance_bridge_cache.open_orders)


async def _read_positions_and_orders() -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str | None]:
    try:
        positions, orders = await asyncio.gather(
            binance_user_client.positions(),
            binance_user_client.open_orders(),
        )
        return positions, orders, "DIRECT_RAILWAY", None
    except BinanceUserApiError as exc:
        bridge = _bridge_payload()
        if bridge is not None and exc.status_code in {403, 451, 502, 503, 504}:
            positions, orders = bridge
            return positions, orders, "LOCAL_BRIDGE", str(exc)
        raise


@router.get("/status")
async def binance_user_status(probe: bool = Query(default=False)):
    status = binance_user_client.status()
    base = {
        **status,
        "bridge": binance_bridge_cache.snapshot(),
    }
    if not probe or not status.get("configured"):
        return base
    try:
        positions, _orders, source, direct_error = await _read_positions_and_orders()
        return {
            **base,
            "probe_ok": True,
            "source": source,
            "open_positions": len(positions),
            "direct_error": direct_error,
        }
    except BinanceUserApiError as exc:
        return {
            **base,
            "probe_ok": False,
            "probe_error": str(exc),
            "binance_code": exc.code,
            "http_status": exc.status_code,
            "source": "UNAVAILABLE",
        }


@router.post("/bridge/push")
async def binance_bridge_push(
    payload: dict[str, Any],
    x_explodex_api_key: str = Header(default=""),
    x_explodex_timestamp: str = Header(default=""),
    x_explodex_signature: str = Header(default=""),
):
    try:
        timestamp_ms = int(x_explodex_timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid bridge timestamp")

    ok, error = verify_bridge_request(
        api_key=x_explodex_api_key,
        timestamp_ms=timestamp_ms,
        signature=x_explodex_signature,
        payload=payload,
    )
    if not ok:
        raise HTTPException(status_code=401, detail=error or "Bridge authentication failed")

    positions = payload.get("positions") if isinstance(payload.get("positions"), list) else []
    orders = payload.get("open_orders") if isinstance(payload.get("open_orders"), list) else []
    if len(positions) > 50 or len(orders) > 500:
        raise HTTPException(status_code=400, detail="Bridge payload exceeds safety limits")

    binance_bridge_cache.store(
        source_timestamp_ms=timestamp_ms,
        positions=positions,
        open_orders=orders,
    )
    return {
        "ok": True,
        "mode": "READ_ONLY_LOCAL_BRIDGE",
        "position_count": len(positions),
        "open_order_count": len(orders),
        "bridge": binance_bridge_cache.snapshot(),
    }


@router.get("/positions")
async def binance_user_positions():
    try:
        positions, _orders, source, direct_error = await _read_positions_and_orders()
        return {
            "mode": "READ_ONLY",
            "source": source,
            "direct_error": direct_error,
            "positions": positions,
            "bridge": binance_bridge_cache.snapshot(),
            "capabilities": binance_user_client.status().get("capabilities"),
        }
    except BinanceUserApiError as exc:
        raise _api_error(exc) from exc


@router.get("/open-orders")
async def binance_user_open_orders(symbol: str | None = Query(default=None)):
    safe = _safe_symbol(symbol) if symbol else None
    try:
        positions, orders, source, direct_error = await _read_positions_and_orders()
        del positions
        if safe:
            orders = [row for row in orders if str(row.get("symbol") or "") == safe]
        return {
            "mode": "READ_ONLY",
            "source": source,
            "direct_error": direct_error,
            "orders": orders,
            "bridge": binance_bridge_cache.snapshot(),
        }
    except BinanceUserApiError as exc:
        raise _api_error(exc) from exc


@router.get("/trades/{symbol}")
async def binance_user_trades(symbol: str, limit: int = Query(default=50, ge=1, le=200)):
    safe = _safe_symbol(symbol)
    try:
        return {
            "mode": "READ_ONLY",
            "symbol": safe,
            "source": "DIRECT_RAILWAY",
            "trades": await binance_user_client.user_trades(safe, limit=limit),
        }
    except BinanceUserApiError as exc:
        raise _api_error(exc) from exc


async def _coach_one(position: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = str(position.get("symbol") or "")
    analysis: dict[str, Any] | None = None
    analysis_error: str | None = None
    try:
        from app.main import live_symbol_analysis

        payload = await live_symbol_analysis(symbol)
        analysis = payload if isinstance(payload, dict) else None
    except Exception as exc:
        analysis_error = f"{type(exc).__name__}: {str(exc)[:300]}"

    symbol_orders = [row for row in orders if str(row.get("symbol") or "") == symbol]
    coach = build_position_coach(position, analysis, symbol_orders)
    return {
        "position": position,
        "coach": coach,
        "open_orders": symbol_orders,
        "analysis_available": analysis is not None,
        "analysis_error": analysis_error,
        "analysis": {
            "direction": analysis.get("direction"),
            "state": analysis.get("state"),
            "current_price": analysis.get("current_price"),
            "entry_low": analysis.get("entry_low"),
            "entry_high": analysis.get("entry_high"),
            "stop_loss": analysis.get("stop_loss"),
            "tp1": analysis.get("tp1"),
            "tp2": analysis.get("tp2"),
            "tp3": analysis.get("tp3"),
            "prediction": analysis.get("prediction"),
        } if analysis else None,
    }


@router.get("/coach")
async def binance_live_position_coach(limit: int = Query(default=8, ge=1, le=12)):
    try:
        positions, orders, source, direct_error = await _read_positions_and_orders()
    except BinanceUserApiError as exc:
        raise _api_error(exc) from exc

    selected = positions[:limit]
    rows = await asyncio.gather(*(_coach_one(position, orders) for position in selected))
    return {
        "mode": "READ_ONLY_LIVE_COACH",
        "source": source,
        "direct_error": direct_error,
        "configured": binance_user_client.configured,
        "position_count": len(positions),
        "shown": len(rows),
        "positions": rows,
        "bridge": binance_bridge_cache.snapshot(),
        "safety": {
            "can_place_orders": False,
            "can_cancel_orders": False,
            "can_move_stop": False,
            "can_withdraw": False,
        },
        "note": (
            "ExplodeX only reads Binance account state here. LOCAL_BRIDGE means the private Binance request came from your own device/network; "
            "the backend receives position/order data only. The coach is analysis, not an instruction to hold, close, add size, or move a stop."
        ),
    }
