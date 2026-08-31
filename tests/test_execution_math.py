from app.services.execution_math import choose_target_for_min_net_rr, evaluate_trade_math


def test_net_rr_includes_costs_and_is_below_gross_rr():
    result = evaluate_trade_math(
        side="LONG",
        entry=100.0,
        stop=99.0,
        target=103.0,
        expected_hold_hours=2.0,
    )
    assert result["valid"] is True
    assert result["gross_rr"] == 3.0
    assert 0 < result["net_rr"] < 3.0
    assert result["breakeven_win_rate_pct"] < 30.0


def test_choose_target_skips_small_tp_and_uses_farther_tp():
    result = choose_target_for_min_net_rr(
        side="LONG",
        entry=100.0,
        stop=99.0,
        targets=[("TP1", 101.0), ("TP2", 102.0), ("TP3", 103.5)],
        expected_hold_hours=2.0,
        min_net_rr=2.5,
    )
    assert result["accepted"] is True
    assert result["chosen_target"]["name"] == "TP3"
    assert result["chosen_target"]["net_rr"] >= 2.5


def test_trade_rejected_when_no_target_compensates_risk():
    result = choose_target_for_min_net_rr(
        side="SHORT",
        entry=100.0,
        stop=102.0,
        targets=[("TP1", 99.0), ("TP2", 98.5), ("TP3", 98.0)],
        expected_hold_hours=4.0,
        min_net_rr=2.0,
    )
    assert result["accepted"] is False
    assert result["reason"] == "no_target_meets_min_net_rr"
