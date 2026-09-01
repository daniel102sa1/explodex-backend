from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.elliott_structure import analyze_symbol_elliott

VERSION = "elliott_persistence_v1"
MAX_SYMBOLS_PER_RUN = 10


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


async def persist_elliott_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.setup_score, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.setup_score DESC NULLS LAST, s.created_at DESC
        LIMIT :limit
    """), {"run_id": run_id, "limit": MAX_SYMBOLS_PER_RUN})).mappings().all()

    semaphore = asyncio.Semaphore(4)

    async def one(symbol: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                return symbol, await analyze_symbol_elliott(symbol)
            except Exception as exc:
                return symbol, {
                    "version": "elliott_structure_v1",
                    "symbol": symbol,
                    "status": "ERROR",
                    "best": None,
                    "errors": [f"{type(exc).__name__}:{str(exc)[:180]}"],
                    "creates_entry": False,
                    "changes_direction": False,
                }

    results = dict(await asyncio.gather(*(one(str(r["symbol"]).upper()) for r in rows)))
    updated = 0
    clear = 0
    aligned = 0
    conflicted = 0

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart:
            continue
        elliott = results.get(str(row["symbol"]).upper()) or {}
        best = _d(elliott.get("best"))
        contract = _d(heart.get("execution_contract"))
        primary_direction = str(contract.get("primary_direction") or heart.get("direction") or "").upper()
        best_direction = str(best.get("direction") or "").upper()
        is_clear = str(elliott.get("status") or "") == "CLEAR_COUNT" and bool(best)
        is_aligned = is_clear and best_direction == primary_direction
        is_conflict = is_clear and best_direction in {"LONG", "SHORT"} and primary_direction in {"LONG", "SHORT"} and best_direction != primary_direction

        elliott["aligned_with_primary_direction"] = is_aligned
        elliott["conflicts_with_primary_direction"] = is_conflict
        heart["elliott_structure"] = elliott
        contract["elliott_structure"] = elliott
        heart["execution_contract"] = contract
        reason["explodex_heart"] = heart
        if prediction:
            prediction["explodex_heart"] = heart
            reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals SET reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {"signal_id": row["signal_id"], "reason": json.dumps(reason)})
        updated += 1
        if is_clear:
            clear += 1
        if is_aligned:
            aligned += 1
        if is_conflict:
            conflicted += 1

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "updated": updated,
        "clear_counts": clear,
        "aligned": aligned,
        "conflicted": conflicted,
        "max_symbols_per_run": MAX_SYMBOLS_PER_RUN,
        "creates_entry": False,
    }
