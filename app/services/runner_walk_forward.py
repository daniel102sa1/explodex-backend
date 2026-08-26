from __future__ import annotations

import json
from collections import defaultdict
from statistics import mean
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _make_windows(total: int) -> list[tuple[int, int]]:
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
        begin, _ = windows[-1]
        windows[-1] = (begin, total)
    return windows


def _summary(rows: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for row in rows:
        metrics = row.get("horizons", {}).get(horizon)
        if isinstance(metrics, dict):
            values.append(metrics)
    sample = len(values)
    if not sample:
        return {"sample": 0}
    extra = [_f(v.get("extra_r_after_tp1")) for v in values]
    pullback = [_f(v.get("pullback_from_tp1_r")) for v in values]
    reach2 = sum(1 for v in values if v.get("reached_2r") is True) / sample * 100.0
    reach3 = sum(1 for v in values if v.get("reached_3r") is True) / sample * 100.0
    held = sum(1 for v in values if v.get("held_beyond_entry") is True) / sample * 100.0
    utility = mean(extra) - 0.60 * mean(pullback)
    return {
        "sample": sample,
        "avg_extra_r": round(mean(extra), 4),
        "avg_pullback_r": round(mean(pullback), 4),
        "reached_2r_pct": round(reach2, 2),
        "reached_3r_pct": round(reach3, 2),
        "held_beyond_entry_pct": round(held, 2),
        "shadow_utility_r": round(utility, 4),
    }


def _verdict(train: dict[str, Any], test: dict[str, Any]) -> str:
    if int(train.get("sample") or 0) < 30 or int(test.get("sample") or 0) < 10:
        return "INSUFFICIENT_SAMPLE"
    train_good = _f(train.get("shadow_utility_r")) > 0 and _f(train.get("reached_2r_pct")) >= 50
    test_good = _f(test.get("shadow_utility_r")) > 0 and _f(test.get("reached_2r_pct")) >= 50
    test_bad = _f(test.get("shadow_utility_r")) <= 0 or _f(test.get("avg_pullback_r")) >= 1.35
    if train_good and test_good:
        return "HELD"
    if train_good and test_bad:
        return "FAILED"
    return "MIXED"


def _cohort(row: dict[str, Any]) -> str:
    meta = row.get("metadata") or {}
    track = "FAST_TRACK" if meta.get("fast_track") is True else "NORMAL"
    burst = "BURST" if meta.get("burst_detected") is True else "NO_BURST"
    locks = str(meta.get("lock_count") or "N/D")
    return f"{track}|{burst}|LOCK_{locks}"


async def build_runner_walk_forward(db: AsyncSession, limit: int = 1800) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT observed_at, metadata
            FROM verdict_memory
            WHERE outcome = 'TP1_FIRST'
              AND metadata ? 'tp1_continuation'
            ORDER BY observed_at ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows: list[dict[str, Any]] = []
    for source in result.mappings().all():
        meta = _json_obj(source.get("metadata"))
        continuation = dict(meta.get("tp1_continuation") or {})
        rows.append({
            "observed_at": source.get("observed_at"),
            "metadata": meta,
            "horizons": dict(continuation.get("horizons") or {}),
        })

    total = len(rows)
    windows = _make_windows(total)
    if not windows:
        return {
            "mode": "SHADOW_ONLY",
            "version": "runner_walk_forward_v1",
            "status": "LEARNING",
            "total_sample": total,
            "minimum_required": 120,
            "window_count": 0,
            "horizons": [],
            "cohorts": [],
            "management_activation_allowed": False,
            "rule": "Runner walk-forward starts at 120 TP1-first cases with continuation data.",
        }

    horizons = ("30", "60", "120", "240")
    horizon_reports: list[dict[str, Any]] = []
    for horizon in horizons:
        reports = []
        for idx, (start, end) in enumerate(windows, start=1):
            train = _summary(rows[:start], horizon)
            test = _summary(rows[start:end], horizon)
            reports.append({"window": idx, "train": train, "test": test, "verdict": _verdict(train, test)})
        eligible = [r for r in reports if r["verdict"] != "INSUFFICIENT_SAMPLE"]
        held = sum(1 for r in eligible if r["verdict"] == "HELD")
        failed = sum(1 for r in eligible if r["verdict"] == "FAILED")
        horizon_reports.append({
            "horizon_minutes": int(horizon),
            "eligible_windows": len(eligible),
            "held_windows": held,
            "failed_windows": failed,
            "repeated_hold": len(eligible) >= 3 and held >= 2 and held / len(eligible) >= 2 / 3,
            "repeated_failure": len(eligible) >= 3 and failed >= 2 and failed / len(eligible) >= 2 / 3,
            "windows": reports,
        })

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_cohort(row)].append(row)
    cohort_reports: list[dict[str, Any]] = []
    for name, group in groups.items():
        if len(group) < 120:
            continue
        group_windows = _make_windows(len(group))
        for horizon in horizons:
            reports = []
            for idx, (start, end) in enumerate(group_windows, start=1):
                train = _summary(group[:start], horizon)
                test = _summary(group[start:end], horizon)
                reports.append({"window": idx, "train": train, "test": test, "verdict": _verdict(train, test)})
            eligible = [r for r in reports if r["verdict"] != "INSUFFICIENT_SAMPLE"]
            held = sum(1 for r in eligible if r["verdict"] == "HELD")
            failed = sum(1 for r in eligible if r["verdict"] == "FAILED")
            cohort_reports.append({
                "cohort": name,
                "horizon_minutes": int(horizon),
                "sample": len(group),
                "eligible_windows": len(eligible),
                "held_windows": held,
                "failed_windows": failed,
                "repeated_hold": len(eligible) >= 3 and held >= 2 and held / len(eligible) >= 2 / 3,
                "repeated_failure": len(eligible) >= 3 and failed >= 2 and failed / len(eligible) >= 2 / 3,
                "windows": reports,
            })

    return {
        "mode": "SHADOW_ONLY",
        "version": "runner_walk_forward_v1",
        "status": "ROLLING_VALIDATION",
        "total_sample": total,
        "window_policy": "4_expanding_train_non_overlapping_forward_tests",
        "window_count": len(windows),
        "horizons": horizon_reports,
        "cohorts": cohort_reports[:80],
        "persistent_holds": [r for r in cohort_reports if r.get("repeated_hold")][:12],
        "persistent_failures": [r for r in cohort_reports if r.get("repeated_failure")][:12],
        "management_activation_allowed": False,
        "can_move_stop": False,
        "can_change_exit": False,
        "rule": "Runner remains shadow-only. Repeated hold requires at least 3 eligible forward windows and success in at least two-thirds of them.",
        "utility_note": "shadow_utility_r = average extra R after TP1 minus 0.60 times average pullback R; it is a research utility, not realized expectancy.",
        "probability_note": "Forward historical validation is evidence, not a probability guarantee for the next trade.",
    }
