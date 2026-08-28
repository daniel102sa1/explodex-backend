from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.selective_precision_lab import chronological_split, wilson_interval


TRAIN_FRACTION = 0.70
MIN_TRAIN_DECIDED = 20
MIN_HOLDOUT_DECIDED = 10
MAX_PRECISION_DROP_PCT = 20.0


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm(value: Any, fallback: str = "N/D") -> str:
    text_value = str(value or "").strip().upper()
    return text_value or fallback


def _decided(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if _norm(r.get("barrier_hit")) in {"TP1", "STOP"}]


def cohort_keys(row: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    symbol = _norm(row.get("symbol"))
    direction = _norm(row.get("direction"))
    trade_class = _norm(row.get("trade_class"))
    catalyst = _norm(row.get("catalyst_state"))
    path_bias = _norm(row.get("path_bias"))

    return [
        ("DIRECTION", (direction,)),
        ("SYMBOL_DIRECTION", (symbol, direction)),
        ("SYMBOL_DIRECTION_CLASS", (symbol, direction, trade_class)),
        ("SYMBOL_DIRECTION_CATALYST", (symbol, direction, catalyst)),
        ("SYMBOL_DIRECTION_PATH", (symbol, direction, path_bias)),
        ("FULL_CONTEXT", (symbol, direction, trade_class, catalyst, path_bias)),
    ]


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided = _decided(rows)
    wins = sum(1 for r in decided if _norm(r.get("barrier_hit")) == "TP1")
    losses = len(decided) - wins
    precision = wins / len(decided) * 100.0 if decided else None
    low, high = wilson_interval(wins, len(decided))
    avg_return = sum(_f(r.get("directional_return_pct")) for r in decided) / len(decided) if decided else None
    avg_mfe = sum(_f(r.get("mfe_pct")) for r in decided) / len(decided) if decided else None
    avg_mae_abs = sum(abs(_f(r.get("mae_pct"))) for r in decided) / len(decided) if decided else None
    return {
        "observations": len(rows),
        "decided": len(decided),
        "wins": wins,
        "losses": losses,
        "observed_tp1_first_pct": round(precision, 2) if precision is not None else None,
        "wilson_low_pct": round(low * 100.0, 2) if low is not None else None,
        "wilson_high_pct": round(high * 100.0, 2) if high is not None else None,
        "avg_directional_return_pct": round(avg_return, 6) if avg_return is not None else None,
        "avg_mfe_pct": round(avg_mfe, 6) if avg_mfe is not None else None,
        "avg_mae_abs_pct": round(avg_mae_abs, 6) if avg_mae_abs is not None else None,
    }


def build_contextual_precision_map(rows: list[dict[str, Any]]) -> dict[str, Any]:
    train_rows, holdout_rows = chronological_split(rows, TRAIN_FRACTION)

    train_groups: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    holdout_groups: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)

    for row in train_rows:
        for key in cohort_keys(row):
            train_groups[key].append(row)
    for row in holdout_rows:
        for key in cohort_keys(row):
            holdout_groups[key].append(row)

    all_keys = set(train_groups) | set(holdout_groups)
    cohorts: list[dict[str, Any]] = []
    for level, values in all_keys:
        train = _stats(train_groups.get((level, values), []))
        holdout = _stats(holdout_groups.get((level, values), []))
        train_p = train.get("observed_tp1_first_pct")
        holdout_p = holdout.get("observed_tp1_first_pct")
        precision_drop = None
        if train_p is not None and holdout_p is not None:
            precision_drop = _f(train_p) - _f(holdout_p)

        enough_sample = train["decided"] >= MIN_TRAIN_DECIDED and holdout["decided"] >= MIN_HOLDOUT_DECIDED
        stable = bool(
            enough_sample
            and precision_drop is not None
            and precision_drop <= MAX_PRECISION_DROP_PCT
            and _f(holdout.get("wilson_low_pct")) >= 40.0
            and _f(holdout.get("avg_directional_return_pct")) > 0.0
        )

        dimension_names = {
            "DIRECTION": ("direction",),
            "SYMBOL_DIRECTION": ("symbol", "direction"),
            "SYMBOL_DIRECTION_CLASS": ("symbol", "direction", "trade_class"),
            "SYMBOL_DIRECTION_CATALYST": ("symbol", "direction", "catalyst_state"),
            "SYMBOL_DIRECTION_PATH": ("symbol", "direction", "path_bias"),
            "FULL_CONTEXT": ("symbol", "direction", "trade_class", "catalyst_state", "path_bias"),
        }[level]
        dimensions = dict(zip(dimension_names, values))

        cohorts.append({
            "level": level,
            "dimensions": dimensions,
            "train": train,
            "holdout": holdout,
            "precision_drop_pct": round(precision_drop, 2) if precision_drop is not None else None,
            "enough_sample": enough_sample,
            "stable_out_of_sample": stable,
            "observed_frequency_not_probability": True,
        })

    cohorts.sort(
        key=lambda c: (
            c["stable_out_of_sample"],
            c["enough_sample"],
            _f(c["holdout"].get("wilson_low_pct"), -1.0),
            _f(c["holdout"].get("observed_tp1_first_pct"), -1.0),
            int(c["holdout"].get("decided") or 0),
        ),
        reverse=True,
    )

    stable = [c for c in cohorts if c["stable_out_of_sample"]]
    by_level: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cohort in cohorts:
        by_level[cohort["level"]].append(cohort)

    return {
        "version": "contextual_precision_map_v1",
        "paper_research_only": True,
        "observations": len(rows),
        "train_observations": len(train_rows),
        "holdout_observations": len(holdout_rows),
        "train_fraction": TRAIN_FRACTION,
        "minimum_train_decided": MIN_TRAIN_DECIDED,
        "minimum_holdout_decided": MIN_HOLDOUT_DECIDED,
        "stable_cohorts": len(stable),
        "best_stable_cohorts": stable[:20],
        "levels": {level: items[:25] for level, items in by_level.items()},
        "method": {
            "success_definition": "TP1 before STOP among decided PAPER outcomes",
            "chronological_holdout": True,
            "dimensions_are_persisted_not_invented": True,
            "dimensions": ["symbol", "direction", "trade_class", "catalyst_state", "path_bias"],
            "regime_dimension": "N/D until a stable regime field is persisted",
            "observed_frequency_is_probability": False,
            "live_rules_changed": False,
        },
        "warning": (
            "These are observed PAPER frequencies on a chronological holdout. They do not guarantee future results, "
            "and sparse cohorts must not influence live entry rules."
        ),
    }


async def contextual_precision_map_report(
    db: AsyncSession,
    horizon_minutes: int = 60,
    limit: int = 20000,
) -> dict[str, Any]:
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT vo.signal_id::text, vo.symbol, vo.observed_at, vo.direction, vo.trade_class,
               vo.catalyst_state, vo.path_bias, vo.fingerprint_score, vo.locks_passed,
               vr.horizon_minutes, vr.barrier_hit, vr.mfe_pct, vr.mae_pct, vr.directional_return_pct
        FROM validation_observations vo
        JOIN validation_horizon_results vr ON vr.signal_id=vo.signal_id
        WHERE vr.horizon_minutes=:horizon
          AND vo.observed_at >= NOW() - INTERVAL '30 days'
        ORDER BY vo.observed_at ASC
        LIMIT :limit
    """), {"horizon": horizon_minutes, "limit": limit})).mappings().all()]

    report = build_contextual_precision_map(rows)
    report["horizon_minutes"] = horizon_minutes
    return report
