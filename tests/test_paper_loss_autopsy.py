from app.services.paper_loss_autopsy import (
    classify_loss_gate,
    classify_portfolio_brake,
    metrics_from_rows,
)


def _rows(*, trades: int, stops: int, wins: int, win_pnl: float = 0.7, loss_pnl: float = -1.0):
    rows = []
    for i in range(trades):
        if i < stops:
            rows.append({"exit_reason": "STOP", "net_pnl": loss_pnl, "fees": 0.1, "slippage": 0.05, "funding_estimate": 0.0})
        elif i < stops + wins:
            rows.append({"exit_reason": "TP1", "net_pnl": win_pnl, "fees": 0.1, "slippage": 0.05, "funding_estimate": 0.0})
        else:
            rows.append({"exit_reason": "MICRO_TIME_EXIT", "net_pnl": -0.1, "fees": 0.1, "slippage": 0.05, "funding_estimate": 0.0})
    return rows


def test_small_sample_does_not_veto():
    exact = metrics_from_rows(_rows(trades=4, stops=4, wins=0))
    gate = classify_loss_gate(exact=exact, context={"trades": 0}, strategy={"trades": 0}, recent_symbol_stops=0)
    assert gate["veto"] is False
    assert gate["state"] == "ALLOW"


def test_repeated_stop_cohort_can_veto():
    context = metrics_from_rows(_rows(trades=20, stops=15, wins=5))
    gate = classify_loss_gate(exact={"trades": 0}, context=context, strategy={"trades": 0}, recent_symbol_stops=0)
    assert gate["veto"] is True
    assert gate["state"] == "VETO"
    assert "context_repeated_stops" in gate["veto_reasons"]


def test_two_recent_symbol_stops_reduce_and_three_veto():
    neutral = {"trades": 0}
    reduced = classify_loss_gate(exact=neutral, context=neutral, strategy=neutral, recent_symbol_stops=2)
    assert reduced["veto"] is False
    assert reduced["risk_multiplier"] <= 0.35

    vetoed = classify_loss_gate(exact=neutral, context=neutral, strategy=neutral, recent_symbol_stops=3)
    assert vetoed["veto"] is True
    assert "symbol_stop_streak" in vetoed["veto_reasons"]


def test_portfolio_brake_activates_on_drawdown():
    recent = metrics_from_rows(_rows(trades=20, stops=14, wins=6))
    brake = classify_portfolio_brake(net_24h=-25.0, recent=recent)
    assert brake["mode"] == "DEFENSIVE"
    assert brake["secondary_entries_enabled"] is False
    assert brake["trend_risk_multiplier"] == 0.50


def test_portfolio_brake_stays_normal_when_recent_edge_is_positive():
    recent = metrics_from_rows(_rows(trades=20, stops=2, wins=18, win_pnl=1.0, loss_pnl=-0.5))
    brake = classify_portfolio_brake(net_24h=8.0, recent=recent)
    assert brake["mode"] == "NORMAL"
    assert brake["secondary_entries_enabled"] is True
    assert brake["trend_risk_multiplier"] == 1.0
