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
    if rr <= 0:
        return None
    return 100.0 / (1.0 + rr)


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


def _assess_cohort(row: dict[str, Any]) -> dict[str, Any]:
    train = dict(row.get("train") or {})
    test = dict(row.get("test") or {})
    train_n = int(train.get("sample") or 0)
    test_n = int(test.get("sample") or 0)
    train_usable = train_n >= 30
    test_usable = test_n >= 12
    usable = train_usable and test_usable

    evidence = 0.0
    reasons: list[str] = []

    train_ev = train.get("observed_ev_r")
    train_cons_ev = train.get("conservative_ev_r")
    test_ev = test.get("observed_ev_r")
    test_cons_ev = test.get("conservative_ev_r")

    if usable:
        if train_ev is not None and _f(train_ev) <= 0:
            evidence += 15
            reasons.append("train observed EV <= 0")
        if train_cons_ev is not None and _f(train_cons_ev) <= -0.10:
            evidence += 15
            reasons.append("train conservative EV <= -0.10R")
        if test_ev is not None and _f(test_ev) <= 0:
            evidence += 25
            reasons.append("later test observed EV <= 0")
        if test_cons_ev is not None and _f(test_cons_ev) <= 0:
            evidence += 15
            reasons.append("later test conservative EV <= 0")

        test_wins = int(test.get("wins") or 0)
        upper = _wilson_upper(test_wins, test_n)
        break_even = _break_even_win_rate(test.get("avg_rr1"))
        test_rate = test.get("win_rate_pct")
        if upper is not None and break_even is not None and upper < break_even:
            evidence += 20
            reasons.append("even Wilson upper bound is below break-even win rate")
        elif test_rate is not None and break_even is not None and _f(test_rate) + 10 < break_even:
            evidence += 10
            reasons.append("test win rate is materially below break-even")

        stability = str(row.get("stability") or "")
        if stability == "FAILED_OUT_OF_SAMPLE":
            evidence += 15
            reasons.append("pattern failed chronologically out of sample")
        elif stability == "WEAK_IN_TRAIN":
            evidence += 10
            reasons.append("pattern is weak in training history")
    else:
        reasons.append("minimum 30 train and 12 later test outcomes not reached")

    evidence = max(0.0, min(100.0, evidence))
    grade = _grade(evidence, usable)
    test_upper = _wilson_upper(int(test.get("wins") or 0), test_n)
    break_even = _break_even_win_rate(test.get("avg_rr1"))

    future_review = usable and evidence >= 70 and str(row.get("stability") or "") in {
        "FAILED_OUT_OF_SAMPLE",
        "WEAK_IN_TRAIN",
    }

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
        "evidence_score": round(evidence, 1),
        "grade": grade,
        "test_wilson_upper_pct": round(test_upper, 2) if test_upper is not None else None,
        "test_break_even_win_rate_pct": round(break_even, 2) if break_even is not None else None,
        "evidence_reasons": reasons,
        "eligible_for_future_veto_review": future_review,
        "veto_active": False,
        "can_create_entry": False,
        "can_raise_leverage": False,
    }


def build_graduated_veto_shadow(meta_report: dict[str, Any] | None) -> dict[str, Any]:
    """Convert combined context cohorts into graded veto evidence without activating a veto.

    This layer is intentionally non-operative. It ranks weak cohorts by statistical
    evidence so a future version can require much stronger proof before any live
    decision is altered.
    """
    meta = meta_report or {}
    cohorts = [row for row in (meta.get("cohorts") or []) if isinstance(row, dict)]
    assessed = [_assess_cohort(row) for row in cohorts]
    assessed.sort(
        key=lambda row: (
            _f(row.get("evidence_score")),
            int((row.get("test") or {}).get("sample") or 0),
            int((row.get("train") or {}).get("sample") or 0),
        ),
        reverse=True,
    )

    strong = [row for row in assessed if row.get("grade") in {"STRONG_CANDIDATE", "VERY_STRONG_CANDIDATE"}]
    caution = [row for row in assessed if row.get("grade") == "CAUTION"]

    return {
        "mode": "SHADOW_ONLY",
        "status": "LEARNING" if meta.get("status") == "LEARNING" else "GRADING",
        "source_meta_status": meta.get("status"),
        "total_sample": meta.get("total_sample", 0),
        "cohorts_assessed": len(assessed),
        "strong_candidates": strong[:12],
        "caution_candidates": caution[:12],
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
            "requires_explicit_activation_in_code": True,
        },
        "rule": "Evidence is graded, not activated. A strong candidate still cannot block an ExplodeX entry in this version.",
        "probability_note": "Historical rates, Wilson intervals and EV are validation evidence, not guaranteed probabilities for the next setup.",
    }
