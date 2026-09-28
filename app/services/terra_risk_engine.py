from __future__ import annotations

"""Terra Adaptive Risk Engine.

PAPER-only sizing guard. Exposure grows only when multiple independent sources
align. Technical invalidation stays structural; size and leverage adapt around
that stop instead of moving it arbitrarily.
"""

from typing import Any


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def evaluate_risk(
    *,
    confidence_score: float,
    stop_distance_pct: float,
    volatility_score: float,
    liquidity_score: float,
    timeframe_alignment: float,
    pattern_score: float,
    btc_context_score: float,
) -> dict[str, Any]:
    evidence = (
        confidence_score * 0.30
        + liquidity_score * 0.15
        + timeframe_alignment * 0.20
        + pattern_score * 0.15
        + btc_context_score * 0.10
        + volatility_score * 0.10
    )
    evidence = clamp(evidence, 0.0, 100.0)

    if evidence < 55:
        tier, leverage, capital, risk_pct = "LOW", 1.0, 3.0, 0.20
    elif evidence < 75:
        tier, leverage, capital, risk_pct = "MEDIUM", 2.0, 8.0, 0.35
    elif evidence < 90:
        tier, leverage, capital, risk_pct = "HIGH", 5.0, 15.0, 0.60
    else:
        tier, leverage, capital, risk_pct = "EXTREME", 12.0, 30.0, 1.00

    if stop_distance_pct > 3:
        capital *= 0.5
        leverage = min(leverage, 3.0)
        risk_pct = min(risk_pct, 0.60)

    if stop_distance_pct > 6:
        capital = min(capital, 5.0)
        leverage = 1.0
        risk_pct = min(risk_pct, 0.25)

    return {
        "tier": tier,
        "evidence_score": round(evidence, 2),
        "leverage": leverage,
        "capital_allocation_pct": round(capital, 2),
        "risk_pct": risk_pct,
        "reason": [
            "leverage_requires_multi_source_alignment",
            "wide_technical_stops_reduce_size_and_leverage",
            "paper_only_risk_guard",
        ],
    }
