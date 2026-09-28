from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def build_learning_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize closed Terra PAPER outcomes by risk tier.

    Learning is intentionally conservative: small samples may only reduce
    exposure, never increase it. Positive sizing adjustments require a much
    larger sample so short lucky streaks cannot teach Terra to over-leverage.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        tier = str(metadata.get("terra_risk_tier") or "UNKNOWN").upper()
        buckets.setdefault(tier, []).append(row)

    out: dict[str, Any] = {"tiers": {}, "sample": len(rows), "paper_only": True}
    for tier, items in buckets.items():
        pnls = [_f(x.get("net_pnl")) for x in items]
        risks = [_f(x.get("risk_usdt")) for x in items]
        gross_profit = sum(x for x in pnls if x > 0)
        gross_loss = abs(sum(x for x in pnls if x < 0))
        r_values = [p / r for p, r in zip(pnls, risks) if r > 0]
        sample = len(items)
        wins = sum(1 for p in pnls if p > 0)
        pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else None)
        avg_r = sum(r_values) / len(r_values) if r_values else None
        out["tiers"][tier] = {
            "sample": sample,
            "wins": wins,
            "losses": sample - wins,
            "win_rate_pct": round(wins / sample * 100.0, 2) if sample else None,
            "profit_factor": round(pf, 4) if pf is not None else None,
            "average_r": round(avg_r, 4) if avg_r is not None else None,
            "net_pnl": round(sum(pnls), 6),
        }
    return out


def adjustment_for_tier(snapshot: dict[str, Any], tier: str) -> dict[str, Any]:
    stats = dict((snapshot.get("tiers") or {}).get(str(tier or "").upper()) or {})
    sample = int(_f(stats.get("sample")))
    pf = stats.get("profit_factor")
    avg_r = stats.get("average_r")
    pfv = _f(pf, 1.0)
    arv = _f(avg_r, 0.0)

    size_multiplier = 1.0
    leverage_multiplier = 1.0
    reason = "insufficient_sample_no_boost"

    # Early evidence can only de-risk.
    if sample >= 10 and (pfv < 0.75 or arv < -0.20):
        size_multiplier, leverage_multiplier, reason = 0.50, 0.50, "tier_materially_unprofitable"
    elif sample >= 20 and (pfv < 0.95 or arv < -0.05):
        size_multiplier, leverage_multiplier, reason = 0.75, 0.75, "tier_below_expectation"
    # Positive adaptation is deliberately slow.
    elif sample >= 150 and pfv >= 1.40 and arv >= 0.10:
        size_multiplier, leverage_multiplier, reason = 1.10, 1.10, "large_sample_positive_edge"
    elif sample >= 80 and pfv >= 1.20 and arv >= 0.05:
        size_multiplier, leverage_multiplier, reason = 1.05, 1.05, "mature_sample_positive_edge"

    return {
        "tier": str(tier or "").upper(),
        "sample": sample,
        "size_multiplier": size_multiplier,
        "leverage_multiplier": leverage_multiplier,
        "reason": reason,
        "stats": stats,
        "paper_only": True,
    }
