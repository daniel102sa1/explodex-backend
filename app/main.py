from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import check_database, get_db
from app.services.binance import binance_client
from app.services.market_context import market_context
from app.services.news_context import news_context_for_symbol
from app.services.opportunities import calibration_by_score, ranked_opportunities
from app.services.paper_trading import (
    manage_open_paper_trades,
    paper_performance,
    sync_ready_signals,
)
from app.services.runtime import runtime_state, start_runtime, stop_runtime
from app.services.scanner import run_scanner
from app.services.scanner_progress import scanner_progress


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = await start_runtime()
    app.state.runtime_tasks = tasks
    try:
        yield
    finally:
        await stop_runtime(tasks)


app = FastAPI(
    title=settings.app_name,
    version="0.6.0",
    description="ExplodeX early LONG/SHORT scanner for Binance USDT-M Futures",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "version": "0.6.0",
        "mode": "paper" if settings.paper_trading_only else "live-enabled",
        "scheduler_enabled": settings.scheduler_enabled,
        "message": "ExplodeX backend online",
    }


@app.get("/health")
async def health():
    db_ok = await check_database()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "paper_trading_only": settings.paper_trading_only,
        "scheduler_enabled": settings.scheduler_enabled,
    }


@app.get("/api/v1/runtime/status")
async def runtime_status():
    return runtime_state.as_dict()


@app.get("/api/v1/scanner/progress")
async def scanner_live_progress():
    return scanner_progress.as_dict()


@app.get("/api/v1/market/context")
async def broad_market_context():
    try:
        return await market_context()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Market context failed: {exc}") from exc


@app.get("/api/v1/news/{symbol}")
async def symbol_news(symbol: str):
    try:
        return await news_context_for_symbol(symbol)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"News context failed: {exc}") from exc


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
                   s.stop_loss, s.tp1, s.tp2, s.tp3,
                   s.expected_move_min_pct, s.expected_move_max_pct,
                   s.expected_duration_min_minutes, s.expected_duration_max_minutes,
                   s.reason
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


@app.get("/api/v1/opportunities")
async def opportunities(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    return await ranked_opportunities(db, limit=limit)


@app.get("/api/v1/calibration")
async def calibration(db: AsyncSession = Depends(get_db)):
    return await calibration_by_score(db)


@app.post("/api/v1/paper/sync")
async def paper_sync(db: AsyncSession = Depends(get_db)):
    try:
        return await sync_ready_signals(db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Paper sync failed: {exc}") from exc


@app.post("/api/v1/paper/manage")
async def paper_manage(db: AsyncSession = Depends(get_db)):
    try:
        return await manage_open_paper_trades(db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Paper manager failed: {exc}") from exc


@app.get("/api/v1/paper/open")
async def paper_open(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text(
            """
            SELECT t.id::text, sy.symbol, t.direction, t.status, t.leverage,
                   t.risk_pct, t.entry_price, t.quantity, t.notional_usdt,
                   t.stop_loss, t.tp1, t.tp2, t.tp3, t.opened_at,
                   t.pnl_usdt, t.r_multiple, t.metadata
            FROM trades t
            JOIN symbols sy ON sy.id = t.symbol_id
            WHERE t.mode = 'PAPER' AND t.status IN ('OPEN','PARTIAL')
            ORDER BY t.opened_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


@app.get("/api/v1/paper/history")
async def paper_history(
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text(
            """
            SELECT t.id::text, sy.symbol, t.direction, t.status,
                   t.entry_price, t.exit_price, t.stop_loss, t.tp1, t.tp2, t.tp3,
                   t.opened_at, t.closed_at, t.pnl_usdt, t.pnl_pct,
                   t.r_multiple, t.fees_usdt, t.close_reason
            FROM trades t
            JOIN symbols sy ON sy.id = t.symbol_id
            WHERE t.mode = 'PAPER' AND t.status IN ('CLOSED','STOPPED')
            ORDER BY t.closed_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


@app.get("/api/v1/paper/performance")
async def paper_stats(db: AsyncSession = Depends(get_db)):
    return await paper_performance(db)


@app.get("/api/v1/alerts/pending")
async def pending_alerts(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text(
            """
            SELECT id::text, signal_id::text, trade_id::text, created_at,
                   channel, severity, title, message
            FROM alerts
            WHERE is_sent = FALSE
            ORDER BY created_at ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]
