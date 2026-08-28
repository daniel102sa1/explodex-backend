from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.validation_mode import recent_validation_rows, run_validation_cycle, validation_report


router = APIRouter(prefix="/api/v1/validation", tags=["validation"])


@router.get("/report")
async def validation_mode_report(db: AsyncSession = Depends(get_db)):
    return await validation_report(db)


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
