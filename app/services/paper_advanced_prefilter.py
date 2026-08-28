from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.advanced_entry_lab import evaluate_advanced_entry


async def prefilter_new_micro_signals(db: AsyncSession, *, limit: int = 10) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT id, symbol, side, score, setup_type, tier, metadata
        FROM paper_micro_signals
        WHERE status='NEW'
          AND observed_at >= NOW() - INTERVAL '10 minutes'
        ORDER BY CASE WHEN tier='STANDARD' THEN 0 ELSE 1 END, score DESC, observed_at ASC
        LIMIT :limit
    """), {"limit": max(1, min(limit, 30))})).mappings().all()

    evaluated = 0
    vetoed = 0
    conflicts = 0
    supported = 0
    errors = 0

    for raw in rows:
        row = dict(raw)
        try:
            advanced = await evaluate_advanced_entry(
                db,
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                setup_type=str(row.get("setup_type") or "GENERAL"),
                score=float(row.get("score") or 0.0),
            )
            evaluated += 1
            state = str(advanced.get("state") or "MIXED")
            if state == "VETO":
                vetoed += 1
            elif state == "CONFLICT":
                conflicts += 1
            elif state in {"SUPPORT", "STRONG_SUPPORT"}:
                supported += 1

            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}
            metadata = dict(metadata)
            metadata["advanced_entry"] = advanced

            if bool(advanced.get("veto")):
                await db.execute(text("""
                    UPDATE paper_micro_signals
                    SET status='SKIPPED', skip_reason='advanced_entry_veto', metadata=CAST(:metadata AS JSONB)
                    WHERE id=:id AND status='NEW'
                """), {"id": row["id"], "metadata": json.dumps(metadata)})
            else:
                await db.execute(text("""
                    UPDATE paper_micro_signals
                    SET metadata=CAST(:metadata AS JSONB)
                    WHERE id=:id AND status='NEW'
                """), {"id": row["id"], "metadata": json.dumps(metadata)})
        except Exception:
            errors += 1
            await db.rollback()

    await db.commit()
    return {
        "version": "advanced_micro_prefilter_v1",
        "paper_only": True,
        "evaluated": evaluated,
        "vetoed": vetoed,
        "conflicts": conflicts,
        "supported": supported,
        "errors": errors,
        "creates_entry": False,
    }
