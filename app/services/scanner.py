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
from app.services.coinglass import coinglass_client
from app.services.coinglass_confirmation import apply_coinglass_confirmation
from app.services.prediction_engine import build_pre_move_prediction
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
        ticker_result, btc_result = await asyncio.gather(
            binance_client.ticker_24h(),
            binance_client.klines("BTCUSDT", interval="5m", limit=120),
            return_exceptions=True,
        )

        if isinstance(ticker_result, Exception):
            raise RuntimeError(f"Unable to load market universe: {ticker_result}")
        if not isinstance(ticker_result, list) or not ticker_result:
            raise RuntimeError("Market provider returned an empty ticker universe")

        tickers = ticker_result
        startup_errors: list[str] = []
        if isinstance(btc_result, Exception):
            btc_context = {
                "trend": "NEUTRAL",
                "change_15m_pct": 0.0,
                "change_1h_pct": 0.0,
                "degraded": True,
            }
            startup_errors.append(f"BTC context unavailable: {str(btc_result)[:300]}")
        else:
            btc_context = build_btc_context(btc_result)

        universe = [t for t in tickers if _is_candidate_ticker(t)]
        universe.sort(key=lambda t: float(t.get("quoteVolume", 0) or 0), reverse=True)
        universe = universe[: settings.scanner_max_symbols]

        if not universe:
            raise RuntimeError(
                f"Provider returned {len(tickers)} tickers but none passed liquidity/filter rules"
            )

        early = [
            t
            for t in universe
            if abs(float(t.get("priceChangePercent", 0) or 0)) <= 6.0
        ]
        selected = early[: max(1, min(deep_limit, 40))]
        if not selected:
            selected = sorted(
                universe,
                key=lambda t: abs(float(t.get("priceChangePercent", 0) or 0)),
            )[: max(1, min(deep_limit, 40))]
            startup_errors.append(
                "No symbols passed the +/-6% early filter; using least-expanded liquid symbols as diagnostic fallback"
            )

        scanner_progress.set_universe(
            len(universe),
            len(early),
            len(selected),
            data_source=binance_client.active_source,
        )
        for startup_error in startup_errors:
            scanner_progress.errors.appendleft(startup_error)

        semaphore = asyncio.Semaphore(5)

        async def analyze_local(ticker: dict[str, Any]):
            symbol = ticker["symbol"]
            scanner_progress.symbol_started(symbol)
            try:
                async with semaphore:
                    snapshot = await binance_client.deep_snapshot(symbol)
                    score = score_snapshot(snapshot, btc_context=btc_context)
                return ticker, snapshot, score
            except Exception as exc:
                scanner_progress.symbol_finished(symbol, error=str(exc)[:500])
                return exc

        local_results = await asyncio.gather(*(analyze_local(t) for t in selected))
        successful = [item for item in local_results if not isinstance(item, Exception)]
        errors: list[str] = list(startup_errors)
        errors.extend(str(item)[:500] for item in local_results if isinstance(item, Exception))

        # CoinGlass is intentionally reserved for the strongest local setups. The
        # Hobbyist plan is 30 req/min and a confirmation bundle may use several
        # cached endpoints, so querying every scanned symbol would be wasteful.
        successful.sort(key=lambda item: float(item[2].get("setup_score", 0)), reverse=True)
        cg_limit = max(0, min(settings.coinglass_max_scanner_candidates, len(successful)))
        cg_targets = [item for item in successful[:cg_limit] if float(item[2].get("setup_score", 0)) >= 64]
        cg_scores: dict[str, dict[str, Any]] = {}
        cg_errors: list[str] = []

        if coinglass_client.configured and cg_targets:
            cg_sem = asyncio.Semaphore(2)

            async def confirm(item: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]):
                ticker, _snapshot, local_score = item
                symbol = ticker["symbol"]
                try:
                    async with cg_sem:
                        cg = await coinglass_client.enrich_symbol(symbol)
                    cg_scores[symbol] = apply_coinglass_confirmation(local_score, cg)
                except Exception as exc:
                    cg_errors.append(f"{symbol}: {str(exc)[:400]}")
                    fallback = dict(local_score)
                    fallback["coinglass"] = {
                        "available": False,
                        "configured": True,
                        "errors": [str(exc)[:400]],
                    }
                    if settings.coinglass_require_for_ready and fallback.get("state") == "READY":
                        fallback["state"] = "PREPARING"
                        metrics = dict(fallback.get("metrics") or {})
                        rejects = list(metrics.get("reject_reasons") or [])
                        if "coinglass_unavailable_for_ready" not in rejects:
                            rejects.append("coinglass_unavailable_for_ready")
                        metrics["reject_reasons"] = rejects
                        metrics["coinglass_available"] = False
                        fallback["metrics"] = metrics
                    cg_scores[symbol] = fallback

            await asyncio.gather(*(confirm(item) for item in cg_targets))
        elif not coinglass_client.configured:
            startup_errors.append("CoinGlass not configured; READY cannot use multi-exchange confirmation")

        ranked: list[dict[str, Any]] = []
        alerts_created = 0
        coinglass_enriched = 0

        for ticker, snapshot, local_score in successful:
            symbol = ticker["symbol"]
            score = cg_scores.get(symbol, local_score)
            if symbol in cg_scores:
                coinglass_enriched += 1
            elif settings.coinglass_require_for_ready and score.get("state") == "READY":
                score = dict(score)
                score["state"] = "PREPARING"
                metrics = dict(score.get("metrics") or {})
                rejects = list(metrics.get("reject_reasons") or [])
                if "coinglass_not_checked" not in rejects:
                    rejects.append("coinglass_not_checked")
                metrics["reject_reasons"] = rejects
                metrics["coinglass_available"] = False
                score["metrics"] = metrics

            cg_payload = score.get("coinglass") or {}
            prediction = build_pre_move_prediction(score, snapshot, cg_payload)
            prediction_matches_direction = prediction.get("direction") == score.get("direction")
            prediction_phase = str(prediction.get("phase", "SIN_SETUP"))
            prediction_score = float(prediction.get("preactivation_score", 0) or 0)

            # READY requires the actual pre-move trigger to be activated in the same
            # direction and must not already be a chase. A good score by itself is
            # never enough to open the paper trade.
            if score.get("state") == "READY" and (
                not prediction_matches_direction
                or prediction_phase != "ACTIVADO"
                or bool(prediction.get("sequence", {}).get("chase_risk"))
            ):
                score = dict(score)
                score["state"] = "PREPARING"
                metrics = dict(score.get("metrics") or {})
                rejects = list(metrics.get("reject_reasons") or [])
                reason = (
                    "pre_move_direction_conflict"
                    if not prediction_matches_direction
                    else "pre_move_chase_risk"
                    if bool(prediction.get("sequence", {}).get("chase_risk"))
                    else "pre_move_not_activated"
                )
                if reason not in rejects:
                    rejects.append(reason)
                metrics["reject_reasons"] = rejects
                score["metrics"] = metrics

            # Early warning: a WATCH can become PREPARING before the large candle
            # only when the prediction agrees with direction and has a strong enough
            # preparation sequence. Hard NO_TRADE is never promoted here.
            if (
                score.get("state") == "WATCH"
                and prediction_matches_direction
                and prediction_phase in {"PREACTIVACION", "VIGILAR_CONFIRMACION"}
                and prediction_score >= 72
                and float(score.get("risk_score", 100)) <= 48
            ):
                score = dict(score)
                score["state"] = "PREPARING"

            score = dict(score)
            score["prediction"] = prediction
            metrics = dict(score.get("metrics") or {})
            metrics["pre_move_type"] = prediction.get("type")
            metrics["pre_move_phase"] = prediction_phase
            metrics["pre_move_score"] = prediction_score
            metrics["pre_move_trigger"] = prediction.get("trigger_price")
            metrics["pre_move_direction_match"] = prediction_matches_direction
            score["metrics"] = metrics

            use_prediction_plan = (
                prediction_matches_direction
                and prediction_phase not in {"SIN_SETUP", "SIN_DATOS"}
                and prediction_score >= 55
            )
            plan_entry_low = prediction.get("entry_low") if use_prediction_plan else score["entry_low"]
            plan_entry_high = prediction.get("entry_high") if use_prediction_plan else score["entry_high"]
            plan_stop = prediction.get("stop_loss") if use_prediction_plan else score["stop_loss"]
            plan_tp1 = prediction.get("tp1") if use_prediction_plan else score["tp1"]
            plan_tp2 = prediction.get("tp2") if use_prediction_plan else score["tp2"]
            plan_tp3 = prediction.get("tp3") if use_prediction_plan else score["tp3"]
            plan_duration_min = prediction.get("expected_duration_min_minutes") if use_prediction_plan else score["expected_duration_min_minutes"]
            plan_duration_max = prediction.get("expected_duration_max_minutes") if use_prediction_plan else score["expected_duration_max_minutes"]

            # Persist the exact plan the paper engine will later read.
            score["entry_low"] = plan_entry_low
            score["entry_high"] = plan_entry_high
            score["stop_loss"] = plan_stop
            score["tp1"] = plan_tp1
            score["tp2"] = plan_tp2
            score["tp3"] = plan_tp3
            score["expected_duration_min_minutes"] = plan_duration_min
            score["expected_duration_max_minutes"] = plan_duration_max

            scanner_progress.symbol_finished(symbol, score=score)
            symbol_id = await _ensure_symbol(db, symbol)

            raw_bundle = {
                "score": score,
                "local_score_before_coinglass": local_score,
                "ticker": ticker,
                "btc_context": btc_context,
                "market_data_source": snapshot.get("source") or binance_client.active_source,
                "coinglass": score.get("coinglass"),
                "prediction": prediction,
            }
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
                    "oi_change_pct": score["metrics"].get("oi_change_pct", 0),
                    "taker_ratio": score["metrics"].get("taker_avg_3", 1),
                    "funding_rate": score["metrics"].get("funding_rate", 0),
                    "long_short_ratio": float((snapshot.get("long_short") or [{}])[-1].get("longShortRatio", 0) or 0),
                    "relative_volume": score["metrics"].get("relative_volume", 1),
                    "atr_pct": score["metrics"].get("atr_pct", 0),
                    "btc_trend": score["metrics"].get("btc_trend", "NEUTRAL"),
                    "raw_data": json.dumps(raw_bundle),
                },
            )

            signal_id = str(uuid.uuid4())
            reason_bundle = {
                "metrics": score["metrics"],
                "components": score["components"],
                "coinglass": score.get("coinglass"),
                "prediction": prediction,
                "local_setup_score_before_coinglass": local_score.get("setup_score"),
                "local_risk_score_before_coinglass": local_score.get("risk_score"),
            }
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
                        :current_price, :entry_low, :entry_high, :invalidation_price,
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
                    "setup_type": str(prediction.get("type") or "early_expansion").lower(),
                    "setup_score": score["setup_score"],
                    "risk_score": score["risk_score"],
                    "confidence_pct": score.get("confidence_pct"),
                    "current_price": score["current_price"],
                    "entry_low": score["entry_low"],
                    "entry_high": score["entry_high"],
                    "invalidation_price": prediction.get("invalidation_price", score["stop_loss"]),
                    "stop_loss": score["stop_loss"],
                    "tp1": score["tp1"],
                    "tp2": score["tp2"],
                    "tp3": score["tp3"],
                    "expected_move_min_pct": score["expected_move_min_pct"],
                    "expected_move_max_pct": score["expected_move_max_pct"],
                    "expected_duration_min_minutes": score["expected_duration_min_minutes"],
                    "expected_duration_max_minutes": score["expected_duration_max_minutes"],
                    "reason": json.dumps(reason_bundle),
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
                        :relative_volume, :absorption_detected, :breakout_confirmed,
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
                    "oi_change_pct": score["metrics"].get("oi_change_pct", 0),
                    "taker_ratio": score["metrics"].get("taker_avg_3", 1),
                    "funding_rate": score["metrics"].get("funding_rate", 0),
                    "relative_volume": score["metrics"].get("relative_volume", 1),
                    "absorption_detected": score["metrics"].get("absorption_conflict", False),
                    "breakout_confirmed": prediction_phase == "ACTIVADO",
                    "btc_filter_passed": not (
                        (score["direction"] == "LONG" and score["metrics"].get("btc_trend") == "BEARISH")
                        or (score["direction"] == "SHORT" and score["metrics"].get("btc_trend") == "BULLISH")
                    ),
                    "notes": json.dumps({**score["metrics"], "prediction": prediction}),
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

            ranked.append({
                "symbol": symbol,
                "change_24h_pct": float(ticker.get("priceChangePercent", 0) or 0),
                **score,
            })

        errors.extend(cg_errors)
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
            "market_data_source": binance_client.active_source,
            "coinglass": coinglass_client.status(),
            "coinglass_enriched": coinglass_enriched,
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