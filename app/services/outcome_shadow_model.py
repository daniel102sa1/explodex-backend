from __future__ import annotations

from math import sqrt
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _wilson(wins: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = wins / total
    z2 = z * z
    center = p + z2 / (2 * total)
    margin = z * sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    denominator = 1 + z2 / total
    return max(0.0, (center - margin) / denominator) * 100.0


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    sample = int(row.get("sample") or 0)
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    rate = wins / sample * 100.0 if sample else None
    wilson = _wilson(wins, sample)
    avg_rr1 = float(row.get("avg_rr1")) if row.get("avg_rr1") is not None else None
    observed_ev_r = None
    conservative_ev_r = None
    if rate is not None and avg_rr1 is not None:
        p = rate / 100.0
        observed_ev_r = p * avg_rr1 - (1.0 - p)
    if wilson is not None and avg_rr1 is not None:
        p_low = wilson / 100.0
        conservative_ev_r = p_low * avg_rr1 - (1.0 - p_low)

    state = "LEARNING"
    if sample >= 30:
        if conservative_ev_r is not None and conservative_ev_r > 0.10 and (wilson or 0) >= 40:
            state = "PROMISING"
        elif observed_ev_r is not None and observed_ev_r <= 0:
            state = "WEAK"
        else:
            state = "MIXED"

    return {
        **row,
        "sample": sample,
        "wins": wins,
        "losses": losses,
        "observed_tp1_first_pct": rate,
        "wilson_low_pct": wilson,
        "observed_ev_r": observed_ev_r,
        "conservative_ev_r": conservative_ev_r,
        "state": state,
        "can_influence_veto": sample >= 30 and state == "WEAK",
        "can_create_entry": False,
    }


async def build_tp1_stop_shadow_report(db: AsyncSession) -> dict[str, Any]:
    """Evaluate TP1-first vs STOP-first in shadow mode without touching live decisions."""
    global_result = await db.execute(
        text(
            """
            SELECT COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS sample,
                   COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                   COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                   AVG(NULLIF(metadata->>'reward_risk_tp1','')::numeric)
                     FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_rr1,
                   AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                   AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct
            FROM verdict_memory
            """
        )
    )
    global_row = _normalize(dict(global_result.mappings().one()))

    cohort_result = await db.execute(
        text(
            """
            SELECT direction,
                   COALESCE(metadata->>'market_regime','N/D') AS market_regime,
                   CASE
                     WHEN NULLIF(metadata->>'early_context_score','')::numeric >= 80 THEN '80-100'
                     WHEN NULLIF(metadata->>'early_context_score','')::numeric >= 65 THEN '65-79'
                     WHEN NULLIF(metadata->>'early_context_score','')::numeric >= 50 THEN '50-64'
                     ELSE '<50_OR_ND'
                   END AS context_bucket,
                   COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS sample,
                   COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                   COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                   AVG(NULLIF(metadata->>'reward_risk_tp1','')::numeric)
                     FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_rr1,
                   AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                   AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct
            FROM verdict_memory
            GROUP BY direction, market_regime, context_bucket
            HAVING COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) > 0
            ORDER BY sample DESC
            LIMIT 40
            """
        )
    )
    cohorts = [_normalize(dict(row)) for row in cohort_result.mappings().all()]

    usable = [row for row in cohorts if row["sample"] >= 30]
    promising = sorted(
        [row for row in usable if row["state"] == "PROMISING"],
        key=lambda item: (item.get("conservative_ev_r") or -999, item["sample"]),
        reverse=True,
    )[:8]
    weak = sorted(
        [row for row in usable if row["state"] == "WEAK"],
        key=lambda item: (item.get("observed_ev_r") or 999, -item["sample"]),
    )[:8]

    return {
        "mode": "SHADOW_ONLY",
        "target": "TP1_FIRST_vs_STOP_FIRST",
        "global": global_row,
        "cohorts": cohorts,
        "promising_cohorts": promising,
        "weak_cohorts": weak,
        "usable_cohorts": len(usable),
        "rule": "Below 30 decided comparable outcomes the model stays LEARNING. It never creates an entry or raises leverage.",
        "probability_note": "Observed rates and Wilson bounds are empirical calibration statistics, not certainty for the next trade.",
    }
