from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

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
        "hint": (
            "Check Binance API permissions and Railway variables. Never paste the API secret into chat."
        ),
    }
    return HTTPException(status_code=status, detail=detail)


@router.get("/status")
async def binance_user_status(probe: bool = Query(default=False)):
    status = binance_user_client.status()
    if not probe or not status.get("configured"):
        return status
    try:
        positions = await binance_user_client.positions()
        return {
            **binance_user_client.status(),
            "probe_ok": True,
            "open_positions": len(positions),
        }
    except BinanceUserApiError as exc:
        return {
            **binance_user_client.status(),
            "probe_ok": False,
            "probe_error": str(exc),
            "binance_code": exc.code,
        }


@router.get("/positions")
async def binance_user_positions():
    try:
        return {
            "mode": "READ_ONLY",
            "positions": await binance_user_client.positions(),
            "capabilities": binance_user_client.status().get("capabilities"),
        }
    except BinanceUserApiError as exc:
        raise _api_error(exc) from exc


@router.get("/open-orders")
async def binance_user_open_orders(symbol: str | None = Query(default=None)):
    safe = _safe_symbol(symbol) if symbol else None
    try:
        return {
            "mode": "READ_ONLY",
            "orders": await binance_user_client.open_orders(safe),
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
            "trades": await binance_user_client.user_trades(safe, limit=limit),
        }
    except BinanceUserApiError as exc:
        raise _api_error(exc) from exc


async def _coach_one(position: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = str(position.get("symbol") or "")
    analysis: dict[str, Any] | None = None
    analysis_error: str | None = None
    try:
        # Import lazily: app.main is already fully loaded before this router is
        # mounted by app.asgi, avoiding a circular import at module import time.
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
        positions, orders = await asyncio.gather(
            binance_user_client.positions(),
            binance_user_client.open_orders(),
        )
    except BinanceUserApiError as exc:
        raise _api_error(exc) from exc

    selected = positions[:limit]
    rows = await asyncio.gather(*(_coach_one(position, orders) for position in selected))
    return {
        "mode": "READ_ONLY_LIVE_COACH",
        "configured": binance_user_client.configured,
        "position_count": len(positions),
        "shown": len(rows),
        "positions": rows,
        "safety": {
            "can_place_orders": False,
            "can_cancel_orders": False,
            "can_move_stop": False,
            "can_withdraw": False,
        },
        "note": (
            "ExplodeX only reads Binance account state here. The coach is analysis, not an instruction to hold, close, add size, or move a stop."
        ),
    }
