from datetime import datetime, timedelta, timezone

from app.services.paper_portfolio import calculate_trade_pnl, choose_leverage, size_position


def test_choose_leverage_is_conservative_on_conflict():
    assert choose_leverage("A+", 90, "CONFLICT") == 1
    assert choose_leverage("A+", 90, "SHOCK_RISK") == 1


def test_choose_leverage_scales_with_quality():
    assert choose_leverage("A+", 85, "SUPPORTIVE") == 4
    assert choose_leverage("A", 78, "NEUTRAL") == 3
    assert choose_leverage("B", 70, "NEUTRAL") == 2


def test_position_sizing_caps_margin():
    sized = size_position(1000.0, 100.0, 99.0, 4)
    assert sized["risk_usdt"] == 10.0
    assert sized["margin"] <= 300.0
    assert sized["notional"] <= 1200.0


def test_long_pnl_deducts_costs():
    opened = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(hours=2)
    result = calculate_trade_pnl(
        side="LONG",
        entry=100.0,
        exit_price=102.0,
        quantity=10.0,
        notional=1000.0,
        opened_at=opened,
        closed_at=closed,
    )
    assert result["gross_pnl"] == 20.0
    assert result["fees"] > 0
    assert result["slippage"] > 0
    assert result["funding_estimate"] > 0
    assert result["net_pnl"] < result["gross_pnl"]


def test_short_profit_is_positive():
    opened = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(minutes=30)
    result = calculate_trade_pnl(
        side="SHORT",
        entry=100.0,
        exit_price=98.0,
        quantity=10.0,
        notional=1000.0,
        opened_at=opened,
        closed_at=closed,
    )
    assert result["gross_pnl"] == 20.0
    assert result["net_pnl"] > 0
