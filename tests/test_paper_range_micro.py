from app.services.paper_range_micro import (
    RANGE_MAX_LEVERAGE,
    RANGE_RISK_PER_TRADE,
    analyze_range_klines,
    net_profit_gate,
    size_range_position,
)


def _row(index: int, close: float, volume: float = 1000.0):
    ts = index * 300_000
    return [ts, close, close + 0.10, close - 0.10, close, volume, ts + 299_999, close * volume, 10, 0, 0, 0]


def test_lateral_range_near_lower_edge_can_be_long_candidate():
    pattern = [99.20, 99.60, 100.40, 100.80, 100.40, 99.60]
    closes = [pattern[i % len(pattern)] for i in range(95)] + [99.25]
    rows = [_row(i, close, 1000.0 + (i % 5) * 20.0) for i, close in enumerate(closes)]

    result = analyze_range_klines(rows)

    assert result["actionable"] is True
    assert result["strategy_mode"] == "RANGE_MICRO"
    assert result["side"] == "LONG"
    assert result["stop_loss"] < result["entry_reference"] < result["take_profit"]
    assert result["range_position"] <= 0.22


def test_clear_trend_is_not_misclassified_as_range_trade():
    closes = [100.0 + i * 0.18 for i in range(96)]
    rows = [_row(i, close) for i, close in enumerate(closes)]

    result = analyze_range_klines(rows)

    assert result["actionable"] is False
    assert result["reason"] in {"range_width", "not_lateral", "not_at_range_edge", "possible_breakout"}


def test_range_position_sizing_caps_risk_and_leverage():
    sizing = size_range_position(balance=1000.0, entry=100.0, stop=99.0, leverage=20)

    assert sizing["leverage"] == RANGE_MAX_LEVERAGE
    assert sizing["risk_usdt"] == 1000.0 * RANGE_RISK_PER_TRADE
    assert sizing["margin"] <= 1000.0 * 0.15 + 1e-9


def test_net_profit_gate_counts_costs_before_accepting_small_profit():
    good = net_profit_gate(
        side="LONG",
        entry=100.0,
        target=101.0,
        quantity=1.0,
        notional=100.0,
        risk_usdt=0.50,
    )
    too_small = net_profit_gate(
        side="LONG",
        entry=100.0,
        target=100.10,
        quantity=1.0,
        notional=100.0,
        risk_usdt=0.50,
    )

    assert good["projected_gross_pnl"] > good["projected_net_pnl"]
    assert good["allowed"] is True
    assert too_small["allowed"] is False
    assert too_small["projected_net_pnl"] < 0.50
