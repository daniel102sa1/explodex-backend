from __future__ import annotations

"""
Terra Controller

Combines Terra sensors before a PAPER trading decision.
This layer keeps decisions explainable: pattern quality, market context,
risk and AI reasoning remain separate signals.
"""

from typing import Any

from app.services.terra_risk_engine import evaluate_risk


def build_terra_context(
    *,
    pattern: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pattern = pattern or {}
    market = market or {}
    decision = decision or {}

    risk = evaluate_risk(
        confidence_score=float(decision.get("confidence_score", 0)),
        stop_distance_pct=float(decision.get("stop_distance_pct", 0)),
        volatility_score=float(market.get("volatility_score", 50)),
        liquidity_score=float(market.get("liquidity_score", 50)),
        timeframe_alignment=float(market.get("timeframe_alignment", 50)),
        pattern_score=float(pattern.get("score", 0)),
        btc_context_score=float(market.get("btc_context_score", 50)),
    )

    return {
        "pattern_engine": pattern,
        "market_context": market,
        "terra_decision": decision,
        "adaptive_risk": risk,
        "paper_mode": True,
    }


def should_allow_entry(context: dict[str, Any]) -> bool:
    risk = context.get("adaptive_risk", {})
    decision = context.get("terra_decision", {})

    if decision.get("action") != "ENTER":
        return False

    return float(risk.get("evidence_score", 0)) >= 55
