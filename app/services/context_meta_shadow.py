from __future__ import annotations

from collections import defaultdict
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


def _bucket(value: Any, cuts: tuple[float, float]) -> str:
    if value is None:
        return "N/D"
    number = _f(value, -999.0)
    if number >= cuts[1]:
        return "HIGH"
    if number >= cuts[0]:
        return "MID"
    return "LOW"


def _context_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("direction") or "N/D"),
        str(row.get("market_regime") or "N/D"),
        _bucket(row.get("microstructure_score"), (45.0, 65.0)),
        str(row.get("cascade_status") or "N/D"),
        str(row.get("exchange_status") or "N/D"),
        "SEQ_READY" if row.get("sequential_ready") is True else "SEQ_WARM" if row.get("sequential_ready") is False else "SEQ_ND",
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [row for row in rows if row.get("outcome") in {"TP1_FIRST", "STOP_FIRST"}]
    wins = sum(1 for row in decided if row.get("outcome") == "TP1_FIRST")
    losses = len(decided) - wins
    rr = [_f(row.get("rr1")) for row in decided if row.get("rr1") is not None and _f(row.get("rr1")) > 0]
    avg_rr = mean(rr) if rr else None
    rate = wins / len(decided) * 100.0 if decided else None
    wilson = _wilson(wins, len(decided))
    observed_ev = None
    conservative_ev = None
    if rate is not None and avg_rr is not None:
        p = rate / 100.0
        observed_ev = p * avg_rr - (1.0 - p)
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
        "observed_ev_r": observed_ev,
        "conservative_ev_r": conservative_ev,
    }


def _cohorts(train: list[dict[str, Any]], test: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped_train: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    grouped_test: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in train:
        grouped_train[_context_key(row)].append(row)
    for row in test:
        grouped_test[_context_key(row)].append(row)

    reports: list[dict[str, Any]] = []
    for key in sorted(set(grouped_train) | set(grouped_test)):
        train_summary = _summary(grouped_train.get(key, []))
        test_summary = _summary(grouped_test.get(key, []))
        train_usable = train_summary["sample"] >= 30
        test_usable = test_summary["sample"] >= 12
        stability = "LEARNING"
        if train_usable and test_usable:
            train_ev = train_summary.get("conservative_ev_r")
            test_ev = test_summary.get("observed_ev_r")
            if train_ev is not None and train_ev > 0.10 and test_ev is not None and test_ev > 0:
                stability = "HOLDS_OUT_OF_SAMPLE"
            elif train_ev is not None and train_ev > 0.10 and (test_ev is None or test_ev <= 0):
                stability = "FAILED_OUT_OF_SAMPLE"
            elif train_ev is not None and train_ev <= 0:
                stability = "WEAK_IN_TRAIN"
            else:
                stability = "MIXED"

        reports.append({
            "direction": key[0],
            "market_regime": key[1],
            "micro_bucket": key[2],
            "cascade_status": key[3],
            "exchange_status": key[4],
            "sequential_status": key[5],
            "train": train_summary,
            "test": test_summary,
            "stability": stability,
            "veto_candidate": train_usable and test_usable and stability in {"FAILED_OUT_OF_SAMPLE", "WEAK_IN_TRAIN"},
            "can_create_entry": False,
            "can_raise_leverage": False,
        })

    reports.sort(key=lambda row: (row["test"]["sample"], row["train"]["sample"]), reverse=True)
    return reports[:80]


async def build_context_meta_shadow_report(db: AsyncSession, limit: int = 1800) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT observed_at, direction, outcome,
                   NULLIF(metadata->>'reward_risk_tp1','')::numeric AS rr1,
                   COALESCE(metadata->>'market_regime','N/D') AS market_regime,
                   NULLIF(metadata->>'microstructure_score','')::numeric AS microstructure_score,
                   COALESCE(metadata->>'cascade_status','N/D') AS cascade_status,
                   COALESCE(metadata->>'exchange_leadlag_status','N/D') AS exchange_status,
                   CASE
                     WHEN metadata->>'sequential_ready' = 'true' THEN TRUE
                     WHEN metadata->>'sequential_ready' = 'false' THEN FALSE
                     ELSE NULL
                   END AS sequential_ready
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
    if total < 80:
        return {
            "mode": "SHADOW_ONLY",
            "status": "LEARNING",
            "total_sample": total,
            "minimum_required": 80,
            "train": _summary(rows),
            "test": _summary([]),
            "cohorts": [],
            "veto_candidates": [],
            "holding_cohorts": [],
            "can_create_entry": False,
            "can_raise_leverage": False,
            "rule": "Combined context meta-model stays descriptive until at least 80 chronologically resolved outcomes exist.",
        }

    split = max(56, int(total * 0.70))
    split = min(split, total - 24)
    train = rows[:split]
    test = rows[split:]
    cohorts = _cohorts(train, test)
    train_summary = _summary(train)
    test_summary = _summary(test)

    status = "MIXED"
    train_ev = train_summary.get("conservative_ev_r")
    test_ev = test_summary.get("observed_ev_r")
    if train_ev is not None and train_ev > 0 and test_ev is not None and test_ev > 0:
        status = "HOLDS_OUT_OF_SAMPLE"
    elif train_ev is not None and train_ev > 0 and (test_ev is None or test_ev <= 0):
        status = "FAILED_OUT_OF_SAMPLE"
    elif train_ev is None or train_ev <= 0:
        status = "NO_GLOBAL_EDGE"

    veto_candidates = [row for row in cohorts if row["veto_candidate"]][:12]
    holding = [row for row in cohorts if row["stability"] == "HOLDS_OUT_OF_SAMPLE"][:12]

    return {
        "mode": "SHADOW_ONLY",
        "status": status,
        "total_sample": total,
        "split_policy": "chronological_70_30",
        "feature_family": ["regime", "microstructure", "liquidation_cascade", "exchange_leadlag", "sequential_book"],
        "train": train_summary,
        "test": test_summary,
        "cohorts": cohorts,
        "veto_candidates": veto_candidates,
        "holding_cohorts": holding,
        "can_create_entry": False,
        "can_raise_leverage": False,
        "veto_activation_allowed": False,
        "rule": "A weak cohort is only a shadow veto candidate after >=30 train and >=12 later test outcomes. This report does not alter live decisions.",
        "probability_note": "Historical and out-of-sample rates are calibration evidence, not a probability guarantee for the next setup.",
    }
