from __future__ import annotations

"""
Terra Controller

Final gate before PAPER execution.
Combines pattern/context/AI decision with adaptive risk.
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
        confidence_score=float(decision.get("confidence_score", decision.get("evidence_score", 0))),
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


def apply_adaptive_risk(context: dict[str, Any]) -> dict[str, Any]:
    """Apply Terra risk decisions before PAPER execution."""
    decision = dict(context.get("terra_decision") or {})
    risk = dict(context.get("adaptive_risk") or {})

    if decision.get("action") != "ENTER":
        decision["allow_entry"] = False
        return decision

    decision["leverage"] = risk.get("leverage", 1.0)
    decision["capital_allocation_pct"] = risk.get("capital_allocation_pct", 5.0)
    decision["risk_tier"] = risk.get("tier")
    decision["risk_evidence_score"] = risk.get("evidence_score")

    if float(risk.get("evidence_score", 0)) < 55:
        decision["action"] = "WAIT"
        decision["allow_entry"] = False
    else:
        decision["allow_entry"] = True

    return decision


def should_allow_entry(context: dict[str, Any]) -> bool:
    final = apply_adaptive_risk(context)
    return bool(final.get("allow_entry"))
