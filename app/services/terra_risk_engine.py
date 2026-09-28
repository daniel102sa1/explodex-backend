from __future__ import annotations

"""
Terra Adaptive Risk Engine

Paper-trading only. Calculates exposure decisions from evidence quality,
volatility and stop distance. It does not predict outcomes.
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
    """Return adaptive paper risk parameters.

    The objective is not to maximize leverage. It increases exposure only when
    several independent pieces of evidence agree.
    """

    evidence = (
        confidence_score * 0.30
        + liquidity_score * 0.15
        + timeframe_alignment * 0.20
        + pattern_score * 0.15
        + btc_context_score * 0.10
        + volatility_score * 0.10
    )

    evidence = clamp(evidence, 0, 100)

    if evidence < 55:
        tier = "LOW"
        leverage = 1.0
        capital = 5.0
    elif evidence < 75:
        tier = "MEDIUM"
        leverage = 2.0
        capital = 10.0
    elif evidence < 90:
        tier = "HIGH"
        leverage = 5.0
        capital = 20.0
    else:
        tier = "EXTREME"
        leverage = 8.0
        capital = 30.0

    # Wider stops automatically reduce size.
    if stop_distance_pct > 3:
        capital *= 0.5
        leverage = min(leverage, 3.0)

    if stop_distance_pct > 6:
        capital = min(capital, 5.0)
        leverage = 1.0

    return {
        "tier": tier,
        "evidence_score": round(evidence, 2),
        "leverage": leverage,
        "capital_allocation_pct": round(capital, 2),\n        "risk_pct": risk_pct,
        "reason": [
            "Leverage depends on evidence alignment, not a single indicator",
            "Stop distance reduces position size when risk increases",
            "Designed for PAPER simulation before any real capital"
        ],
    }
