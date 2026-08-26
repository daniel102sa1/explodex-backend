from __future__ import annotations

from math import sqrt
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
    denom = 1 + z2 / total
    low = max(0.0, (center - margin) / denom)
    high = min(1.0, (center + margin) / denom)
    return low, high


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [r for r in rows if r.get("outcome") in {"TP1_FIRST", "STOP_FIRST"}]
    wins = sum(1 for r in decided if r.get("outcome") == "TP1_FIRST")
    losses = len(decided) - wins
    low, high = _wilson(wins, len(decided))

    rr_values: list[float] = []
    observed_r: list[float] = []
    for row in decided:
        entry = _f(row.get("entry_price"))
        stop = _f(row.get("stop_loss"))
        tp1 = _f(row.get("tp1"))
        risk = abs(entry - stop)
        rr = abs(tp1 - entry) / risk if risk > 0 else 0.0
        if rr > 0:
            rr_values.append(rr)
            observed_r.append(rr if row.get("outcome") == "TP1_FIRST" else -1.0)

    avg_rr = sum(rr_values) / len(rr_values) if rr_values else None
    observed_ev_r = sum(observed_r) / len(observed_r) if observed_r else None
    conservative_ev_r = None
    if low is not None and avg_rr is not None:
        conservative_ev_r = low * avg_rr - (1.0 - low)

    mfe = [_f(r.get("mfe_pct")) for r in decided if r.get("mfe_pct") is not None]
    mae = [_f(r.get("mae_pct")) for r in decided if r.get("mae_pct") is not None]
    minutes = [_f(r.get("minutes_to_outcome")) for r in decided if r.get("minutes_to_outcome") is not None]

    return {
        "sample": len(decided),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / len(decided) * 100.0 if decided else None,
        "wilson_low_pct": low * 100.0 if low is not None else None,
        "wilson_high_pct": high * 100.0 if high is not None else None,
        "avg_rr1": avg_rr,
        "observed_ev_r": observed_ev_r,
        "conservative_ev_r": conservative_ev_r,
        "avg_mfe_pct": sum(mfe) / len(mfe) if mfe else None,
        "avg_mae_pct": sum(mae) / len(mae) if mae else None,
        "avg_minutes_to_outcome": sum(minutes) / len(minutes) if minutes else None,
        "sample_status": "USABLE" if len(decided) >= 30 else "CALIBRATING",
    }


def _group(rows: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(key_fn(row))
        buckets.setdefault(key, []).append(row)
    output = []
    for key, items in buckets.items():
        output.append({"cohort": key, **_summary(items)})
    output.sort(key=lambda item: int(item.get("sample") or 0), reverse=True)
    return output


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


async def build_fusion_edge_research(db: AsyncSession, limit: int = 1800) -> dict[str, Any]:
    """Measure whether server Verdict Fusion states are actually associated with edge.

    This report is research-only. It does not create entries, change leverage, or
    imply a probability for the next trade. It compares realized PAPER outcomes.
    """
    result = await db.execute(
        text(
            """
            SELECT observed_at, direction, outcome, entry_price, stop_loss, tp1,
                   mfe_pct, mae_pct, minutes_to_outcome, metadata
            FROM verdict_memory
            WHERE metadata->>'server_fusion_version' = 'server_parity_v1'
            ORDER BY observed_at ASC
            LIMIT :limit
            """
        ),
        {"limit": max(100, min(limit, 5000))},
    )
    rows = [dict(row) for row in result.mappings().all()]
    decided = [row for row in rows if row.get("outcome") in {"TP1_FIRST", "STOP_FIRST"}]

    def meta(row: dict[str, Any], key: str, default: Any = None) -> Any:
        metadata = row.get("metadata") or {}
        return metadata.get(key, default) if isinstance(metadata, dict) else default

    by_lock_count = _group(decided, lambda r: meta(r, "lock_count", "N/D"))
    by_burst = _group(decided, lambda r: "BURST" if _boolean(meta(r, "burst_detected")) else "NO_BURST")
    by_fast_track = _group(decided, lambda r: "FAST_TRACK" if _boolean(meta(r, "fast_track")) else "NORMAL")
    by_candidate = _group(decided, lambda r: "CANDIDATE" if _boolean(meta(r, "candidate_enter")) else "NOT_CANDIDATE")
    by_direction = _group(decided, lambda r: r.get("direction") or "N/D")

    strong_profiles = []
    for label, cohorts in (
        ("lock_count", by_lock_count),
        ("burst", by_burst),
        ("track", by_fast_track),
        ("candidate", by_candidate),
    ):
        for cohort in cohorts:
            if int(cohort.get("sample") or 0) < 30:
                continue
            observed_ev = cohort.get("observed_ev_r")
            conservative_ev = cohort.get("conservative_ev_r")
            if observed_ev is not None and conservative_ev is not None and observed_ev > 0 and conservative_ev > 0:
                strong_profiles.append({"family": label, **cohort})

    return {
        "mode": "SHADOW_RESEARCH",
        "sample": len(decided),
        "total_rows": len(rows),
        "by_lock_count": by_lock_count,
        "by_burst": by_burst,
        "by_fast_track": by_fast_track,
        "by_candidate": by_candidate,
        "by_direction": by_direction,
        "strong_profiles": strong_profiles,
        "can_create_entry": False,
        "can_raise_leverage": False,
        "can_activate_veto": False,
        "important": (
            "Win rate and Wilson bands are historical PAPER statistics, not the probability that the next trade wins. "
            "Positive observed/conservative EV R is required before treating a cohort as promising."
        ),
        "continuation_limit": (
            "Current verdict outcomes stop evaluation when TP1 or STOP is first hit. This report measures entry quality, "
            "not how far the move continued after TP1. A separate continuation study is required to optimize capture of large trends."
        ),
    }
