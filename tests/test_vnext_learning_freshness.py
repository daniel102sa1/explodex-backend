import inspect

from app.services.scanner import _merge_open_position_tickers
from app.services.shadow_forecast_memory import _due_horizons, evaluate_shadow_forecasts


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
