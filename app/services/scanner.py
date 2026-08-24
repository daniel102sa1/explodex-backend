from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.binance import binance_client
from app.services.scanner_progress import scanner_progress
from app.services.scoring import build_btc_context, score_snapshot
from app.services.signal_alerts import create_signal_alert


def _is_candidate_ticker(t: dict[str, Any]) -> bool:
    symbol = str(t.get("symbol", ""))
    if not symbol.endswith("USDT"):
        return False
    if "_" in symbol:
        return False
    quote_volume = float(t.get("quoteVolume", 0) or 0)
    return quote_volume >= settings.scanner_min_quote_volume_usdt


async def _ensure_symbol(db: AsyncSession, symbol: str) -> str:
    result = await db.execute(
        text("SELECT id::text FROM symbols WHERE symbol = :symbol"),
        {"symbol": symbol},
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    symbol_id = str(uuid.uuid4())
    await db.execute(
        text(
            """
            INSERT INTO symbols (id, symbol, base_asset, quote_asset, enabled)
            VALUES (:id, :symbol, :base_asset, 'USDT', TRUE)
            """
        ),
        {
            "id": symbol_id,
            "symbol": symbol,
            "base_asset": symbol.removesuffix("USDT"),
        },
    )
    return symbol_id


async def run_scanner(db: AsyncSession, deep_limit: int = 20) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    scanner_progress.start(run_id)

    await db.execute(
        text("INSERT INTO scanner_runs (id, started_at, status) VALUES (:id, :started_at, 'running')"),
        {"id": run_id, "started_at": started_at},
    )
    await db.commit()

    try:
        # The tradable universe is mandatory. BTC context is useful but must never
        # prevent the scanner from running by itself.
        ticker_result, btc_result = await asyncio.gather(
            binance_client.ticker_24h(),
            binance_client.klines("BTCUSDT", interval="5m", limit=120),
            return_exceptions=True,
        )

        if isinstance(ticker_result, Exception):
            raise RuntimeError(f"Unable to load Binance Futures universe: {ticker_result}")
        if not isinstance(ticker_result, list) or not ticker_result:
            raise RuntimeError("Binance Futures returned an empty ticker universe")

        tickers = ticker_result
        startup_errors: list[str] = []
        if isinstance(btc_result, Exception):
            btc_context = {"trend": "NEUTRAL", "change_15m_pct": 0.0, "change_1h_pct": 0.0, "degraded": True}
            startup_errors.append(f"BTC context unavailable: {str(btc_result)[:300]}")
        else:
            btc_context = build_btc_context(btc_result)

        universe = [t for t in tickers if _is_candidate_ticker(t)]
        universe.sort(key=lambda t: float(t.get("quoteVolume", 0) or 0), reverse=True)
        universe = universe[: settings.scanner_max_symbols]

        if not universe:
            raise RuntimeError(
                f"Binance returned {len(tickers)} tickers but none passed the liquidity/filter rules"
            )

        early = [
            t
            for t in universe
            if abs(float(t.get("priceChangePercent", 0) or 0)) <= 6.0
        ]
        selected = early[: max(1, min(deep_limit, 40))]

        # If every liquid symbol is already outside the early-move filter, still
        # analyze the least-expanded names instead of returning an empty scanner.
        if not selected:
            fallback = sorted(
                universe,
                key=lambda t: abs(float(t.get("priceChangePercent", 0) or 0)),
            )
            selected = fallback[: max(1, min(deep_limit, 40))]
            startup_errors.append("No symbols passed the +/-6% early filter; using least-expanded liquid symbols as diagnostic fallback")

        scanner_progress.set_universe(len(universe), len(early), len(selected), data_source="BINANCE_FUTURES")
        for startup_error in startup_errors:
            scanner_progress.errors.appendleft(startup_error)

        semaphore = asyncio.Semaphore(5)

        async def analyze(ticker: dict[str, Any]):
            symbol = ticker["symbol"]
            scanner_progress.symbol_started(symbol)
            try:
                async with semaphore:
                    snapshot = await binance_client.deep_snapshot(symbol)
                    score = score_snapshot(snapshot, btc_context=btc_context)
                scanner_progress.symbol_finished(symbol, score=score)
                return ticker, snapshot, score
            except Exception as exc:
                scanner_progress.symbol_finished(symbol, error=str(exc)[:500])
                return exc

        results_raw = await asyncio.gather(*(analyze(t) for t in selected))

        ranked: list[dict[str, Any]] = []
        errors: list[str] = list(startup_errors)
        alerts_created = 0

        for item in results_raw:
            if isinstance(item, Exception):
                errors.append(str(item)[:500])
                continue

            ticker, snapshot, score = item
            symbol = ticker["symbol"]
            symbol_id = await _ensure_symbol(db, symbol)

            await db.execute(
                text(
                    """
                    INSERT INTO market_snapshots (
                        symbol_id, captured_at, price, change_24h_pct, volume_24h_usdt,
                        open_interest, open_interest_change_pct, taker_buy_sell_ratio,
                        funding_rate, long_short_ratio, volume_5m, relative_volume,
                        atr_pct, btc_trend, raw_data
                    ) VALUES (
                        :symbol_id, NOW(), :price, :change_24h_pct, :volume_24h_usdt,
                        :open_interest, :oi_change_pct, :taker_ratio,
                        :funding_rate, :long_short_ratio, NULL, :relative_volume,
                        :atr_pct, :btc_trend, CAST(:raw_data AS JSONB)
                    )
                    """
                ),
                {
                    "symbol_id": symbol_id,
                    "price": score["current_price"],
                    "change_24h_pct": float(ticker.get("priceChangePercent", 0) or 0),
                    "volume_24h_usdt": float(ticker.get("quoteVolume", 0) or 0),
                    "open_interest": float(snapshot.get("open_interest", {}).get("openInterest", 0) or 0),
                    "oi_change_pct": score["metrics"]["oi_change_pct"],
                    "taker_ratio": score["metrics"]["taker_avg_3"],
                    "funding_rate": score["metrics"]["funding_rate"],
                    "long_short_ratio": float((snapshot.get("long_short") or [{}])[-1].get("longShortRatio", 0) or 0),
                    "relative_volume": score["metrics"]["relative_volume"],
                    "atr_pct": score["metrics"]["atr_pct"],
                    "btc_trend": score["metrics"]["btc_trend"],
                    "raw_data": json.dumps({"score": score, "ticker": ticker, "btc_context": btc_context}),
                },
            )

            signal_id = str(uuid.uuid4())
            await db.execute(
                text(
                    """
                    INSERT INTO signals (
                        id, symbol_id, scanner_run_id, direction, state, setup_type,
                        timeframe, setup_score, risk_score, confidence_pct,
                        current_price, entry_low, entry_high, invalidation_price,
                        stop_loss, tp1, tp2, tp3,
                        expected_move_min_pct, expected_move_max_pct,
                        expected_duration_min_minutes, expected_duration_max_minutes,
                        reason, is_active
                    ) VALUES (
                        :id, :symbol_id, :scanner_run_id, :direction, :state, :setup_type,
                        '5m', :setup_score, :risk_score, :confidence_pct,
                        :current_price, :entry_low, :entry_high, :stop_loss,
                        :stop_loss, :tp1, :tp2, :tp3,
                        :expected_move_min_pct, :expected_move_max_pct,
                        :expected_duration_min_minutes, :expected_duration_max_minutes,
                        :reason, TRUE
                    )
                    """
                ),
                {
                    "id": signal_id,
                    "symbol_id": symbol_id,
                    "scanner_run_id": run_id,
                    "direction": score["direction"],
                    "state": score["state"],
                    "setup_type": "early_expansion",
                    "setup_score": score["setup_score"],
                    "risk_score": score["risk_score"],
                    "confidence_pct": score["confidence_pct"],
                    "current_price": score["current_price"],
                    "entry_low": score["entry_low"],
                    "entry_high": score["entry_high"],
                    "stop_loss": score["stop_loss"],
                    "tp1": score["tp1"],
                    "tp2": score["tp2"],
                    "tp3": score["tp3"],
                    "expected_move_min_pct": score["expected_move_min_pct"],
                    "expected_move_max_pct": score["expected_move_max_pct"],
                    "expected_duration_min_minutes": score["expected_duration_min_minutes"],
                    "expected_duration_max_minutes": score["expected_duration_max_minutes"],
                    "reason": json.dumps({"metrics": score["metrics"], "components": score["components"]}),
                },
            )

            await db.execute(
                text(
                    """
                    INSERT INTO signal_metrics (
                        signal_id, structure_score, oi_score, taker_score, volume_score,
                        funding_score, btc_score, absorption_score, volatility_score,
                        liquidity_score, oi_change_pct, taker_ratio, funding_rate,
                        relative_volume, absorption_detected, breakout_confirmed,
                        btc_filter_passed, notes
                    ) VALUES (
                        :signal_id, :structure_score, :oi_score, :taker_score, :volume_score,
                        :funding_score, :btc_score, :absorption_score, :volatility_score,
                        :liquidity_score, :oi_change_pct, :taker_ratio, :funding_rate,
                        :relative_volume, :absorption_detected, FALSE,
                        :btc_filter_passed, CAST(:notes AS JSONB)
                    )
                    """
                ),
                {
                    "signal_id": signal_id,
                    "structure_score": score["components"].get("structure"),
                    "oi_score": score["components"].get("oi"),
                    "taker_score": score["components"].get("taker"),
                    "volume_score": score["components"].get("volume"),
                    "funding_score": score["components"].get("funding"),
                    "btc_score": score["components"].get("btc"),
                    "absorption_score": score["components"].get("response"),
                    "volatility_score": score["components"].get("compression"),
                    "liquidity_score": score["components"].get("orderbook", 10),
                    "oi_change_pct": score["metrics"]["oi_change_pct"],
                    "taker_ratio": score["metrics"]["taker_avg_3"],
                    "funding_rate": score["metrics"]["funding_rate"],
                    "relative_volume": score["metrics"]["relative_volume"],
                    "absorption_detected": score["metrics"]["absorption_conflict"],
                    "btc_filter_passed": not (
                        (score["direction"] == "LONG" and score["metrics"]["btc_trend"] == "BEARISH")
                        or (score["direction"] == "SHORT" and score["metrics"]["btc_trend"] == "BULLISH")
                    ),
                    "notes": json.dumps(score["metrics"]),
                },
            )

            if await create_signal_alert(
                db,
                signal_id=signal_id,
                symbol_id=symbol_id,
                symbol=symbol,
                score=score,
            ):
                alerts_created += 1

            ranked.append({"symbol": symbol, "change_24h_pct": float(ticker.get("priceChangePercent", 0) or 0), **score})

        ranked.sort(key=lambda x: (x["setup_score"], -x["risk_score"]), reverse=True)
        candidates = [x for x in ranked if x["state"] != "NO_TRADE"]
        final_status = "completed" if ranked else "degraded"

        await db.execute(
            text(
                """
                UPDATE scanner_runs
                SET finished_at = NOW(), symbols_scanned = :symbols_scanned,
                    candidates_found = :candidates_found, status = :status,
                    error_message = :error_message
                WHERE id = :id
                """
            ),
            {
                "id": run_id,
                "symbols_scanned": len(selected),
                "candidates_found": len(candidates),
                "status": final_status,
                "error_message": " | ".join(errors[:5]) if errors else None,
            },
        )
        await db.commit()
        scanner_progress.finish(final_status)

        return {
            "run_id": run_id,
            "btc_context": btc_context,
            "symbols_scanned": len(selected),
            "successful_analyses": len(ranked),
            "candidates_found": len(candidates),
            "alerts_created": alerts_created,
            "status": final_status,
            "errors": errors[:5],
            "top": ranked[:10],
        }
    except Exception as exc:
        scanner_progress.fatal_error(str(exc))
        scanner_progress.finish("failed")
        await db.rollback()
        await db.execute(
            text("UPDATE scanner_runs SET finished_at = NOW(), status = 'failed', error_message = :error WHERE id = :id"),
            {"id": run_id, "error": str(exc)[:2000]},
        )
        await db.commit()
        raise
