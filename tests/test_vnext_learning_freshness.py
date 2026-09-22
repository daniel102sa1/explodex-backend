import inspect

from app.services.scanner import _merge_open_position_tickers
from app.services.shadow_forecast_memory import _due_horizons, _select_risk_calibration, evaluate_shadow_forecasts


def test_open_paper_symbols_are_forced_into_deep_scan():
    selected = [
        {"symbol": "BTCUSDT", "quoteVolume": "1000000"},
        {"symbol": "ETHUSDT", "quoteVolume": "900000"},
    ]
    tickers = selected + [
        {"symbol": "LITUSDT", "quoteVolume": "1000"},
        {"symbol": "AVAXUSDT", "quoteVolume": "800000"},
    ]

    merged = _merge_open_position_tickers(
        selected,
        tickers,
        {"BTCUSDT", "LITUSDT"},
    )

    symbols = [row["symbol"] for row in merged]
    assert symbols.count("BTCUSDT") == 1
    assert "LITUSDT" in symbols


def test_shadow_due_horizons_skip_already_matured_and_future_windows():
    outcomes = {
        "15m": {"mature": True},
        "1h": {"mature": False},
    }

    assert _due_horizons(30, outcomes) == []
    assert _due_horizons(90, outcomes) == ["1h"]
    assert _due_horizons(300, outcomes) == ["1h", "4h"]



def test_shadow_evaluator_prioritizes_recent_due_rows():
    source = inspect.getsource(evaluate_shadow_forecasts)
    assert "ORDER BY observed_at DESC" in source



def test_shadow_risk_calibration_prefers_usable_one_hour():
    one_hour = {"rows": [{"direction": "LONG", "sample": 40, "status": "USABLE", "bounded_conviction_adjustment": 2.5}]}
    fifteen = {"rows": [{"direction": "LONG", "sample": 100, "status": "USABLE", "bounded_conviction_adjustment": -5.0}]}
    selected = _select_risk_calibration("LONG", one_hour, fifteen)
    assert selected["source_horizon"] == "1h"
    assert selected["bounded_conviction_adjustment"] == 2.5
    assert selected["short_horizon_can_only_reduce_risk"] is False


def test_usable_15m_fallback_cannot_raise_risk():
    one_hour = {"rows": [{"direction": "LONG", "sample": 0, "status": "CALIBRATING", "bounded_conviction_adjustment": 0.0}]}
    fifteen = {"rows": [{"direction": "LONG", "sample": 80, "status": "USABLE", "bounded_conviction_adjustment": 5.0}]}
    selected = _select_risk_calibration("LONG", one_hour, fifteen)
    assert selected["source_horizon"] == "15m"
    assert selected["bounded_conviction_adjustment"] == 0.0
    assert selected["short_horizon_can_only_reduce_risk"] is True


def test_usable_15m_fallback_preserves_negative_brake():
    one_hour = {"rows": []}
    fifteen = {"rows": [{"direction": "SHORT", "sample": 80, "status": "USABLE", "bounded_conviction_adjustment": -5.0}]}
    selected = _select_risk_calibration("SHORT", one_hour, fifteen)
    assert selected["source_horizon"] == "15m"
    assert selected["bounded_conviction_adjustment"] == -5.0



def test_calibrating_fallback_reports_actual_selected_horizon():
    one_hour = {"rows": [{"direction": "LONG", "sample": 2, "status": "CALIBRATING", "bounded_conviction_adjustment": 0.0}]}
    fifteen = {"rows": [{"direction": "LONG", "sample": 20, "status": "CALIBRATING", "bounded_conviction_adjustment": 0.0}]}
    selected = _select_risk_calibration("LONG", one_hour, fifteen)
    assert selected["source_horizon"] == "15m"
    assert selected["short_horizon_can_only_reduce_risk"] is True


def test_shadow_calibration_groups_by_horizon_forecast_direction():
    from app.services.shadow_forecast_memory import shadow_calibration_report
    source = inspect.getsource(shadow_calibration_report)
    assert "forecast #>> ARRAY[:h,'direction']" in source
