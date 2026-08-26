from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tp1_continuation_research import build_tp1_continuation_report


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _runner_state(summary: dict[str, Any], horizon: int) -> dict[str, Any]:
    sample = int(summary.get("sample") or 0)
    extra = _f(summary.get("avg_extra_r_after_tp1"))
    pullback = _f(summary.get("avg_pullback_from_tp1_r"))
    reach2 = _f(summary.get("reached_2r_pct"))
    reach3 = _f(summary.get("reached_3r_pct"))
    reach4 = _f(summary.get("reached_4r_pct"))
    held_be = _f(summary.get("held_beyond_entry_pct"))

    usable = sample >= 30
    strong_sample = sample >= 60

    # This is a technical research score, never a next-trade probability.
    score = 0.0
    score += min(30.0, max(0.0, extra) * 15.0)
    score += min(22.0, reach2 * 0.22)
    score += min(20.0, reach3 * 0.20)
    score += min(12.0, reach4 * 0.12)
    score += min(10.0, held_be * 0.10)
    score -= min(28.0, max(0.0, pullback) * 14.0)
    score = max(0.0, min(100.0, score))

    if not usable:
        state = "CALIBRATING"
    elif extra >= 0.75 and pullback <= 0.80 and reach3 >= 35:
        state = "RUNNER_PROMISING"
    elif extra >= 0.35 and pullback <= 1.10 and reach2 >= 55:
        state = "RUNNER_POSSIBLE"
    elif pullback >= 1.35 or (extra <= 0.15 and reach2 < 40):
        state = "PROTECT_PROFIT"
    else:
        state = "MIXED"

    return {
        "horizon_minutes": horizon,
        "sample": sample,
        "sample_status": "STRONG" if strong_sample else "USABLE" if usable else "CALIBRATING",
        "state": state,
        "runner_evidence_score": round(score, 1),
        "avg_extra_r_after_tp1": summary.get("avg_extra_r_after_tp1"),
        "avg_pullback_from_tp1_r": summary.get("avg_pullback_from_tp1_r"),
        "reached_2r_pct": summary.get("reached_2r_pct"),
        "reached_3r_pct": summary.get("reached_3r_pct"),
        "reached_4r_pct": summary.get("reached_4r_pct"),
        "held_beyond_entry_pct": summary.get("held_beyond_entry_pct"),
        "can_change_management": False,
    }


def _cohort_candidate(row: dict[str, Any]) -> dict[str, Any]:
    summary = _runner_state(row, int(row.get("horizon_minutes") or 0))
    return {
        "cohort": row.get("cohort"),
        **summary,
    }


async def build_runner_shadow_model(db: AsyncSession) -> dict[str, Any]:
    """Translate post-TP1 observations into a conservative runner hypothesis.

    It cannot manage a trade. It only identifies historical profiles where keeping
    a small runner after TP1 may deserve future paper validation.
    """
    continuation = await build_tp1_continuation_report(db)
    horizons = dict(continuation.get("horizons") or {})
    global_states = []
    for key, summary in sorted(horizons.items(), key=lambda item: int(item[0])):
        if isinstance(summary, dict):
            global_states.append(_runner_state(summary, int(key)))

    cohort_states = [
        _cohort_candidate(row)
        for row in (continuation.get("cohorts") or [])
        if isinstance(row, dict)
    ]
    usable = [row for row in cohort_states if int(row.get("sample") or 0) >= 30]
    promising = [
        row for row in usable
        if row.get("state") in {"RUNNER_PROMISING", "RUNNER_POSSIBLE"}
    ]
    defensive = [row for row in usable if row.get("state") == "PROTECT_PROFIT"]
    promising.sort(key=lambda row: (-_f(row.get("runner_evidence_score")), -int(row.get("sample") or 0)))
    defensive.sort(key=lambda row: (-int(row.get("sample") or 0), _f(row.get("runner_evidence_score"))))

    preferred_horizon = None
    candidates = [row for row in global_states if row.get("state") == "RUNNER_PROMISING"]
    if not candidates:
        candidates = [row for row in global_states if row.get("state") == "RUNNER_POSSIBLE"]
    if candidates:
        candidates.sort(key=lambda row: (-_f(row.get("runner_evidence_score")), int(row.get("horizon_minutes") or 9999)))
        preferred_horizon = candidates[0].get("horizon_minutes")

    return {
        "mode": "SHADOW_ONLY",
        "version": "runner_shadow_v1",
        "global_horizons": global_states,
        "preferred_horizon_minutes": preferred_horizon,
        "promising_cohorts": promising[:12],
        "protect_profit_cohorts": defensive[:12],
        "usable_cohorts": len(usable),
        "can_change_management": False,
        "can_move_stop": False,
        "can_create_entry": False,
        "rule": (
            "Runner hypotheses require at least 30 comparable post-TP1 samples. "
            "Even then they remain shadow-only until repeated out-of-sample validation shows better expectancy without excessive giveback."
        ),
        "score_note": "runner_evidence_score is a 0-100 research score, not a probability of continuation.",
        "probability_note": "Historical continuation rates do not guarantee the next trade.",
    }
