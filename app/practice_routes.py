from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.practice_symbols import practice_symbol_catalog
from app.services.practice_trading import (
    cancel_practice_order,
    close_practice_trade,
    create_practice_limit_order,
    modify_practice_trade,
    move_practice_stop_to_break_even,
    open_practice_trade,
    partial_close_practice_trade,
    practice_events,
    practice_history,
    practice_summary,
    reset_practice_account,
    sync_practice_account,
)

router = APIRouter(prefix="/api/v1/practice", tags=["practice-trading"])


class PracticeOpenRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80)
    symbol: str = Field(min_length=2, max_length=32)
    side: str
    order_type: str = Field(default="MARKET", max_length=16)
    limit_price: float | None = Field(default=None, gt=0)
    margin: float = Field(gt=0)
    leverage: int = Field(ge=1, le=20)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    tp2: float | None = Field(default=None, gt=0)
    tp3: float | None = Field(default=None, gt=0)
    timeframe: str | None = Field(default=None, max_length=16)
    pattern: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=1000)




class PracticeModifyRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    tp2: float | None = Field(default=None, gt=0)
    tp3: float | None = Field(default=None, gt=0)


class PracticePartialCloseRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80)
    fraction: float = Field(gt=0, le=1)


class PracticeSessionRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80)


class PracticeCloseRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80)
    reason: str = Field(default="USER_CLOSE", max_length=64)


class PracticeResetRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80)


@router.get("/symbols")
async def get_practice_symbols():
    """Active futures symbols without opening a PostgreSQL session."""
    try:
        return await practice_symbol_catalog()
    except Exception:
        return {"source":"UNAVAILABLE","symbols":[],"count":0,"paper_only":True}


@router.get("/summary")
async def get_practice_summary(
    session_id: str = Query(min_length=8, max_length=80),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await practice_summary(db, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/history")
async def get_practice_history(
    session_id: str = Query(min_length=8, max_length=80),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    try:
        return {
            "paper_only": True,
            "practice_mode": True,
            "rows": await practice_history(db, session_id, limit=limit),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/open")
async def open_manual_practice_trade(
    request: PracticeOpenRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        order_type = str(request.order_type or "MARKET").upper()
        if order_type == "LIMIT":
            if request.limit_price is None:
                raise ValueError("limit_price is required for LIMIT practice orders")
            return await create_practice_limit_order(
                db,
                session_id=request.session_id,
                symbol=request.symbol,
                side=request.side,
                limit_price=request.limit_price,
                margin=request.margin,
                leverage=request.leverage,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
                tp2=request.tp2,
                tp3=request.tp3,
                timeframe=request.timeframe,
                pattern=request.pattern,
                note=request.note,
            )
        if order_type != "MARKET":
            raise ValueError("order_type must be MARKET or LIMIT")
        return await open_practice_trade(
            db,
            session_id=request.session_id,
            symbol=request.symbol,
            side=request.side,
            margin=request.margin,
            leverage=request.leverage,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            tp2=request.tp2,
            tp3=request.tp3,
            timeframe=request.timeframe,
            pattern=request.pattern,
            note=request.note,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=f"practice trade open failed: {type(exc).__name__}") from exc


@router.post("/{trade_id}/close")
async def close_manual_practice_trade(
    trade_id: int,
    request: PracticeCloseRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await close_practice_trade(
            db,
            session_id=request.session_id,
            trade_id=trade_id,
            reason=request.reason,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=f"practice trade close failed: {type(exc).__name__}") from exc


@router.post("/reset")
async def reset_manual_practice_account(
    request: PracticeResetRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await reset_practice_account(db, request.session_id)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sync")
async def sync_manual_practice(
    request: PracticeSessionRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await sync_practice_account(db, request.session_id)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{trade_id}/break-even")
async def move_manual_practice_to_break_even(
    trade_id: int,
    request: PracticeSessionRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await move_practice_stop_to_break_even(db, request.session_id, trade_id)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{trade_id}/modify")
async def modify_manual_practice_trade(
    trade_id: int,
    request: PracticeModifyRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await modify_practice_trade(
            db,
            session_id=request.session_id,
            trade_id=trade_id,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            tp2=request.tp2,
            tp3=request.tp3,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{trade_id}/partial-close")
async def partial_close_manual_practice_trade(
    trade_id: int,
    request: PracticePartialCloseRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await partial_close_practice_trade(
            db,
            session_id=request.session_id,
            trade_id=trade_id,
            fraction=request.fraction,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/orders/{order_id}/cancel")
async def cancel_manual_practice_order(
    order_id: int,
    request: PracticeSessionRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await cancel_practice_order(db, request.session_id, order_id)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{trade_id}/events")
async def get_manual_practice_events(
    trade_id: int,
    session_id: str = Query(min_length=8, max_length=80),
    db: AsyncSession = Depends(get_db),
):
    try:
        return {
            "paper_only": True,
            "practice_mode": True,
            "rows": await practice_events(db, session_id, trade_id),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
