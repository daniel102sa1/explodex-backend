from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.binance import binance_client
from app.services.scoring import score_snapshot


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
    await db.execute(
        text("INSERT INTO scanner_runs (id, started_at, status) VALUES (:id, :started_at, 'running')"),
        {"id": run_id, "started_at": started_at},
    )
    await db.commit()

    try:
        tickers = await binance_client.ticker_24h()
        universe = [t for t in tickers if _is_candidate_ticker(t)]

        # Prefer liquid symbols that have not already moved excessively in 24h.
        universe.sort(key=lambda t: float(t.get("quoteVolume", 0) or 0), reverse=True)
        universe = universe[: settings.scanner_max_symbols]

        early = [
            t for t in universe
            if abs(float(t.get("priceChangePercent", 0) or 0)) <= 6.0
        ]
        selected = early[: max(1, min(deep_limit, 40))]

        semaphore = asyncio.Semaphore(5)

        async def analyze(ticker: dict[str, Any]):
            async with semaphore:
                symbol = ticker["symbol"]
                snapshot = await binance_client.deep_snapshot(symbol)
                score = score_snapshot(snapshot)
                return ticker, snapshot, score

        results_raw = await asyncio.gather(
            *(analyze(t) for t in selected),
            return_exceptions=True,
        )

        ranked: list[dict[str, Any]] = []
        for item in results_raw:
            if isinstance(item, Exception):
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
                        NULL, NULL, CAST(:raw_data AS JSONB)
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
                    "raw_data": __import__("json").dumps({"score": score, "ticker": ticker}),
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
                        stop_loss, tp1, tp2, tp3, reason, is_active
                    ) VALUES (
                        :id, :symbol_id, :scanner_run_id, :direction, :state, :setup_type,
                        '5m', :setup_score, :risk_score, :confidence_pct,
                        :current_price, :entry_low, :entry_high, :stop_loss,
                        :stop_loss, :tp1, :tp2, :tp3, :reason, TRUE
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
                    "confidence_pct": score["setup_score"],
                    "current_price": score["current_price"],
                    "entry_low": score["entry_low"],
                    "entry_high": score["entry_high"],
                    "stop_loss": score["stop_loss"],
                    "tp1": score["tp1"],
                    "tp2": score["tp2"],
                    "tp3": score["tp3"],
                    "reason": __import__("json").dumps(score["metrics"]),
                },
            )

            ranked.append(
                {
                    "symbol": symbol,
                    "change_24h_pct": float(ticker.get("priceChangePercent", 0) or 0),
                    **score,
                }
            )

        ranked.sort(key=lambda x: (x["setup_score"], -x["risk_score"]), reverse=True)
        candidates = [x for x in ranked if x["state"] != "NO_TRADE"]

        await db.execute(
            text(
                """
                UPDATE scanner_runs
                SET finished_at = NOW(), symbols_scanned = :symbols_scanned,
                    candidates_found = :candidates_found, status = 'completed'
                WHERE id = :id
                """
            ),
            {
                "id": run_id,
                "symbols_scanned": len(selected),
                "candidates_found": len(candidates),
            },
        )
        await db.commit()

        return {
            "run_id": run_id,
            "symbols_scanned": len(selected),
            "candidates_found": len(candidates),
            "top": ranked[:10],
        }
    except Exception as exc:
        await db.rollback()
        await db.execute(
            text(
                "UPDATE scanner_runs SET finished_at = NOW(), status = 'failed', error_message = :error WHERE id = :id"
            ),
            {"id": run_id, "error": str(exc)[:2000]},
        )
        await db.commit()
        raise
