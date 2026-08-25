from contextlib import asynccontextmanager
import asyncio

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import check_database, ensure_runtime_schema, get_db
from app.services.binance import binance_client
from app.services.coinglass import coinglass_client
from app.services.coinglass_confirmation import apply_coinglass_confirmation
from app.services.dashboard import live_event_feed, live_predictions, prediction_history
from app.services.market_context import market_context
from app.services.news_context import news_context_for_symbol
from app.services.opportunities import calibration_by_score, ranked_opportunities
from app.services.paper_time_management import manage_open_paper_trades_with_time
from app.services.paper_trading import paper_performance, sync_ready_signals
from app.services.prediction_engine import build_pre_move_prediction
from app.services.runtime import runtime_state, start_runtime, stop_runtime
from app.services.scanner import run_scanner
from app.services.scanner_progress import scanner_progress
from app.services.scoring import build_btc_context, score_snapshot


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_runtime_schema()
    tasks = await start_runtime()
    app.state.runtime_tasks = tasks
    try:
        yield
    finally:
        await stop_runtime(tasks)


app = FastAPI(
    title=settings.app_name,
    version="0.11.1",
    description=(
        "ExplodeX early LONG/SHORT scanner with multi-exchange confirmation, "
        "pre-move prediction and paper-only risk management"
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://explodex-web.vercel.app",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _safe_symbol(symbol: str) -> str:
    value = symbol.upper().strip()
    if not value.endswith("USDT"):
        value = f"{value}USDT"
    if not value.replace("USDT", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid symbol")
    return value


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _gate_ready_with_prediction(scored: dict, prediction: dict) -> dict:
    if scored.get("state") != "READY":
        return scored
    direction_match = prediction.get("direction") == scored.get("direction")
    phase = str(prediction.get("phase", "SIN_SETUP"))
    sequence = prediction.get("sequence") if isinstance(prediction.get("sequence"), dict) else {}
    chase_risk = bool(sequence.get("chase_risk"))
    if direction_match and phase == "ACTIVADO" and not chase_risk:
        return scored
    gated = dict(scored)
    gated["state"] = "PREPARING"
    metrics = dict(gated.get("metrics") or {})
    rejects = list(metrics.get("reject_reasons") or [])
    reason = "pre_move_direction_conflict" if not direction_match else "pre_move_chase_risk" if chase_risk else "pre_move_not_activated"
    if reason not in rejects:
        rejects.append(reason)
    metrics["reject_reasons"] = rejects
    metrics["pre_move_direction_match"] = direction_match
    metrics["pre_move_phase"] = phase
    metrics["pre_move_chase_risk"] = chase_risk
    gated["metrics"] = metrics
    return gated


@app.get("/")
async def root():
    return {"name": settings.app_name, "version": "0.11.1", "mode": "paper" if settings.paper_trading_only else "live-enabled", "scheduler_enabled": settings.scheduler_enabled, "market_data_source": binance_client.active_source, "coinglass": {"enabled": settings.coinglass_enabled, "configured": coinglass_client.configured, "require_for_ready": settings.coinglass_require_for_ready}, "prediction_engine": "pre-move-v1", "ready_policy": "direction_match + ACTIVADO + no_chase", "message": "ExplodeX backend online"}


@app.get("/health")
async def health():
    db_ok = await check_database()
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "paper_trading_only": settings.paper_trading_only, "scheduler_enabled": settings.scheduler_enabled, "market_data_source": binance_client.active_source, "provider_warning": binance_client.last_primary_error, "prediction_engine": "pre-move-v1", "ready_policy": "direction_match + ACTIVADO + no_chase", "coinglass": coinglass_client.status()}


@app.get("/api/v1/runtime/status")
async def runtime_status():
    payload = runtime_state.as_dict(); payload["market_data_source"] = binance_client.active_source; payload["provider_warning"] = binance_client.last_primary_error; payload["coinglass"] = coinglass_client.status(); payload["prediction_engine"] = "pre-move-v1"; payload["ready_policy"] = "direction_match + ACTIVADO + no_chase"; return payload


@app.get("/api/v1/scanner/progress")
async def scanner_live_progress():
    payload = scanner_progress.as_dict(); payload["market_data_source"] = binance_client.active_source; payload["provider_warning"] = binance_client.last_primary_error; payload["coinglass"] = coinglass_client.status(); return payload


@app.get("/api/v1/coinglass/status")
async def coinglass_status(probe: bool = Query(default=False)):
    return await coinglass_client.status_probe() if probe else coinglass_client.status()


@app.get("/api/v1/coinglass/{symbol}/heatmap")
async def coinglass_heatmap(symbol: str, range_value: str = Query(default="24h")):
    safe_symbol = _safe_symbol(symbol); allowed = {"12h", "24h", "1d", "3d", "7d", "30d"}; safe_range = range_value if range_value in allowed else "24h"; return await coinglass_client.heatmap_summary(safe_symbol, safe_range)


@app.get("/api/v1/coinglass/{symbol}")
async def coinglass_symbol(symbol: str):
    safe_symbol = _safe_symbol(symbol)
    try: return await coinglass_client.enrich_symbol(safe_symbol)
    except Exception as exc: raise HTTPException(status_code=502, detail=f"CoinGlass unavailable: {exc}") from exc


@app.get("/api/v1/market/context")
async def broad_market_context():
    try:
        payload = await market_context(); payload["market_data_source"] = binance_client.active_source; payload["provider_warning"] = binance_client.last_primary_error; payload["coinglass"] = coinglass_client.status(); return payload
    except Exception as exc: raise HTTPException(status_code=502, detail=f"Market context failed: {exc}") from exc


@app.get("/api/v1/news/{symbol}")
async def symbol_news(symbol: str):
    try: return await news_context_for_symbol(_safe_symbol(symbol))
    except Exception as exc: raise HTTPException(status_code=502, detail=f"News context failed: {exc}") from exc


@app.get("/api/v1/market/price/{symbol}")
async def market_price(symbol: str):
    try:
        payload = await binance_client.price(_safe_symbol(symbol)); payload["source"] = payload.get("source") or binance_client.active_source; return payload
    except Exception as exc: raise HTTPException(status_code=502, detail=f"Market data error: {exc}") from exc


@app.get("/api/v1/market/candles/{symbol}")
async def market_candles(symbol: str, interval: str = Query(default="15m"), limit: int = Query(default=120, ge=20, le=300)):
    allowed = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}; safe_interval = interval if interval in allowed else "15m"; safe_symbol = _safe_symbol(symbol)
    try:
        rows = await binance_client.klines(safe_symbol, safe_interval, limit); candles = []
        for row in rows:
            if len(row) < 8: continue
            candles.append({"time": int(row[0]), "open": _float(row[1]), "high": _float(row[2]), "low": _float(row[3]), "close": _float(row[4]), "volume": _float(row[7])})
        return {"symbol": safe_symbol, "interval": safe_interval, "source": binance_client.active_source, "provider_warning": binance_client.last_primary_error, "candles": candles}
    except Exception as exc: raise HTTPException(status_code=502, detail=f"Candles unavailable: {exc}") from exc


@app.get("/api/v1/analysis/{symbol}")
async def live_symbol_analysis(symbol: str):
    safe_symbol = _safe_symbol(symbol)
    try:
        snapshot, btc_klines = await asyncio.gather(binance_client.deep_snapshot(safe_symbol), binance_client.klines("BTCUSDT", interval="5m", limit=120))
        btc_context = build_btc_context(btc_klines); local_scored = score_snapshot(snapshot, btc_context=btc_context); cg = await coinglass_client.enrich_symbol(safe_symbol); scored = apply_coinglass_confirmation(local_scored, cg); prediction = build_pre_move_prediction(scored, snapshot, cg); scored = _gate_ready_with_prediction(scored, prediction)
        availability = {"price_structure": bool(snapshot.get("klines")), "multi_timeframe_15m": bool(snapshot.get("klines_15m")), "multi_timeframe_1h": bool(snapshot.get("klines_1h")), "open_interest_current": bool(snapshot.get("open_interest")), "open_interest_history": bool(snapshot.get("open_interest_history")), "taker_ratio": bool(snapshot.get("taker")), "funding": bool(snapshot.get("premium")), "global_long_short": bool(snapshot.get("long_short")), "order_book": bool(snapshot.get("order_book", {}).get("bids") or snapshot.get("order_book", {}).get("asks")), "futures_flow": bool(snapshot.get("agg_trades")), "spot_flow": bool(snapshot.get("spot_agg_trades")), "top_trader_accounts": bool(snapshot.get("top_long_short_accounts")), "top_trader_positions": bool(snapshot.get("top_long_short_positions")), "coinglass_aggregated_oi": bool(cg.get("open_interest", {}).get("available")), "coinglass_aggregated_taker": bool(cg.get("taker", {}).get("available")), "coinglass_funding": bool(cg.get("funding", {}).get("available")), "coinglass_liquidations": bool(cg.get("liquidations", {}).get("available"))}
        required = [availability["price_structure"], availability["multi_timeframe_15m"], availability["multi_timeframe_1h"], availability["order_book"], availability["futures_flow"], availability["coinglass_aggregated_oi"]]
        data_quality = "FULL" if all(availability.values()) else "TRADE_GRADE" if all(required) else "LIMITED"
        direction_match = prediction.get("direction") == scored.get("direction"); chase_risk = bool(prediction.get("sequence", {}).get("chase_risk") if isinstance(prediction.get("sequence"), dict) else False)
        return {"symbol": safe_symbol, "source": snapshot.get("source") or binance_client.active_source, "provider_warning": snapshot.get("provider_warning") or binance_client.last_primary_error, "data_quality": data_quality, "availability": availability, "coinglass": cg, "prediction": prediction, "current_open_interest": _float(snapshot.get("open_interest", {}).get("openInterest")), "direction": scored["direction"], "state": scored["state"], "setup_score": scored["setup_score"], "local_setup_score_before_coinglass": local_scored["setup_score"], "long_score": scored["long_score"], "short_score": scored["short_score"], "risk_score": scored["risk_score"], "local_risk_score_before_coinglass": local_scored["risk_score"], "current_price": scored["current_price"], "entry_low": prediction.get("entry_low", scored["entry_low"]), "entry_high": prediction.get("entry_high", scored["entry_high"]), "invalidation_price": prediction.get("invalidation_price", scored["stop_loss"]), "stop_loss": prediction.get("stop_loss", scored["stop_loss"]), "tp1": prediction.get("tp1", scored["tp1"]), "tp2": prediction.get("tp2", scored["tp2"]), "tp3": prediction.get("tp3", scored["tp3"]), "expected_move_min_pct": scored["expected_move_min_pct"], "expected_move_max_pct": scored["expected_move_max_pct"], "expected_duration_min_minutes": prediction.get("expected_duration_min_minutes", scored["expected_duration_min_minutes"]), "expected_duration_max_minutes": prediction.get("expected_duration_max_minutes", scored["expected_duration_max_minutes"]), "components": scored["components"], "metrics": scored["metrics"], "btc_context": btc_context, "ready_checks": {"direction_match": direction_match, "prediction_activated": prediction.get("phase") == "ACTIVADO", "chase_risk": chase_risk, "ready": scored.get("state") == "READY"}, "risk_policy": {"paper_only": settings.paper_trading_only, "coinglass_required_for_ready": settings.coinglass_require_for_ready, "prediction_activation_required_for_ready": True, "direction_match_required_for_ready": True, "no_chase_required_for_ready": True, "score_is_probability": False}, "note": "El score mide calidad del setup; la predicción previa tampoco garantiza una vela grande."}
    except Exception as exc: raise HTTPException(status_code=502, detail=f"Live analysis unavailable: {exc}") from exc


@app.post("/api/v1/scanner/run")
async def scanner_run(deep_limit: int = Query(default=20, ge=1, le=40), db: AsyncSession = Depends(get_db)):
    try: return await run_scanner(db, deep_limit=deep_limit)
    except Exception as exc: raise HTTPException(status_code=500, detail=f"Scanner failed: {exc}") from exc


@app.get("/api/v1/scanner/latest")
async def scanner_latest(db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("SELECT id::text, started_at, finished_at, symbols_scanned, candidates_found, status, error_message FROM scanner_runs ORDER BY started_at DESC LIMIT 1")); row = result.mappings().first(); return dict(row) if row else None


@app.get("/api/v1/signals/active")
async def active_signals(limit: int = Query(default=20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("SELECT s.id::text, sy.symbol, s.created_at, s.direction, s.state, s.setup_score, s.risk_score, s.confidence_pct, s.current_price, s.entry_low, s.entry_high, s.stop_loss, s.tp1, s.tp2, s.tp3, s.expected_move_min_pct, s.expected_move_max_pct, s.expected_duration_min_minutes, s.expected_duration_max_minutes, s.reason FROM signals s JOIN symbols sy ON sy.id = s.symbol_id WHERE s.is_active = TRUE ORDER BY s.setup_score DESC, s.risk_score ASC, s.created_at DESC LIMIT :limit"), {"limit": limit}); return [dict(row) for row in result.mappings().all()]


@app.get("/api/v1/predictions/live")
async def predictions_live(limit: int = Query(default=50, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    return await live_predictions(db, limit=limit)


@app.get("/api/v1/predictions/{symbol}/history")
async def predictions_history(symbol: str, limit: int = Query(default=12, ge=2, le=50), db: AsyncSession = Depends(get_db)):
    return await prediction_history(db, _safe_symbol(symbol), limit=limit)


@app.get("/api/v1/events/live")
async def events_live(limit: int = Query(default=80, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    return await live_event_feed(db, limit=limit)


@app.get("/api/v1/opportunities")
async def opportunities(limit: int = Query(default=50, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    return await ranked_opportunities(db, limit=limit)


@app.get("/api/v1/calibration")
async def calibration(db: AsyncSession = Depends(get_db)):
    return await calibration_by_score(db)


@app.post("/api/v1/paper/sync")
async def paper_sync(db: AsyncSession = Depends(get_db)):
    try: return await sync_ready_signals(db)
    except Exception as exc: raise HTTPException(status_code=500, detail=f"Paper sync failed: {exc}") from exc


@app.post("/api/v1/paper/manage")
async def paper_manage(db: AsyncSession = Depends(get_db)):
    try: return await manage_open_paper_trades_with_time(db)
    except Exception as exc: raise HTTPException(status_code=500, detail=f"Paper manager failed: {exc}") from exc


@app.get("/api/v1/paper/open")
async def paper_open(limit: int = Query(default=20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("SELECT t.id::text, sy.symbol, t.direction, t.status, t.leverage, t.risk_pct, t.entry_price, t.quantity, t.notional_usdt, t.stop_loss, t.tp1, t.tp2, t.tp3, t.opened_at, t.pnl_usdt, t.r_multiple, t.metadata FROM trades t JOIN symbols sy ON sy.id = t.symbol_id WHERE t.mode = 'PAPER' AND t.status IN ('OPEN','PARTIAL') ORDER BY t.opened_at DESC LIMIT :limit"), {"limit": limit}); return [dict(row) for row in result.mappings().all()]


@app.get("/api/v1/paper/history")
async def paper_history(limit: int = Query(default=50, ge=1, le=500), db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("SELECT t.id::text, sy.symbol, t.direction, t.status, t.entry_price, t.exit_price, t.stop_loss, t.tp1, t.tp2, t.tp3, t.opened_at, t.closed_at, t.pnl_usdt, t.pnl_pct, t.r_multiple, t.fees_usdt, t.close_reason FROM trades t JOIN symbols sy ON sy.id = t.symbol_id WHERE t.mode = 'PAPER' AND t.status IN ('CLOSED','STOPPED') ORDER BY t.closed_at DESC LIMIT :limit"), {"limit": limit}); return [dict(row) for row in result.mappings().all()]


@app.get("/api/v1/paper/performance")
async def paper_stats(db: AsyncSession = Depends(get_db)):
    return await paper_performance(db)


@app.get("/api/v1/alerts/pending")
async def pending_alerts(limit: int = Query(default=50, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        SELECT a.id::text, a.signal_id::text, a.trade_id::text, a.created_at,
               a.channel, a.severity, a.title, a.message,
               COALESCE(sy_signal.symbol, sy_trade.symbol) AS symbol,
               COALESCE(s.direction, t.direction) AS direction,
               s.state AS signal_state,
               s.setup_score, s.risk_score,
               COALESCE(s.entry_low, t.entry_price) AS entry_low,
               COALESCE(s.entry_high, t.entry_price) AS entry_high,
               COALESCE(s.stop_loss, t.stop_loss) AS stop_loss,
               COALESCE(s.tp1, t.tp1) AS tp1,
               COALESCE(s.tp2, t.tp2) AS tp2,
               COALESCE(s.tp3, t.tp3) AS tp3
        FROM alerts a
        LEFT JOIN signals s ON s.id = a.signal_id
        LEFT JOIN symbols sy_signal ON sy_signal.id = s.symbol_id
        LEFT JOIN trades t ON t.id = a.trade_id
        LEFT JOIN symbols sy_trade ON sy_trade.id = t.symbol_id
        WHERE a.is_sent = FALSE
        ORDER BY a.created_at ASC
        LIMIT :limit
    """), {"limit": limit})
    return [dict(row) for row in result.mappings().all()]
