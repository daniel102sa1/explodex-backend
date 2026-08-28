from app.services.paper_micro_scalp import (
    MICRO_MAX_LEVERAGE,
    MICRO_RISK_PER_TRADE,
    analyze_micro_scalp,
    micro_net_profit_gate,
    size_micro_position,
)


def _row(index: int, open_: float, close: float, volume: float = 1000.0):
    high = max(open_, close) + 0.10
    low = min(open_, close) - 0.10
    ts = index * 300_000
    return [ts, open_, high, low, close, volume, ts + 299_999, close * volume, 10, 0, 0, 0]


def test_micro_scalp_accepts_mild_liquid_trend_without_chase():
    rows = []
    price = 100.0
    for i in range(72):
        drift = 0.035 if i % 3 else 0.015
        open_ = price
        price += drift
        rows.append(_row(i, open_, price, 1000.0 + (i % 7) * 25.0))

    result = analyze_micro_scalp(rows)

    assert result["eligible"] is True
    assert result["strategy_mode"] == "MICRO_SCALP"
    assert result["side"] == "LONG"
    assert result["stop_loss"] < result["entry_reference"] < result["take_profit"]
    assert result["tier"] in {"STANDARD", "EXPLORATION"}


def test_micro_scalp_rejects_dead_market():
    rows = [_row(i, 100.0, 100.001, 1000.0) for i in range(72)]

    result = analyze_micro_scalp(rows)

    assert result["eligible"] is False
    assert result["reason"] in {"too_dead", "no_direction"}


def test_micro_scalp_sizing_caps_risk_and_leverage():
    sizing = size_micro_position(balance=1000.0, entry=100.0, stop=99.5, leverage=20)

    assert sizing["leverage"] == MICRO_MAX_LEVERAGE
    assert sizing["risk_usdt"] == 1000.0 * MICRO_RISK_PER_TRADE
    assert sizing["margin"] <= 1000.0 * 0.12 + 1e-9


def test_micro_cost_gate_accepts_net_positive_and_rejects_tiny_move():
    good = micro_net_profit_gate(
        side="LONG",
        entry=100.0,
        target=100.8,
        quantity=1.0,
        notional=100.0,
        risk_usdt=0.20,
    )
    tiny = micro_net_profit_gate(
        side="LONG",
        entry=100.0,
        target=100.12,
        quantity=1.0,
        notional=100.0,
        risk_usdt=0.20,
    )

    assert good["projected_gross_pnl"] > good["projected_net_pnl"]
    assert good["allowed"] is True
    assert tiny["allowed"] is False
