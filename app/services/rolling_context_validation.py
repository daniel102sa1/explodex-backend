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


def _wilson(wins: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    z2 = z * z
    center = p + z2 / (2 * total)
    margin = z * sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    denominator = 1 + z2 / total
    low = max(0.0, (center - margin) / denominator) * 100.0
    high = min(1.0, (center + margin) / denominator) * 100.0
    return low, high


def _bucket(value: Any, cuts: tuple[float, float]) -> str:
    if value is None:
        return "N/D"
    number = _f(value, -999.0)
    if number >= cuts[1]:
        return "HIGH"
    if number >= cuts[0]:
        return "MID"
    return "LOW"


def _key(row: dict[str, Any]) -> tuple[str, ...]:
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
    low, high = _wilson(wins, len(decided))
    observed_ev = None
    conservative_ev = None
    break_even = None
    if avg_rr is not None and avg_rr > 0:
        break_even = 100.0 / (1.0 + avg_rr)
        if rate is not None:
            p = rate / 100.0
            observed_ev = p * avg_rr - (1.0 - p)
        if low is not None:
            p = low / 100.0
            conservative_ev = p * avg_rr - (1.0 - p)
    return {
        "sample": len(decided),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": rate,
        "wilson_low_pct": low,
        "wilson_upper_pct": high,
        "avg_rr1": avg_rr,
        "break_even_win_rate_pct": break_even,
        "observed_ev_r": observed_ev,
        "conservative_ev_r": conservative_ev,
    }


def _window_verdict(train: dict[str, Any], test: dict[str, Any]) -> str:
    if int(train.get("sample") or 0) < 30 or int(test.get("sample") or 0) < 10:
        return "INSUFFICIENT_COHORT_SAMPLE"

    test_ev = test.get("observed_ev_r")
    train_cons_ev = train.get("conservative_ev_r")
    upper = test.get("wilson_upper_pct")
    break_even = test.get("break_even_win_rate_pct")

    statistically_below_break_even = (
        upper is not None and break_even is not None and _f(upper) < _f(break_even)
    )
    if test_ev is not None and _f(test_ev) <= 0 and statistically_below_break_even:
        return "FAILED_STRONGLY"
    if test_ev is not None and _f(test_ev) <= 0:
        return "FAILED"
    if train_cons_ev is not None and _f(train_cons_ev) > 0 and test_ev is not None and _f(test_ev) > 0:
        return "HELD"
    return "MIXED"


def _make_windows(total: int) -> list[tuple[int, int]]:
    """Return expanding-train / forward-test boundaries.

    Four non-overlapping forward test windows are used. For the minimum 120 cases,
    this produces 60 initial train + four later 15-case tests. With more history,
    both train and test windows expand proportionally.
    """
    if total < 120:
        return []
    test_size = max(15, total // 8)
    initial_train = total - test_size * 4
    if initial_train < 60:
        test_size = max(15, (total - 60) // 4)
        initial_train = total - test_size * 4
    windows: list[tuple[int, int]] = []
    start = max(60, initial_train)
    for _ in range(4):
        end = min(total, start + test_size)
        if end <= start:
            break
        windows.append((start, end))
        start = end
    if windows and windows[-1][1] < total:
        last_start, _ = windows[-1]
        windows[-1] = (last_start, total)
    return windows


def _cohort_payload(key: tuple[str, ...]) -> dict[str, str]:
    return {
        "direction": key[0],
        "market_regime": key[1],
        "micro_bucket": key[2],
        "cascade_status": key[3],
        "exchange_status": key[4],
        "sequential_status": key[5],
    }


async def build_rolling_context_validation(db: AsyncSession, limit: int = 1800) -> dict[str, Any]:
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
    windows = _make_windows(total)
    if not windows:
        return {
            "mode": "SHADOW_ONLY",
            "status": "LEARNING",
            "total_sample": total,
            "minimum_required": 120,
            "window_count": 0,
            "windows": [],
            "cohorts": [],
            "persistent_failures": [],
            "persistent_holds": [],
            "veto_activation_allowed": False,
            "rule": "Rolling validation starts at 120 resolved outcomes and never changes live entries in this version.",
        }

    all_keys = sorted(set(_key(row) for row in rows))
    cohort_windows: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    window_reports: list[dict[str, Any]] = []

    for index, (test_start, test_end) in enumerate(windows, start=1):
        train_rows = rows[:test_start]
        test_rows = rows[test_start:test_end]
        global_train = _summary(train_rows)
        global_test = _summary(test_rows)
        window_reports.append({
            "window": index,
            "train_end_index": test_start,
            "test_start_index": test_start,
            "test_end_index": test_end,
            "train": global_train,
            "test": global_test,
        })

        train_groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        test_groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in train_rows:
            train_groups[_key(row)].append(row)
        for row in test_rows:
            test_groups[_key(row)].append(row)

        for key in all_keys:
            train_summary = _summary(train_groups.get(key, []))
            test_summary = _summary(test_groups.get(key, []))
            verdict = _window_verdict(train_summary, test_summary)
            cohort_windows[key].append({
                "window": index,
                "train": train_summary,
                "test": test_summary,
                "verdict": verdict,
            })

    cohort_reports: list[dict[str, Any]] = []
    for key, reports in cohort_windows.items():
        eligible = [r for r in reports if r["verdict"] != "INSUFFICIENT_COHORT_SAMPLE"]
        failures = [r for r in eligible if r["verdict"] in {"FAILED", "FAILED_STRONGLY"}]
        strong_failures = [r for r in eligible if r["verdict"] == "FAILED_STRONGLY"]
        holds = [r for r in eligible if r["verdict"] == "HELD"]
        eligible_n = len(eligible)
        fail_rate = len(failures) / eligible_n if eligible_n else 0.0
        hold_rate = len(holds) / eligible_n if eligible_n else 0.0

        repeated_failure = eligible_n >= 3 and len(failures) >= 2 and fail_rate >= 2 / 3
        repeated_strong_failure = eligible_n >= 3 and len(strong_failures) >= 2 and fail_rate >= 2 / 3
        repeated_hold = eligible_n >= 3 and len(holds) >= 2 and hold_rate >= 2 / 3

        cohort_reports.append({
            **_cohort_payload(key),
            "eligible_windows": eligible_n,
            "failure_windows": len(failures),
            "strong_failure_windows": len(strong_failures),
            "hold_windows": len(holds),
            "failure_rate": round(fail_rate, 3) if eligible_n else None,
            "hold_rate": round(hold_rate, 3) if eligible_n else None,
            "repeated_failure": repeated_failure,
            "repeated_strong_failure": repeated_strong_failure,
            "repeated_hold": repeated_hold,
            "windows": reports,
            "can_activate_veto": False,
        })

    cohort_reports.sort(
        key=lambda row: (
            bool(row.get("repeated_strong_failure")),
            bool(row.get("repeated_failure")),
            int(row.get("failure_windows") or 0),
            int(row.get("eligible_windows") or 0),
        ),
        reverse=True,
    )
    persistent_failures = [row for row in cohort_reports if row.get("repeated_failure")][:15]
    persistent_holds = [row for row in cohort_reports if row.get("repeated_hold")][:15]

    return {
        "mode": "SHADOW_ONLY",
        "status": "ROLLING_VALIDATION",
        "total_sample": total,
        "window_policy": "4_expanding_train_non_overlapping_forward_tests",
        "window_count": len(windows),
        "windows": window_reports,
        "cohorts": cohort_reports[:120],
        "persistent_failures": persistent_failures,
        "persistent_holds": persistent_holds,
        "veto_activation_allowed": False,
        "can_create_entry": False,
        "can_raise_leverage": False,
        "rule": "A repeated failure requires at least 3 eligible rolling windows and failure in at least two-thirds of them. It is still shadow-only.",
        "probability_note": "Rolling historical performance is validation evidence, not a guaranteed probability for the next setup.",
    }
