from __future__ import annotations

import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


MIN_TRAIN_DECIDED = 40
MIN_HOLDOUT_DECIDED = 20
TRAIN_FRACTION = 0.70
MAX_ALLOWED_PRECISION_DROP_PCT = 15.0
TARGETS = (70.0, 80.0, 85.0, 90.0)


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _barrier_success(value: Any) -> bool:
    return str(value or "").upper() == "TP1"


def wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    z2 = z * z
    denom = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    low = max(0.0, (centre - margin) / denom)
    high = min(1.0, (centre + margin) / denom)
    return low, high


def chronological_split(rows: list[dict[str, Any]], train_fraction: float = TRAIN_FRACTION) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda r: r.get("observed_at"))
    if len(ordered) < 2:
        return ordered, []
    split_index = max(1, min(len(ordered) - 1, int(len(ordered) * train_fraction)))
    return ordered[:split_index], ordered[split_index:]


def filter_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for fp in (60, 65, 70, 75, 80, 85):
        for locks in (4, 5, 6):
            for master_yes in (False, True):
                for catalyst_guard in (False, True):
                    specs.append({
                        "min_fingerprint": fp,
                        "min_locks": locks,
                        "master_yes": master_yes,
                        "catalyst_guard": catalyst_guard,
                    })
    return specs


def _select(rows: list[dict[str, Any]], spec: dict[str, Any]) -> list[dict[str, Any]]:
    blocked_catalysts = {"CONFLICT", "SHOCK_RISK"}
    return [
        r for r in rows
        if _f(r.get("fingerprint_score")) >= spec["min_fingerprint"]
        and int(_f(r.get("locks_passed"))) >= spec["min_locks"]
        and (not spec["master_yes"] or str(r.get("master_state") or "").upper() == "YES")
        and (
            not spec["catalyst_guard"]
            or str(r.get("catalyst_state") or "").upper() not in blocked_catalysts
        )
        and str(r.get("trade_class") or "") in {"TRADE_NOW", "TRADE_SOON", "WATCHLIST"}
    ]


def evaluate_spec(rows: list[dict[str, Any]], spec: dict[str, Any]) -> dict[str, Any]:
    selected = _select(rows, spec)
    decided = [r for r in selected if str(r.get("barrier_hit") or "").upper() in {"TP1", "STOP"}]
    wins = sum(1 for r in decided if _barrier_success(r.get("barrier_hit")))
    precision = wins / len(decided) * 100.0 if decided else None
    low, high = wilson_interval(wins, len(decided))
    coverage = len(selected) / len(rows) * 100.0 if rows else 0.0
    avg_return = sum(_f(r.get("directional_return_pct")) for r in decided) / len(decided) if decided else None
    avg_mfe = sum(_f(r.get("mfe_pct")) for r in decided) / len(decided) if decided else None
    avg_mae_abs = sum(abs(_f(r.get("mae_pct"))) for r in decided) / len(decided) if decided else None
    return {
        **spec,
        "selected": len(selected),
        "decided": len(decided),
        "wins": wins,
        "losses": len(decided) - wins,
        "precision_pct": round(precision, 2) if precision is not None else None,
        "wilson_low_pct": round(low * 100.0, 2) if low is not None else None,
        "wilson_high_pct": round(high * 100.0, 2) if high is not None else None,
        "coverage_pct": round(coverage, 2),
        "avg_directional_return_pct": round(avg_return, 6) if avg_return is not None else None,
        "avg_mfe_pct": round(avg_mfe, 6) if avg_mfe is not None else None,
        "avg_mae_abs_pct": round(avg_mae_abs, 6) if avg_mae_abs is not None else None,
        "enough_sample": len(decided) >= MIN_TRAIN_DECIDED,
    }


def evaluate_filter_grid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [evaluate_spec(rows, spec) for spec in filter_specs()]
    out = [row for row in out if row["selected"] > 0]
    out.sort(
        key=lambda r: (
            r["enough_sample"],
            _f(r.get("wilson_low_pct"), -1.0),
            _f(r.get("precision_pct"), -1.0),
            _f(r.get("coverage_pct")),
        ),
        reverse=True,
    )
    return out


def validate_train_candidate(candidate: dict[str, Any], holdout_rows: list[dict[str, Any]]) -> dict[str, Any]:
    spec = {
        "min_fingerprint": candidate["min_fingerprint"],
        "min_locks": candidate["min_locks"],
        "master_yes": candidate["master_yes"],
        "catalyst_guard": candidate["catalyst_guard"],
    }
    test = evaluate_spec(holdout_rows, spec)
    train_precision = _f(candidate.get("precision_pct"), -1.0)
    holdout_precision = _f(test.get("precision_pct"), -1.0)
    drop = train_precision - holdout_precision if train_precision >= 0 and holdout_precision >= 0 else None
    stable = bool(
        candidate.get("decided", 0) >= MIN_TRAIN_DECIDED
        and test.get("decided", 0) >= MIN_HOLDOUT_DECIDED
        and test.get("precision_pct") is not None
        and (drop is not None and drop <= MAX_ALLOWED_PRECISION_DROP_PCT)
        and _f(test.get("wilson_low_pct")) >= 45.0
        and _f(test.get("avg_directional_return_pct")) > 0.0
    )
    return {
        "spec": spec,
        "train": candidate,
        "holdout": test,
        "precision_drop_pct": round(drop, 2) if drop is not None else None,
        "stable_out_of_sample": stable,
    }


def reliability_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bins = ((0, 59), (60, 64), (65, 69), (70, 74), (75, 79), (80, 84), (85, 89), (90, 100))
    output: list[dict[str, Any]] = []
    for low_score, high_score in bins:
        bucket = [
            r for r in rows
            if low_score <= _f(r.get("fingerprint_score")) <= high_score
            and str(r.get("barrier_hit") or "").upper() in {"TP1", "STOP"}
        ]
        wins = sum(1 for r in bucket if _barrier_success(r.get("barrier_hit")))
        low, high = wilson_interval(wins, len(bucket))
        output.append({
            "fingerprint_bin": f"{low_score}-{high_score}",
            "decided": len(bucket),
            "wins": wins,
            "observed_tp1_first_pct": round(wins / len(bucket) * 100.0, 2) if bucket else None,
            "wilson_low_pct": round(low * 100.0, 2) if low is not None else None,
            "wilson_high_pct": round(high * 100.0, 2) if high is not None else None,
            "enough_sample": len(bucket) >= MIN_HOLDOUT_DECIDED,
        })
    return output


def summarize_targets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize precision targets for v2 holdout results and legacy retrospective grids.

    The compatibility alias `achieved_out_of_sample_proxy` is retained because older
    callers/tests expect it. In legacy-grid mode it remains explicitly labeled as a
    retrospective proxy and must not be interpreted as true out-of-sample validation.
    """
    validated_mode = any(isinstance(row.get("holdout"), dict) for row in items)
    summary: list[dict[str, Any]] = []

    for target in TARGETS:
        if validated_mode:
            candidates = [
                row for row in items
                if row.get("stable_out_of_sample")
                and isinstance(row.get("holdout"), dict)
                and row["holdout"].get("precision_pct") is not None
                and _f(row["holdout"]["precision_pct"]) >= target
            ]
            best = max(
                candidates,
                key=lambda row: (
                    _f(row["holdout"].get("coverage_pct")),
                    int(row["holdout"].get("decided") or 0),
                    _f(row["holdout"].get("wilson_low_pct")),
                ),
                default=None,
            )
            achieved = best is not None
            summary.append({
                "target_precision_pct": target,
                "achieved_on_chronological_holdout": achieved,
                "achieved_out_of_sample_proxy": achieved,
                "legacy_retrospective_proxy": False,
                "best": best,
            })
            continue

        candidates = [
            row for row in items
            if row.get("enough_sample")
            and row.get("precision_pct") is not None
            and _f(row.get("precision_pct")) >= target
        ]
        best = max(
            candidates,
            key=lambda row: (
                _f(row.get("coverage_pct")),
                int(row.get("decided") or 0),
            ),
            default=None,
        )
        summary.append({
            "target_precision_pct": target,
            "achieved_out_of_sample_proxy": best is not None,
            "achieved_on_chronological_holdout": False,
            "legacy_retrospective_proxy": True,
            "best": best,
        })
    return summary


async def selective_precision_report(db: AsyncSession, horizon_minutes: int = 60, limit: int = 10000) -> dict[str, Any]:
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT vo.signal_id::text, vo.symbol, vo.observed_at, vo.direction, vo.trade_class, vo.master_state,
               vo.fingerprint_score, vo.locks_passed, vo.catalyst_state, vo.path_bias,
               vr.horizon_minutes, vr.barrier_hit, vr.mfe_pct, vr.mae_pct, vr.directional_return_pct
        FROM validation_observations vo
        JOIN validation_horizon_results vr ON vr.signal_id=vo.signal_id
        WHERE vr.horizon_minutes=:horizon
          AND vo.observed_at >= NOW() - INTERVAL '30 days'
        ORDER BY vo.observed_at ASC
        LIMIT :limit
    """), {"horizon": horizon_minutes, "limit": limit})).mappings().all()]

    train_rows, holdout_rows = chronological_split(rows)
    train_grid = evaluate_filter_grid(train_rows)
    train_candidates = [row for row in train_grid if row.get("decided", 0) >= MIN_TRAIN_DECIDED][:25]
    validated = [validate_train_candidate(candidate, holdout_rows) for candidate in train_candidates]
    validated.sort(
        key=lambda row: (
            row["stable_out_of_sample"],
            _f(row["holdout"].get("wilson_low_pct"), -1.0),
            _f(row["holdout"].get("precision_pct"), -1.0),
            int(row["holdout"].get("decided") or 0),
        ),
        reverse=True,
    )
    stable = [row for row in validated if row.get("stable_out_of_sample")]
    best = stable[0] if stable else None

    status = "INSUFFICIENT_HOLDOUT_SAMPLE"
    if best:
        p = _f(best["holdout"].get("precision_pct"))
        status = "HOLDOUT_EDGE_FOUND"
        if p >= 80:
            status = "HIGH_SELECTIVE_PRECISION_OOS"
        if p >= 90:
            status = "NINETY_RESEARCH_THRESHOLD_OOS"
    elif len(holdout_rows) >= MIN_HOLDOUT_DECIDED:
        status = "NO_STABLE_COHORT_YET"

    return {
        "version": "selective_precision_lab_v2_chronological_holdout",
        "paper_research_only": True,
        "horizon_minutes": horizon_minutes,
        "observations": len(rows),
        "train_observations": len(train_rows),
        "holdout_observations": len(holdout_rows),
        "train_fraction": TRAIN_FRACTION,
        "minimum_train_decided": MIN_TRAIN_DECIDED,
        "minimum_holdout_decided": MIN_HOLDOUT_DECIDED,
        "status": status,
        "best_holdout_validated_cohort": best,
        "precision_targets": summarize_targets(validated),
        "top_holdout_validated_cohorts": validated[:15],
        "holdout_reliability_by_fingerprint": reliability_table(holdout_rows),
        "method": {
            "success_definition": "TP1 before STOP among decided outcomes",
            "discovery_period": "oldest 70% of chronologically ordered observations",
            "validation_period": "newest 30% held out from rule discovery",
            "uncertainty_interval": "95% Wilson interval",
            "max_allowed_train_to_holdout_precision_drop_pct": MAX_ALLOWED_PRECISION_DROP_PCT,
            "coverage_tradeoff": True,
            "live_rules_changed": False,
            "probability_claim": False,
        },
        "warning": (
            "A high holdout percentage is still not a guarantee. Repeated future walk-forward windows are required "
            "before any calibration is allowed to influence live entry rules."
        ),
    }
