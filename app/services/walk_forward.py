from __future__ import annotations

from math import sqrt
from statistics import mean
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _wilson(wins: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = wins / total
    z2 = z * z
    center = p + z2 / (2 * total)
    margin = z * sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    denominator = 1 + z2 / total
    return max(0.0, (center - margin) / denominator) * 100.0


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [row for row in rows if row.get("outcome") in {"TP1_FIRST", "STOP_FIRST"}]
    wins = sum(1 for row in decided if row.get("outcome") == "TP1_FIRST")
    losses = len(decided) - wins
    rate = wins / len(decided) * 100.0 if decided else None
    rr = [_f(row.get("rr1")) for row in decided if row.get("rr1") is not None and _f(row.get("rr1")) > 0]
    avg_rr = mean(rr) if rr else None
    ev = None
    conservative_ev = None
    wilson = _wilson(wins, len(decided))
    if rate is not None and avg_rr is not None:
        p = rate / 100.0
        ev = p * avg_rr - (1.0 - p)
    if wilson is not None and avg_rr is not None:
        p = wilson / 100.0
        conservative_ev = p * avg_rr - (1.0 - p)
    return {
        "sample": len(decided),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": rate,
        "wilson_low_pct": wilson,
        "avg_rr1": avg_rr,
        "observed_ev_r": ev,
        "conservative_ev_r": conservative_ev,
    }


def _cohort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    direction = str(row.get("direction") or "N/D")
    regime = str(row.get("market_regime") or "N/D")
    score = row.get("early_context_score")
    value = _f(score, -999.0)
    bucket = "80-100" if value >= 80 else "65-79" if value >= 65 else "50-64" if value >= 50 else "<50_OR_ND"
    return direction, regime, bucket


def _cohort_reports(train: list[dict[str, Any]], test: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = sorted(set(_cohort_key(row) for row in train + test))
    reports: list[dict[str, Any]] = []
    for key in keys:
        train_rows = [row for row in train if _cohort_key(row) == key]
        test_rows = [row for row in test if _cohort_key(row) == key]
        train_summary = _summary(train_rows)
        test_summary = _summary(test_rows)
        train_usable = train_summary["sample"] >= 30
        test_usable = test_summary["sample"] >= 12
        stability = "LEARNING"
        if train_usable and test_usable:
            train_ev = train_summary.get("conservative_ev_r")
            test_ev = test_summary.get("observed_ev_r")
            if train_ev is not None and train_ev > 0 and test_ev is not None and test_ev > 0:
                stability = "HOLDS_OUT_OF_SAMPLE"
            elif train_ev is not None and train_ev > 0 and (test_ev is None or test_ev <= 0):
                stability = "FAILED_OUT_OF_SAMPLE"
            else:
                stability = "NOT_PROMISING_IN_TRAIN"
        reports.append({
            "direction": key[0],
            "market_regime": key[1],
            "context_bucket": key[2],
            "train": train_summary,
            "test": test_summary,
            "stability": stability,
            "can_influence_veto": train_usable and test_usable and stability == "FAILED_OUT_OF_SAMPLE",
            "can_create_entry": False,
        })
    reports.sort(key=lambda row: (row["test"]["sample"], row["train"]["sample"]), reverse=True)
    return reports[:50]


async def build_walk_forward_report(db: AsyncSession, limit: int = 1200) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT observed_at, direction, outcome,
                   NULLIF(metadata->>'reward_risk_tp1','')::numeric AS rr1,
                   COALESCE(metadata->>'market_regime','N/D') AS market_regime,
                   NULLIF(metadata->>'early_context_score','')::numeric AS early_context_score
            FROM verdict_memory
            WHERE outcome IN ('TP1_FIRST','STOP_FIRST')
            ORDER BY observed_at ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows = [dict(row) for row in result.mappings().all()]
    total = len(rows)
    if total < 60:
        return {
            "mode": "SHADOW_ONLY",
            "status": "LEARNING",
            "total_sample": total,
            "minimum_required": 60,
            "train": _summary(rows),
            "test": _summary([]),
            "cohorts": [],
            "rule": "Walk-forward stays inactive until at least 60 chronologically resolved outcomes exist.",
            "can_create_entry": False,
        }

    split = max(42, int(total * 0.70))
    split = min(split, total - 18)
    train = rows[:split]
    test = rows[split:]
    train_summary = _summary(train)
    test_summary = _summary(test)
    cohorts = _cohort_reports(train, test)

    train_ev = train_summary.get("conservative_ev_r")
    test_ev = test_summary.get("observed_ev_r")
    status = "MIXED"
    if train_ev is not None and train_ev > 0 and test_ev is not None and test_ev > 0:
        status = "HOLDS_OUT_OF_SAMPLE"
    elif train_ev is not None and train_ev > 0 and (test_ev is None or test_ev <= 0):
        status = "FAILED_OUT_OF_SAMPLE"
    elif train_ev is None or train_ev <= 0:
        status = "NO_TRAIN_EDGE"

    return {
        "mode": "SHADOW_ONLY",
        "status": status,
        "total_sample": total,
        "split_policy": "chronological_70_30",
        "train": train_summary,
        "test": test_summary,
        "cohorts": cohorts,
        "holding_cohorts": [row for row in cohorts if row["stability"] == "HOLDS_OUT_OF_SAMPLE"][:8],
        "failed_cohorts": [row for row in cohorts if row["stability"] == "FAILED_OUT_OF_SAMPLE"][:8],
        "can_create_entry": False,
        "rule": "Training always precedes testing chronologically. Test outcomes never tune their own historical thresholds.",
        "probability_note": "Out-of-sample rates are validation statistics, not guaranteed probabilities for the next trade.",
    }
