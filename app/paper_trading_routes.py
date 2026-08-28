from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.paper_execution_v2 import run_paper_cycle_v2
from app.services.paper_portfolio import paper_history, paper_summary

router = APIRouter(prefix="/api/v1/paper-trading", tags=["paper-trading"])


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    return await paper_summary(db)


@router.get("/history")
async def history(
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    return {
        "version": "paper_portfolio_v1",
        "execution_version": "paper_execution_v2",
        "paper_only": True,
        "rows": await paper_history(db, limit=limit),
    }


@router.post("/run")
async def run_cycle(db: AsyncSession = Depends(get_db)):
    return {
        "version": "paper_portfolio_v1",
        "execution_version": "paper_execution_v2",
        "paper_only": True,
        "result": await run_paper_cycle_v2(db),
        "note": "Simulación únicamente. La apertura usa el precio observable al ejecutar el ciclo; no envía órdenes reales ni usa credenciales privadas de trading.",
    }
