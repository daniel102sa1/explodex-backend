from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client
from app.services.macro_cycle_engine import VERSION as MACRO_VERSION, build_macro_cycle

VERSION = "macro_cycle_persistence_v1"
MAX_SYMBOLS = 12
CONCURRENCY = 3


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


async def persist_macro_cycle_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.setup_score, s.risk_score, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.setup_score DESC NULLS LAST, s.risk_score ASC NULLS LAST
        LIMIT :limit
    """), {"run_id": run_id, "limit": MAX_SYMBOLS})).mappings().all()]

    if not rows:
        return {"version": VERSION, "seen": 0, "updated": 0, "macro_version": MACRO_VERSION}

    try:
        btc_payload = await binance_client.historical_daily_klines("BTCUSDT", 1095)
        btc_rows = list(btc_payload.get("rows") or [])
    except Exception:
        btc_rows = []

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        symbol = str(row.get("symbol") or "").upper()
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
        return row, macro

    results = await asyncio.gather(*(one(row) for row in rows))

    updated = 0
    states: dict[str, int] = {}
    long_bases = 0
    full_3y = 0
    for row, macro in results:
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
        updated += 1
        state = str(macro.get("state") or "UNAVAILABLE")
        states[state] = states.get(state, 0) + 1
        long_bases += int(bool(macro.get("long_base_candidate")))
        full_3y += int(bool(macro.get("complete_3y")))

    await db.commit()
    return {
        "version": VERSION,
        "macro_version": MACRO_VERSION,
        "seen": len(rows),
        "updated": updated,
        "states": states,
        "long_base_candidates": long_bases,
        "complete_3y_histories": full_3y,
        "can_create_entry": False,
    }


async def macro_cycle_report(db: AsyncSession, *, minutes: int = 180, limit: int = 20) -> dict[str, Any]:
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT DISTINCT ON (s.symbol_id)
               sy.symbol, s.direction, s.state, s.setup_score, s.risk_score,
               s.created_at, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.created_at >= NOW() - (:minutes * INTERVAL '1 minute')
        ORDER BY s.symbol_id, s.created_at DESC
    """), {"minutes": max(30, min(minutes, 1440))})).mappings().all()]

    items: list[dict[str, Any]] = []
    for raw in rows:
        reason = _d(raw.get("reason"))
        heart = _d(reason.get("explodex_heart"))
        macro = _d(heart.get("macro_cycle")) or _d(reason.get("macro_cycle"))
        if not macro:
            continue
        items.append({
            "symbol": raw.get("symbol"),
            "signal_direction": raw.get("direction"),
            "signal_state": raw.get("state"),
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
        })

    items.sort(
        key=lambda item: (
            0 if item.get("long_base_candidate") else 1,
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
        "long_base_candidates": sum(1 for item in items if item.get("long_base_candidate")),
        "score_is_probability": False,
        "entry_authority": False,
        "note": "Macro radar discovers slow accumulation/distribution context; lower-timeframe timing still decides entries.",
    }
