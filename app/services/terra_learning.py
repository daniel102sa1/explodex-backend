from __future__ import annotations

"""Outcome memory for Terra leverage decisions.

Historical outcomes can reduce future exposure after enough observations.
Positive small samples never increase risk; this avoids self-reinforcing
overfitting while still letting Terra learn where leverage has hurt.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "terra_leverage_memory_v1"


async def load_terra_leverage_memory(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT
            COALESCE(metadata->'terra_risk_context'->>'tier', 'UNKNOWN') AS tier,
            COUNT(*)::int AS sample,
            COUNT(*) FILTER (WHERE net_pnl > 0)::int AS winners,
            COALESCE(AVG(net_pnl), 0) AS avg_net_pnl,
            COALESCE(SUM(net_pnl), 0) AS total_net_pnl,
            COALESCE(AVG(leverage), 0) AS avg_leverage
        FROM paper_positions
        WHERE status='CLOSED' AND grade='TERRA'
        GROUP BY 1
    """))).mappings().all()

    tiers: dict[str, Any] = {}
    for raw in rows:
        row = dict(raw)
        sample = int(row.get("sample") or 0)
        winners = int(row.get("winners") or 0)
        avg_pnl = float(row.get("avg_net_pnl") or 0)
        win_rate = (winners / sample * 100.0) if sample else None
        multiplier = 1.0
        reason = "insufficient_sample_no_adjustment"
        if sample >= 20:
            if avg_pnl < 0 and (win_rate is None or win_rate < 45.0):
                multiplier = 0.65
                reason = "mature_negative_tier_reduce_exposure"
            elif avg_pnl < 0:
                multiplier = 0.80
                reason = "negative_expectancy_reduce_exposure"
            else:
                multiplier = 1.0
                reason = "non_negative_history_no_extra_boost"
        tiers[str(row.get("tier") or "UNKNOWN").upper()] = {
            "sample": sample,
            "winners": winners,
            "win_rate_pct": round(win_rate, 2) if win_rate is not None else None,
            "avg_net_pnl": round(avg_pnl, 6),
            "total_net_pnl": round(float(row.get("total_net_pnl") or 0), 6),
            "avg_leverage": round(float(row.get("avg_leverage") or 0), 3),
            "risk_multiplier": multiplier,
            "reason": reason,
        }

    return {
        "version": VERSION,
        "paper_only": True,
        "tiers": tiers,
        "policy": {
            "minimum_sample_to_reduce": 20,
            "positive_history_can_raise_risk": False,
            "purpose": "learn_when_leverage_has_hurt_without_overfitting_small_samples",
        },
    }


def multiplier_for_tier(memory: dict[str, Any], tier: str) -> float:
    data = (memory.get("tiers") or {}).get(str(tier or "UNKNOWN").upper()) or {}
    try:
        value = float(data.get("risk_multiplier") or 1.0)
    except (TypeError, ValueError):
        value = 1.0
    return max(0.50, min(1.0, value))
