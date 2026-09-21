from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.binance import binance_client
from app.services.macro_cycle_engine import VERSION as MACRO_VERSION, build_macro_cycle

VERSION = "macro_cycle_persistence_v2_independent_radar"
MAX_SIGNAL_SYMBOLS = 12
ROTATING_RADAR_SYMBOLS = 10
CONCURRENCY = 3


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _d(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


async def ensure_macro_cycle_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS macro_cycle_snapshots (
            symbol TEXT PRIMARY KEY,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            macro JSONB NOT NULL,
            source_context TEXT NOT NULL DEFAULT 'RADAR'
        )
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_macro_cycle_snapshots_observed_at
        ON macro_cycle_snapshots (observed_at DESC)
    """))
    await db.commit()


def _eligible_macro_ticker(ticker: dict[str, Any]) -> bool:
    symbol = str(ticker.get("symbol") or "").upper()
    if not symbol.endswith("USDT") or "_" in symbol:
        return False
    return _f(ticker.get("quoteVolume")) >= float(settings.scanner_min_quote_volume_usdt)


async def _radar_symbols(db: AsyncSession, *, current_symbols: set[str]) -> list[str]:
    """Rotate through the liquid universe so dormant multi-year bases are visible.

    Macro discovery must not depend on a coin already winning the intraday setup
    scanner. Current signals and open PAPER symbols are always included, then a
    small batch of the stalest liquid symbols is refreshed each scanner cycle.
    """
    symbols = set(current_symbols)

    try:
        open_rows = (await db.execute(text("""
            SELECT DISTINCT symbol
            FROM paper_positions
            WHERE status='OPEN'
        """))).scalars().all()
        symbols.update(str(x).upper() for x in open_rows if x)
    except Exception:
        await db.rollback()

    try:
        tickers = await binance_client.ticker_24h()
        universe = [dict(t) for t in tickers if isinstance(t, dict) and _eligible_macro_ticker(t)]
        universe.sort(key=lambda t: _f(t.get("quoteVolume")), reverse=True)
        universe = universe[: max(20, int(settings.scanner_max_symbols))]
        liquid_symbols = [str(t.get("symbol") or "").upper() for t in universe]
    except Exception:
        liquid_symbols = []

    if liquid_symbols:
        existing_rows = (await db.execute(text("""
            SELECT symbol, observed_at
            FROM macro_cycle_snapshots
            WHERE symbol = ANY(:symbols)
        """), {"symbols": liquid_symbols})).mappings().all()
        seen = {str(row["symbol"]).upper(): row.get("observed_at") for row in existing_rows}

        # Unseen symbols first, then oldest observations. This creates a rotating
        # universe scan without firing dozens of long-history requests every minute.
        ordered = sorted(
            liquid_symbols,
            key=lambda symbol: (
                0 if symbol not in seen else 1,
                seen.get(symbol) or 0,
            ),
        )
        for symbol in ordered[:ROTATING_RADAR_SYMBOLS]:
            symbols.add(symbol)

    return sorted(symbols)


async def persist_macro_cycle_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    await ensure_macro_cycle_schema(db)

    signal_rows = [dict(row) for row in (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.setup_score, s.risk_score, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.setup_score DESC NULLS LAST, s.risk_score ASC NULLS LAST
        LIMIT :limit
    """), {"run_id": run_id, "limit": MAX_SIGNAL_SYMBOLS})).mappings().all()]

    current_by_symbol = {str(row.get("symbol") or "").upper(): row for row in signal_rows}
    radar_symbols = await _radar_symbols(db, current_symbols=set(current_by_symbol))

    if not radar_symbols:
        return {"version": VERSION, "seen": 0, "updated": 0, "macro_version": MACRO_VERSION}

    try:
        btc_payload = await binance_client.historical_daily_klines("BTCUSDT", 1095)
        btc_rows = list(btc_payload.get("rows") or [])
    except Exception:
        btc_rows = []

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(symbol: str) -> tuple[str, dict[str, Any]]:
        try:
            async with semaphore:
                payload = await binance_client.historical_daily_klines(symbol, 1095)
            macro = build_macro_cycle(
                list(payload.get("rows") or []),
                btc_rows=btc_rows,
                source=str(payload.get("source") or ""),
            )
            macro["history_warning"] = payload.get("warning")
            macro["days_requested"] = payload.get("days_requested")
        except Exception as exc:
            macro = {
                "version": MACRO_VERSION,
                "available": False,
                "symbol": symbol,
                "reason": "macro_runtime_error",
                "error": f"{type(exc).__name__}:{str(exc)[:240]}",
                "can_create_entry": False,
            }
        return symbol, macro

    results = await asyncio.gather(*(one(symbol) for symbol in radar_symbols))

    updated_signals = 0
    states: dict[str, int] = {}
    long_bases = 0
    full_3y = 0
    radar_errors = 0

    for symbol, macro in results:
        source_context = "SIGNAL" if symbol in current_by_symbol else "RADAR"
        await db.execute(text("""
            INSERT INTO macro_cycle_snapshots (symbol, observed_at, macro, source_context)
            VALUES (:symbol, NOW(), CAST(:macro AS JSONB), :source_context)
            ON CONFLICT (symbol) DO UPDATE SET
                observed_at=EXCLUDED.observed_at,
                macro=EXCLUDED.macro,
                source_context=EXCLUDED.source_context
        """), {
            "symbol": symbol,
            "macro": json.dumps(macro),
            "source_context": source_context,
        })

        state = str(macro.get("state") or "UNAVAILABLE")
        states[state] = states.get(state, 0) + 1
        long_bases += int(bool(macro.get("long_base_candidate")))
        full_3y += int(bool(macro.get("complete_3y")))
        radar_errors += int(not bool(macro.get("available")))

        row = current_by_symbol.get(symbol)
        if not row:
            continue

        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart:
            continue

        heart["macro_cycle"] = macro
        learning = _d(heart.get("learning"))
        learning["macro_cycle"] = {
            "version": macro.get("version"),
            "history_days": macro.get("history_days"),
            "state": macro.get("state"),
            "bias": macro.get("bias"),
            "score_is_probability": False,
            "entry_authority": False,
        }
        heart["learning"] = learning

        reason["macro_cycle"] = macro
        reason["explodex_heart"] = heart
        if prediction:
            prediction["macro_cycle"] = macro
            prediction["explodex_heart"] = heart
            reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals
            SET reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {"signal_id": row["signal_id"], "reason": json.dumps(reason)})
        updated_signals += 1

    await db.commit()
    return {
        "version": VERSION,
        "macro_version": MACRO_VERSION,
        "signal_symbols": len(signal_rows),
        "radar_symbols": len(radar_symbols),
        "updated_signals": updated_signals,
        "states": states,
        "long_base_candidates": long_bases,
        "complete_3y_histories": full_3y,
        "radar_errors": radar_errors,
        "independent_of_intraday_candidate_selection": True,
        "can_create_entry": False,
    }


async def macro_cycle_report(db: AsyncSession, *, minutes: int = 180, limit: int = 20) -> dict[str, Any]:
    await ensure_macro_cycle_schema(db)
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT m.symbol, m.observed_at, m.macro, m.source_context,
               latest.direction AS signal_direction,
               latest.state AS signal_state,
               latest.setup_score,
               latest.risk_score
        FROM macro_cycle_snapshots m
        LEFT JOIN LATERAL (
            SELECT s.direction, s.state, s.setup_score, s.risk_score
            FROM signals s
            JOIN symbols sy ON sy.id=s.symbol_id
            WHERE sy.symbol=m.symbol
            ORDER BY s.created_at DESC
            LIMIT 1
        ) latest ON TRUE
        WHERE m.observed_at >= NOW() - (:minutes * INTERVAL '1 minute')
        ORDER BY m.observed_at DESC
    """), {"minutes": max(30, min(minutes, 10080))})).mappings().all()]

    items: list[dict[str, Any]] = []
    for raw in rows:
        macro = _d(raw.get("macro"))
        if not macro:
            continue
        items.append({
            "symbol": raw.get("symbol"),
            "observed_at": raw.get("observed_at").isoformat() if raw.get("observed_at") else None,
            "source_context": raw.get("source_context"),
            "signal_direction": raw.get("signal_direction"),
            "signal_state": raw.get("signal_state"),
            "setup_score": raw.get("setup_score"),
            "risk_score": raw.get("risk_score"),
            "macro_state": macro.get("state"),
            "macro_bias": macro.get("bias"),
            "macro_confidence_score": macro.get("confidence_score"),
            "history_days": macro.get("history_days"),
            "history_years_approx": macro.get("history_years_approx"),
            "complete_3y": bool(macro.get("complete_3y")),
            "long_base_candidate": bool(macro.get("long_base_candidate")),
            "suggested_watch_horizon": macro.get("suggested_watch_horizon"),
            "relative_strength_90d_vs_btc_pct": _d(macro.get("features")).get("relative_strength_90d_vs_btc_pct"),
            "position_in_180d_range": _d(macro.get("features")).get("position_in_180d_range"),
            "accumulation_score": macro.get("accumulation_score"),
            "distribution_score": macro.get("distribution_score"),
            "source": macro.get("source"),
            "history_warning": macro.get("history_warning"),
        })

    items.sort(
        key=lambda item: (
            0 if item.get("long_base_candidate") else 1,
            0 if item.get("macro_state") in {"ACCUMULATION_LATE", "DISTRIBUTION_LATE"} else 1,
            -_f(item.get("macro_confidence_score")),
            -_f(item.get("setup_score")),
        )
    )
    return {
        "version": VERSION,
        "macro_version": MACRO_VERSION,
        "paper_only": True,
        "window_minutes": minutes,
        "rows": items[:max(1, min(limit, 100))],
        "radar_population": len(items),
        "long_base_candidates": sum(1 for item in items if item.get("long_base_candidate")),
        "complete_3y_histories": sum(1 for item in items if item.get("complete_3y")),
        "score_is_probability": False,
        "entry_authority": False,
        "independent_of_intraday_candidate_selection": True,
        "note": "Macro radar rotates through the liquid universe even when a coin is not an intraday candidate. It discovers slow accumulation/distribution context; lower-timeframe timing still decides entries.",
    }

