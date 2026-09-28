from __future__ import annotations

"""Terra Adaptive Risk Engine V2.

PAPER-only deterministic guard. Terra chooses the thesis; this module converts
quality, liquidity, volatility, stop distance and timeframe agreement into
bounded exposure. Leverage is a margin tool, never a probability claim.
"""

from typing import Any

VERSION = "terra_adaptive_risk_v2"


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
    requested_leverage: float = 1.0,
    requested_capital_allocation_pct: float = 5.0,
    requested_risk_pct: float = 0.25,
    learning_multiplier: float = 1.0,
) -> dict[str, Any]:
    confidence_score = clamp(confidence_score, 0, 100)
    volatility_score = clamp(volatility_score, 0, 100)
    liquidity_score = clamp(liquidity_score, 0, 100)
    timeframe_alignment = clamp(timeframe_alignment, 0, 100)
    pattern_score = clamp(pattern_score, 0, 100)
    btc_context_score = clamp(btc_context_score, 0, 100)
    learning_multiplier = clamp(learning_multiplier, 0.50, 1.00)

    evidence = (
        confidence_score * 0.30
        + timeframe_alignment * 0.22
        + liquidity_score * 0.16
        + pattern_score * 0.14
        + btc_context_score * 0.10
        + volatility_score * 0.08
    )
    evidence = clamp(evidence, 0, 100)

    allow_entry = evidence >= 55 and 0.05 <= stop_distance_pct <= 8.0 and liquidity_score >= 25
    if evidence < 55:
        tier, max_leverage, max_capital, max_risk = "LOW", 1.0, 0.0, 0.0
    elif evidence < 68:
        tier, max_leverage, max_capital, max_risk = "CAUTIOUS", 2.0, 8.0, 0.25
    elif evidence < 80:
        tier, max_leverage, max_capital, max_risk = "MEDIUM", 4.0, 15.0, 0.50
    elif evidence < 90:
        tier, max_leverage, max_capital, max_risk = "HIGH", 8.0, 25.0, 0.75
    else:
        tier, max_leverage, max_capital, max_risk = "EXTREME", 15.0, 40.0, 1.00

    reasons: list[str] = []
    if stop_distance_pct > 3.0:
        max_capital *= 0.65
        max_leverage = min(max_leverage, 5.0)
        max_risk *= 0.75
        reasons.append("wide_stop_reduces_exposure")
    if stop_distance_pct > 6.0:
        max_capital = min(max_capital, 5.0)
        max_leverage = 1.0
        max_risk = min(max_risk, 0.25)
        reasons.append("very_wide_stop_forces_low_exposure")
    if liquidity_score < 45:
        max_leverage = min(max_leverage, 3.0)
        max_capital *= 0.60
        reasons.append("weak_liquidity_caps_leverage")
    if volatility_score < 35:
        max_leverage = min(max_leverage, 4.0)
        max_capital *= 0.75
        reasons.append("unstable_volatility_caps_exposure")
    if timeframe_alignment < 45:
        max_leverage = min(max_leverage, 2.0)
        max_capital *= 0.50
        max_risk *= 0.60
        reasons.append("timeframe_conflict_reduces_exposure")

    max_leverage *= learning_multiplier
    max_capital *= learning_multiplier
    max_risk *= learning_multiplier

    requested_leverage = max(1.0, requested_leverage)
    requested_capital_allocation_pct = max(0.0, requested_capital_allocation_pct)
    requested_risk_pct = max(0.0, requested_risk_pct)

    leverage = min(requested_leverage, max_leverage) if allow_entry else 1.0
    capital = min(requested_capital_allocation_pct, max_capital) if allow_entry else 0.0
    risk_pct = min(requested_risk_pct, max_risk) if allow_entry else 0.0

    if allow_entry and capital <= 0:
        capital = min(5.0, max_capital)
    if allow_entry and risk_pct <= 0:
        risk_pct = min(0.25, max_risk)

    reasons.insert(0, f"evidence_tier_{tier.lower()}")
    reasons.append("leverage_requires_multi_source_alignment")
    reasons.append("position_size_is_bounded_by_structural_stop_risk")
    reasons.append("paper_only")

    return {
        "version": VERSION,
        "allow_entry": bool(allow_entry),
        "tier": tier,
        "evidence_score": round(evidence, 2),
        "leverage": round(clamp(leverage, 1.0, 20.0), 2),
        "capital_allocation_pct": round(clamp(capital, 0.0, 50.0), 2),
        "risk_pct": round(clamp(risk_pct, 0.0, 2.0), 4),
        "stop_distance_pct": round(stop_distance_pct, 4),
        "learning_multiplier": round(learning_multiplier, 4),
        "reason": reasons,
        "caps": {
            "max_leverage": round(max_leverage, 2),
            "max_capital_allocation_pct": round(max_capital, 2),
            "max_risk_pct": round(max_risk, 4),
        },
    }
