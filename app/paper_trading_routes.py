from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.paper_execution_v2 import run_paper_cycle_v2
from app.services.paper_micro_scalp import micro_summary, scan_micro_scalps
from app.services.paper_orders import paper_order_history, paper_order_stats
from app.services.paper_portfolio import paper_history, paper_summary
from app.services.paper_range_micro import range_summary, scan_all_eligible_ranges
from app.services.validation_mode import ensure_validation_schema

router = APIRouter(prefix="/api/v1/paper-trading", tags=["paper-trading"])


async def _ensure_paper_dependencies(db: AsyncSession) -> None:
    """Create validation tables before PAPER tables that reference them."""
    await ensure_validation_schema(db)


async def _safe_component(
    db: AsyncSession,
    name: str,
    loader: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    """Keep one optional PAPER diagnostic from taking down the whole summary."""
    try:
        return await loader(db)
    except Exception as exc:
        # A failed SQL statement leaves PostgreSQL transactions aborted. Roll it
        # back so the next optional component can still load.
        await db.rollback()
        return {
            "available": False,
            "paper_only": True,
            "component": name,
            "error_type": type(exc).__name__,
        }


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    result = await paper_summary(db)
    result["orders"] = await _safe_component(db, "orders", paper_order_stats)
    result["range_micro"] = await _safe_component(db, "range_micro", range_summary)
    result["micro_scalp"] = await _safe_component(db, "micro_scalp", micro_summary)
    return result


@router.get("/history")
async def history(
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_paper_dependencies(db)
    return {
        "version": "paper_portfolio_v1",
        "execution_version": "paper_execution_v2_multi_strategy_v2",
        "paper_only": True,
        "rows": await paper_history(db, limit=limit),
    }


@router.get("/orders")
async def orders(
    limit: int = Query(default=200, ge=1, le=1000),
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_paper_dependencies(db)
    normalized = str(status or "").upper() or None
    if normalized not in {None, "PENDING", "FILLED", "CANCELED"}:
        normalized = None
    return {
        "version": "paper_orders_v1",
        "paper_only": True,
        "stats": await paper_order_stats(db),
        "rows": await paper_order_history(db, limit=limit, status=normalized),
    }


@router.get("/range-micro")
async def range_micro_summary(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await range_summary(db)


@router.post("/range-micro/scan")
async def range_micro_scan(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return {
        "paper_only": True,
        "result": await scan_all_eligible_ranges(db, force=True),
        "note": "Escanea todas las Futures USDT elegibles por liquidez para detectar rangos laterales PAPER. No envía órdenes reales.",
    }


@router.get("/micro-scalp")
async def micro_scalp_summary(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return await micro_summary(db)


@router.post("/micro-scalp/scan")
async def micro_scalp_scan(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return {
        "paper_only": True,
        "result": await scan_micro_scalps(db, force=True),
        "note": "MICRO SCALP busca operaciones PAPER cortas en mercados líquidos. También muestra por qué se rechazan monedas. No cambia el clasificador de entradas reales ni envía órdenes reales.",
    }


@router.post("/run")
async def run_cycle(db: AsyncSession = Depends(get_db)):
    await _ensure_paper_dependencies(db)
    return {
        "version": "paper_portfolio_v1",
        "execution_version": "paper_execution_v2_multi_strategy_v2",
        "paper_only": True,
        "result": await run_paper_cycle_v2(db),
        "note": "Simulación únicamente. TREND/PRE-MOVE, RANGE MICRO y MICRO SCALP comparten la cuenta PAPER; costos, posiciones y cierres se guardan en PostgreSQL. No se envían órdenes reales.",
    }
