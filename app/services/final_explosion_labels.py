from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.explosion_intelligence import VERSION, classify_horizons


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


async def finalize_explosion_outcomes(db: AsyncSession, limit: int = 250) -> dict[str, Any]:
    """Create training labels only after the 24h horizon is available.

    Short-horizon observations remain useful for monitoring, but they are not
    promoted to permanent training truth before 4h/6h/24h have had time to
    reveal delayed explosions, reversals and fake breakouts.
    """
    rows = (await db.execute(text("""
        SELECT id::text, direction, metadata
        FROM verdict_memory
        WHERE metadata ? 'horizon_outcomes'
          AND (metadata->'horizon_outcomes') ? '24h'
          AND COALESCE(metadata->>'explosion_label_version','') <> :version
        ORDER BY observed_at ASC
        LIMIT :limit
    """), {"version": VERSION, "limit": max(10, min(int(limit), 500))})).mappings().all()

    updated = 0
    labels: defaultdict[str, int] = defaultdict(int)
    for raw in rows:
        row = dict(raw)
        metadata = _d(row.get("metadata"))
        outcomes = _d(metadata.get("horizon_outcomes"))
        if "24h" not in outcomes:
            continue
        result = classify_horizons(str(row.get("direction") or ""), outcomes)
        if not result:
            continue
        metadata["explosion_label_version"] = VERSION
        metadata["explosion_label_maturity"] = "FINAL_24H"
        metadata["explosion_label"] = result["label"]
        metadata["timing_quality"] = result["timing_quality"]
        metadata["explosion_evaluation"] = result
        await db.execute(text("""
            UPDATE verdict_memory
            SET metadata=CAST(:metadata AS JSONB), evaluated_at=NOW()
            WHERE id=CAST(:id AS UUID)
        """), {"id": row["id"], "metadata": json.dumps(metadata)})
        labels[result["label"]] += 1
        updated += 1

    await db.commit()
    return {
        "seen": len(rows),
        "updated": updated,
        "labels": dict(labels),
        "maturity": "FINAL_24H",
        "rule": "Only 24h-complete observations become timing-model training labels.",
    }
