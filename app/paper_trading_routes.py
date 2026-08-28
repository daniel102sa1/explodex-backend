from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.paper_execution_v2 import run_paper_cycle_v2
from app.services.paper_orders import paper_order_history, paper_order_stats
from app.services.paper_portfolio import paper_history, paper_summary

router = APIRouter(prefix="/api/v1/paper-trading", tags=["paper-trading"])


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    result = await paper_summary(db)
    result["orders"] = await paper_order_stats(db)
    return result


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


@router.get("/orders")
async def orders(
    limit: int = Query(default=200, ge=1, le=1000),
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    normalized = str(status or "").upper() or None
    if normalized not in {None, "PENDING", "FILLED", "CANCELED"}:
        normalized = None
    return {
        "version": "paper_orders_v1",
        "paper_only": True,
        "stats": await paper_order_stats(db),
        "rows": await paper_order_history(db, limit=limit, status=normalized),
    }


@router.post("/run")
async def run_cycle(db: AsyncSession = Depends(get_db)):
    return {
        "version": "paper_portfolio_v1",
        "execution_version": "paper_execution_v2",
        "paper_only": True,
        "result": await run_paper_cycle_v2(db),
        "note": "Simulación únicamente. Las órdenes, posiciones y cierres se guardan en PostgreSQL; no se envían órdenes reales ni se usan credenciales privadas de trading.",
    }
