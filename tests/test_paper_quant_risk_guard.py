from app.services.paper_quant_risk_guard import build_quant_metrics, evaluate_quant_guard


def _row(pnl: float, risk: float = 1.0):
    return {"net_pnl": pnl, "risk_usdt": risk}


def test_quant_metrics_include_sharpe_sortino_cvar_and_deflated_proxy():
    rows = [_row(1.2), _row(0.8), _row(-1.0), _row(1.1), _row(-0.7)] * 5
    metrics = build_quant_metrics(rows, recent_window=20, trials=8)

    assert metrics["sample"] == 20
    assert metrics["trade_sharpe"] is not None
    assert metrics["trade_sortino"] is not None
    assert metrics["cvar_95_r"] is not None
    assert metrics["deflated_sharpe_proxy"] <= metrics["trade_sharpe"]
    assert metrics["deflated_sharpe_is_exact"] is False


def test_six_consecutive_losses_halts_new_entries():
    rows = [_row(-1.0)] * 6 + [_row(1.0)] * 14
    metrics = build_quant_metrics(rows, recent_window=20, trials=4)
    guard = evaluate_quant_guard(metrics=metrics, net_24h=-5.0)

    assert guard["state"] == "HALT_NEW_ENTRIES"
    assert guard["risk_multiplier"] == 0.0
    assert "six_consecutive_losses" in guard["hard_reasons"]
    assert guard["can_create_entry"] is False
    assert guard["can_change_direction"] is False


def test_moderate_drawdown_reduces_without_halting():
    rows = [_row(1.0), _row(-1.0)] * 10
    metrics = build_quant_metrics(rows, recent_window=20, trials=2)
    guard = evaluate_quant_guard(metrics=metrics, net_24h=-12.0)

    assert guard["state"] in {"REDUCE", "REDUCE_HARD"}
    assert 0.0 < guard["risk_multiplier"] < 1.0
    assert guard["halt_new_entries"] is False


def test_healthy_recent_distribution_allows_normal_risk():
    rows = [_row(1.4), _row(1.0), _row(-0.6), _row(1.2), _row(-0.5)] * 4
    metrics = build_quant_metrics(rows, recent_window=20, trials=3)
    guard = evaluate_quant_guard(metrics=metrics, net_24h=8.0)

    assert guard["state"] == "ALLOW"
    assert guard["risk_multiplier"] == 1.0
    assert guard["halt_new_entries"] is False
