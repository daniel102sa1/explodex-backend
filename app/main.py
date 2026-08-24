from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import check_database, get_db
from app.services.binance import binance_client
from app.services.scanner import run_scanner

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="ExplodeX early LONG/SHORT scanner for Binance USDT-M Futures",
)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "version": "0.1.0",
        "mode": "paper" if settings.paper_trading_only else "live-enabled",
        "message": "ExplodeX backend online",
    }


@app.get("/health")
async def health():
    db_ok = await check_database()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "paper_trading_only": settings.paper_trading_only,
    }


@app.get("/api/v1/market/price/{symbol}")
async def market_price(symbol: str):
    try:
        return await binance_client.price(symbol)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Binance error: {exc}") from exc


@app.post("/api/v1/scanner/run")
async def scanner_run(
    deep_limit: int = Query(default=20, ge=1, le=40),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await run_scanner(db, deep_limit=deep_limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Scanner failed: {exc}") from exc


@app.get("/api/v1/scanner/latest")
async def scanner_latest(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text(
            """
            SELECT id::text, started_at, finished_at, symbols_scanned,
                   candidates_found, status, error_message
            FROM scanner_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
    )
    row = result.mappings().first()
    return dict(row) if row else None


@app.get("/api/v1/signals/active")
async def active_signals(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text(
            """
            SELECT s.id::text, sy.symbol, s.created_at, s.direction, s.state,
                   s.setup_score, s.risk_score, s.confidence_pct,
                   s.current_price, s.entry_low, s.entry_high,
                   s.stop_loss, s.tp1, s.tp2, s.tp3, s.reason
            FROM signals s
            JOIN symbols sy ON sy.id = s.symbol_id
            WHERE s.is_active = TRUE
            ORDER BY s.setup_score DESC, s.risk_score ASC, s.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]
