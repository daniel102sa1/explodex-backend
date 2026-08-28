from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


MIN_SAMPLE = 40
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


def evaluate_filter_grid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Search conservative high-precision cohorts; research only, never a live entry rule."""
    specs: list[dict[str, Any]] = []
    for fp in (60, 65, 70, 75, 80):
        for locks in (4, 5, 6):
            for master_yes in (False, True):
                specs.append({"min_fingerprint": fp, "min_locks": locks, "master_yes": master_yes})

    out: list[dict[str, Any]] = []
    total = len(rows)
    for spec in specs:
        selected = [
            r for r in rows
            if _f(r.get("fingerprint_score")) >= spec["min_fingerprint"]
            and int(_f(r.get("locks_passed"))) >= spec["min_locks"]
            and (not spec["master_yes"] or str(r.get("master_state") or "").upper() == "YES")
            and str(r.get("trade_class") or "") in {"TRADE_NOW", "TRADE_SOON", "WATCHLIST"}
        ]
        if not selected:
            continue
        decided = [r for r in selected if str(r.get("barrier_hit") or "").upper() in {"TP1", "STOP"}]
        wins = sum(1 for r in decided if _barrier_success(r.get("barrier_hit")))
        precision = (wins / len(decided) * 100.0) if decided else None
        coverage = (len(selected) / total * 100.0) if total else 0.0
        out.append({
            **spec,
            "selected": len(selected),
            "decided": len(decided),
            "wins": wins,
            "precision_pct": round(precision, 2) if precision is not None else None,
            "coverage_pct": round(coverage, 2),
            "enough_sample": len(decided) >= MIN_SAMPLE,
        })
    out.sort(key=lambda r: (
        bool(r["enough_sample"]),
        _f(r.get("precision_pct"), -1.0),
        int(r.get("decided") or 0),
    ), reverse=True)
    return out


def summarize_targets(grid: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for target in TARGETS:
        candidates = [
            r for r in grid
            if r.get("enough_sample") and r.get("precision_pct") is not None and _f(r["precision_pct"]) >= target
        ]
        best = max(candidates, key=lambda r: (_f(r.get("coverage_pct")), int(r.get("decided") or 0)), default=None)
        summary.append({
            "target_precision_pct": target,
            "achieved_out_of_sample_proxy": best is not None,
            "best": best,
        })
    return summary


async def selective_precision_report(db: AsyncSession, horizon_minutes: int = 60, limit: int = 10000) -> dict[str, Any]:
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT vo.signal_id::text, vo.symbol, vo.observed_at, vo.trade_class, vo.master_state,
               vo.fingerprint_score, vo.locks_passed, vo.catalyst_state, vo.path_bias,
               vr.horizon_minutes, vr.barrier_hit, vr.mfe_pct, vr.mae_pct, vr.directional_return_pct
        FROM validation_observations vo
        JOIN validation_horizon_results vr ON vr.signal_id=vo.signal_id
        WHERE vr.horizon_minutes=:horizon
          AND vo.observed_at >= NOW() - INTERVAL '30 days'
        ORDER BY vo.observed_at ASC
        LIMIT :limit
    """), {"horizon": horizon_minutes, "limit": limit})).mappings().all()]

    grid = evaluate_filter_grid(rows)
    enough = [r for r in grid if r.get("enough_sample") and r.get("precision_pct") is not None]
    best = enough[0] if enough else None
    status = "INSUFFICIENT_SAMPLE"
    if best:
        p = _f(best.get("precision_pct"))
        status = "PROMISING" if p >= 70 else "WEAK_EDGE"
        if p >= 85:
            status = "HIGH_SELECTIVE_PRECISION"
        if p >= 90:
            status = "NINETY_RESEARCH_THRESHOLD_REACHED"

    return {
        "version": "selective_precision_lab_v1",
        "paper_research_only": True,
        "horizon_minutes": horizon_minutes,
        "observations": len(rows),
        "minimum_decided_sample": MIN_SAMPLE,
        "status": status,
        "best_valid_cohort": best,
        "precision_targets": summarize_targets(grid),
        "top_cohorts": grid[:20],
        "method": {
            "success_definition": "TP1 before STOP among decided outcomes",
            "coverage_tradeoff": True,
            "abstention_principle": "higher selectivity may increase precision while reducing frequency",
            "live_rules_changed": False,
            "probability_claim": False,
        },
        "warning": (
            "This is a retrospective research grid on PAPER observations, not proof of future 90% accuracy. "
            "Any promising cohort must later be tested chronologically on unseen data before changing live rules."
        ),
    }
