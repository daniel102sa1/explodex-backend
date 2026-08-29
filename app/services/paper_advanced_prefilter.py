from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.advanced_entry_lab import evaluate_advanced_entry
from app.services.paper_loss_autopsy import evaluate_anti_loss_gate


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
    anti_loss_vetoed = 0
    anti_loss_reduced = 0
    errors = 0

    for raw in rows:
        row = dict(raw)
        try:
            symbol = str(row["symbol"])
            side = str(row["side"])
            setup_type = str(row.get("setup_type") or "GENERAL")
            score = float(row.get("score") or 0.0)

            advanced = await evaluate_advanced_entry(
                db,
                symbol=symbol,
                side=side,
                setup_type=setup_type,
                score=score,
            )
            anti_loss = await evaluate_anti_loss_gate(
                db,
                symbol=symbol,
                side=side,
                strategy_mode="MICRO_SCALP",
                setup_type=setup_type,
                score=score,
            )
            evaluated += 1

            state = str(advanced.get("state") or "MIXED")
            if state == "VETO":
                vetoed += 1
            elif state == "CONFLICT":
                conflicts += 1
            elif state in {"SUPPORT", "STRONG_SUPPORT"}:
                supported += 1

            if bool(anti_loss.get("veto")):
                anti_loss_vetoed += 1
            elif float(anti_loss.get("risk_multiplier") or 1.0) < 1.0:
                anti_loss_reduced += 1

            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}
            metadata = dict(metadata)
            metadata["advanced_entry"] = advanced
            metadata["anti_loss_gate"] = anti_loss

            if bool(advanced.get("veto")) or bool(anti_loss.get("veto")):
                skip_reason = "anti_loss_veto" if bool(anti_loss.get("veto")) else "advanced_entry_veto"
                await db.execute(text("""
                    UPDATE paper_micro_signals
                    SET status='SKIPPED', skip_reason=:skip_reason, metadata=CAST(:metadata AS JSONB)
                    WHERE id=:id AND status='NEW'
                """), {"id": row["id"], "skip_reason": skip_reason, "metadata": json.dumps(metadata)})
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
        "version": "advanced_micro_prefilter_v2_anti_loss",
        "paper_only": True,
        "evaluated": evaluated,
        "vetoed": vetoed,
        "conflicts": conflicts,
        "supported": supported,
        "anti_loss_vetoed": anti_loss_vetoed,
        "anti_loss_reduced": anti_loss_reduced,
        "errors": errors,
        "creates_entry": False,
    }
