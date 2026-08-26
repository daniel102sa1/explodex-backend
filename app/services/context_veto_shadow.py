from __future__ import annotations

from math import sqrt
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _wilson_upper(wins: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = wins / total
    z2 = z * z
    center = p + z2 / (2 * total)
    margin = z * sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    denominator = 1 + z2 / total
    return min(1.0, (center + margin) / denominator) * 100.0


def _break_even_win_rate(avg_rr1: Any) -> float | None:
    rr = _f(avg_rr1)
    return 100.0 / (1.0 + rr) if rr > 0 else None


def _grade(score: float, usable: bool) -> str:
    if not usable:
        return "INSUFFICIENT_SAMPLE"
    if score >= 85:
        return "VERY_STRONG_CANDIDATE"
    if score >= 70:
        return "STRONG_CANDIDATE"
    if score >= 50:
        return "CAUTION"
    if score >= 30:
        return "WATCH"
    return "OBSERVE"


def _cohort_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("direction") or "N/D"),
        str(row.get("market_regime") or "N/D"),
        str(row.get("micro_bucket") or "N/D"),
        str(row.get("cascade_status") or "N/D"),
        str(row.get("exchange_status") or "N/D"),
        str(row.get("sequential_status") or "N/D"),
    )


def _rolling_index(rolling_report: dict[str, Any] | None) -> dict[tuple[str, ...], dict[str, Any]]:
    report = rolling_report or {}
    return {
        _cohort_key(row): row
        for row in (report.get("cohorts") or [])
        if isinstance(row, dict)
    }


def _assess_cohort(row: dict[str, Any], rolling: dict[str, Any] | None) -> dict[str, Any]:
    train = dict(row.get("train") or {})
    test = dict(row.get("test") or {})
    train_n = int(train.get("sample") or 0)
    test_n = int(test.get("sample") or 0)
    usable = train_n >= 30 and test_n >= 12
    evidence = 0.0
    reasons: list[str] = []

    if usable:
        if train.get("observed_ev_r") is not None and _f(train.get("observed_ev_r")) <= 0:
            evidence += 10
            reasons.append("train observed EV <= 0")
        if train.get("conservative_ev_r") is not None and _f(train.get("conservative_ev_r")) <= -0.10:
            evidence += 10
            reasons.append("train conservative EV <= -0.10R")
        if test.get("observed_ev_r") is not None and _f(test.get("observed_ev_r")) <= 0:
            evidence += 20
            reasons.append("later test observed EV <= 0")
        if test.get("conservative_ev_r") is not None and _f(test.get("conservative_ev_r")) <= 0:
            evidence += 10
            reasons.append("later test conservative EV <= 0")

        upper = _wilson_upper(int(test.get("wins") or 0), test_n)
        break_even = _break_even_win_rate(test.get("avg_rr1"))
        if upper is not None and break_even is not None and upper < break_even:
            evidence += 20
            reasons.append("Wilson upper bound is below break-even win rate")

        stability = str(row.get("stability") or "")
        if stability == "FAILED_OUT_OF_SAMPLE":
            evidence += 10
            reasons.append("failed chronological holdout")
        elif stability == "WEAK_IN_TRAIN":
            evidence += 5
            reasons.append("weak in training history")

        if rolling:
            if rolling.get("repeated_strong_failure"):
                evidence += 30
                reasons.append("failed strongly in repeated rolling windows")
            elif rolling.get("repeated_failure"):
                evidence += 20
                reasons.append("failed repeatedly in rolling windows")
            elif int(rolling.get("eligible_windows") or 0) >= 3:
                evidence -= 10
                reasons.append("rolling windows did not confirm persistent weakness")
        else:
            reasons.append("rolling validation not yet available for this cohort")
    else:
        reasons.append("minimum 30 train and 12 later test outcomes not reached")

    evidence = max(0.0, min(100.0, evidence))
    grade = _grade(evidence, usable)
    upper = _wilson_upper(int(test.get("wins") or 0), test_n)
    break_even = _break_even_win_rate(test.get("avg_rr1"))
    repeated_failure = bool(rolling and rolling.get("repeated_failure"))
    future_review = (
        usable
        and train_n >= 50
        and test_n >= 20
        and evidence >= 70
        and repeated_failure
        and test.get("observed_ev_r") is not None
        and _f(test.get("observed_ev_r")) <= 0
        and upper is not None
        and break_even is not None
        and upper < break_even
    )

    return {
        "direction": row.get("direction"),
        "market_regime": row.get("market_regime"),
        "micro_bucket": row.get("micro_bucket"),
        "cascade_status": row.get("cascade_status"),
        "exchange_status": row.get("exchange_status"),
        "sequential_status": row.get("sequential_status"),
        "stability": row.get("stability"),
        "train": train,
        "test": test,
        "rolling_validation": rolling,
        "evidence_score": round(evidence, 1),
        "grade": grade,
        "test_wilson_upper_pct": round(upper, 2) if upper is not None else None,
        "test_break_even_win_rate_pct": round(break_even, 2) if break_even is not None else None,
        "evidence_reasons": reasons,
        "eligible_for_future_veto_review": future_review,
        "veto_active": False,
        "can_create_entry": False,
        "can_raise_leverage": False,
    }


def build_graduated_veto_shadow(
    meta_report: dict[str, Any] | None,
    rolling_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = meta_report or {}
    rolling_by_key = _rolling_index(rolling_report)
    cohorts = [row for row in (meta.get("cohorts") or []) if isinstance(row, dict)]
    assessed = [_assess_cohort(row, rolling_by_key.get(_cohort_key(row))) for row in cohorts]
    assessed.sort(
        key=lambda row: (
            _f(row.get("evidence_score")),
            int((row.get("rolling_validation") or {}).get("failure_windows") or 0),
            int((row.get("test") or {}).get("sample") or 0),
        ),
        reverse=True,
    )
    strong = [row for row in assessed if row.get("grade") in {"STRONG_CANDIDATE", "VERY_STRONG_CANDIDATE"}]
    reviewable = [row for row in assessed if row.get("eligible_for_future_veto_review")]

    return {
        "mode": "SHADOW_ONLY",
        "status": "LEARNING" if meta.get("status") == "LEARNING" else "GRADING_WITH_ROLLING_VALIDATION",
        "source_meta_status": meta.get("status"),
        "rolling_status": (rolling_report or {}).get("status"),
        "total_sample": meta.get("total_sample", 0),
        "cohorts_assessed": len(assessed),
        "strong_candidates": strong[:12],
        "future_review_candidates": reviewable[:12],
        "top_ranked": assessed[:20],
        "veto_active": False,
        "veto_activation_allowed": False,
        "can_create_entry": False,
        "can_raise_leverage": False,
        "future_activation_requirements": {
            "minimum_train_sample": 50,
            "minimum_later_test_sample": 20,
            "requires_negative_test_ev": True,
            "requires_wilson_upper_below_break_even": True,
            "requires_repeated_walk_forward_failure": True,
            "minimum_eligible_rolling_windows": 3,
            "minimum_failure_fraction": "2/3",
            "requires_explicit_activation_in_code": True,
        },
        "rule": "A cohort can reach future-review status only after repeated weakness across independent forward windows. No veto is active.",
        "probability_note": "Historical rates, Wilson intervals, EV and rolling failures are validation evidence, not guaranteed probabilities.",
    }
