from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.selective_precision_lab import selective_precision_report
from app.services.trade_now_diagnostics import trade_now_reachability_report
from app.services.validation_mode import recent_validation_rows, run_validation_cycle, validation_report


router = APIRouter(prefix="/api/v1/validation", tags=["validation"])


@router.get("/report")
async def validation_mode_report(db: AsyncSession = Depends(get_db)):
    return await validation_report(db)


@router.get("/trade-now-reachability")
async def validation_trade_now_reachability(
    limit: int = Query(default=2000, ge=10, le=10000),
    db: AsyncSession = Depends(get_db),
):
    return await trade_now_reachability_report(db, limit=limit)


@router.get("/selective-precision")
async def validation_selective_precision(
    horizon_minutes: int = Query(default=60, ge=5, le=120),
    limit: int = Query(default=10000, ge=100, le=50000),
    db: AsyncSession = Depends(get_db),
):
    return await selective_precision_report(db, horizon_minutes=horizon_minutes, limit=limit)


@router.get("/recent")
async def validation_mode_recent(
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    return {
        "version": "validation_mode_v1",
        "rows": await recent_validation_rows(db, limit=limit),
        "paper_research_only": True,
    }


@router.post("/run")
async def validation_mode_run(db: AsyncSession = Depends(get_db)):
    result = await run_validation_cycle(db)
    return {
        "version": "validation_mode_v1",
        "result": result,
        "paper_research_only": True,
        "note": "Este ciclo solo captura y evalúa predicciones pasadas; no modifica reglas de entrada ni ejecuta órdenes.",
    }
