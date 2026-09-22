from app.services.vnext_evaluation import _normalized_risk_report


def test_normalized_risk_report_rescales_net_pnl_by_actual_stop_risk():
    report = _normalized_risk_report(
        [
            {
                "id": 1,
                "symbol": "BTCUSDT",
                "side": "LONG",
                "risk_usdt": 0.10,
                "net_pnl": 0.30,
            }
        ],
        starting_balance=1000.0,
    )
    assert report["usable_closed_trades"] == 1
    assert report["scenarios"]["risk_0_50_pct"]["aggregate_normalized_net_pnl"] == 15.0
    assert report["scenarios"]["risk_1_00_pct"]["aggregate_normalized_net_pnl"] == 30.0
    trade = report["recent_trades"][0]
    assert trade["realized_r_multiple_net"] == 3.0
    assert trade["scenarios"]["risk_0_50_pct"]["target_risk_usdt"] == 5.0


def test_normalized_risk_report_ignores_rows_without_real_risk():
    report = _normalized_risk_report(
        [{"id": 2, "symbol": "X", "side": "LONG", "risk_usdt": 0.0, "net_pnl": 99.0}],
        starting_balance=1000.0,
    )
    assert report["usable_closed_trades"] == 0
    assert report["scenarios"]["risk_0_50_pct"]["aggregate_normalized_net_pnl"] == 0.0
