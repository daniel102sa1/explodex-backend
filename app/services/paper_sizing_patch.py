from __future__ import annotations

from typing import Any

from app.services import paper_portfolio as base

VERSION = "paper_sizing_patch_v1_actual_stop_risk"


def corrected_size_position(balance: float, entry: float, stop: float, leverage: int) -> dict[str, float]:
    balance = float(balance or 0.0)
    entry = float(entry or 0.0)
    stop = float(stop or 0.0)
    leverage = max(1, int(leverage or 1))
    stop_distance = abs(entry - stop)
    if balance <= 0 or entry <= 0 or stop_distance <= 0:
        return {
            "risk_budget_usdt": 0.0,
            "risk_usdt": 0.0,
            "risk_pct_of_balance": 0.0,
            "quantity": 0.0,
            "notional": 0.0,
            "margin": 0.0,
        }

    risk_budget = balance * base.RISK_PER_TRADE
    quantity_by_risk = risk_budget / stop_distance
    max_margin = balance * 0.30
    max_notional = max_margin * leverage
    quantity_by_margin = max_notional / entry
    quantity = max(0.0, min(quantity_by_risk, quantity_by_margin))
    notional = quantity * entry
    margin = notional / leverage
    actual_risk = quantity * stop_distance

    return {
        "risk_budget_usdt": round(risk_budget, 6),
        "risk_usdt": round(actual_risk, 6),
        "risk_pct_of_balance": round(actual_risk / balance * 100.0, 6) if balance > 0 else 0.0,
        "quantity": round(quantity, 10),
        "notional": round(notional, 6),
        "margin": round(margin, 6),
    }


def install_corrected_paper_sizing() -> None:
    base.size_position = corrected_size_position
