from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.practice_trading import (
    close_practice_trade,
    open_practice_trade,
    practice_history,
    practice_summary,
    reset_practice_account,
)

router = APIRouter(prefix="/api/v1/practice", tags=["practice-trading"])


class PracticeOpenRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80)
    symbol: str = Field(min_length=2, max_length=32)
    side: str
    margin: float = Field(gt=0)
    leverage: int = Field(ge=1, le=20)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    tp2: float | None = Field(default=None, gt=0)
    tp3: float | None = Field(default=None, gt=0)
    timeframe: str | None = Field(default=None, max_length=16)
    pattern: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=1000)


class PracticeCloseRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80)
    reason: str = Field(default="USER_CLOSE", max_length=64)


class PracticeResetRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80)


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
