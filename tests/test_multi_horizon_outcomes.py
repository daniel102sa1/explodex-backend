from app.services.multi_horizon_outcomes import HORIZON_LABELS, HORIZONS_MINUTES, evaluate_horizon


def candles(*rows):
    return [
        {"time": float(t), "open": o, "high": h, "low": l, "close": c}
        for t, o, h, l, c in rows
    ]


def test_required_long_horizons_are_present():
    assert 240 in HORIZONS_MINUTES
    assert 360 in HORIZONS_MINUTES
    assert 1440 in HORIZONS_MINUTES
    assert HORIZON_LABELS[240] == "4h"
    assert HORIZON_LABELS[360] == "6h"
    assert HORIZON_LABELS[1440] == "24h"


def test_long_horizon_measures_direction_mfe_mae_and_r():
    start = 1_000_000
    rows = candles(
        (start, 100, 101, 99, 100.5),
        (start + 60_000, 100.5, 103, 100, 102.0),
        (start + 120_000, 102, 104, 101, 103.0),
    )
    result = evaluate_horizon(
        candles=rows,
        observed_ms=start,
        horizon_minutes=240,
        direction="LONG",
        entry=100,
        stop=98,
        tp1=102,
        resolution_minutes=1,
    )
    assert result is not None
    assert result["directional_return_pct"] == 3.0
    assert result["mfe_pct"] == 4.0
    assert result["mae_pct"] == 1.0
    assert result["favorable_r"] == 2.0
    assert result["tp1_hit_by_horizon"] is True
    assert result["expansion_class"] == "STRONG_EXPANSION"


def test_short_horizon_is_direction_adjusted():
    start = 2_000_000
    rows = candles(
        (start, 100, 100.5, 99, 99.5),
        (start + 60_000, 99.5, 100, 96, 97.0),
    )
    result = evaluate_horizon(
        candles=rows,
        observed_ms=start,
        horizon_minutes=360,
        direction="SHORT",
        entry=100,
        stop=102,
        tp1=98,
        resolution_minutes=1,
    )
    assert result is not None
    assert result["directional_return_pct"] == 3.0
    assert result["mfe_pct"] == 4.0
    assert result["mae_pct"] == 0.5
    assert result["tp1_hit_by_horizon"] is True


def test_long_horizon_does_not_turn_direction_wrong_into_success():
    start = 3_000_000
    rows = candles((start, 100, 101, 96, 97))
    result = evaluate_horizon(
        candles=rows,
        observed_ms=start,
        horizon_minutes=1440,
        direction="LONG",
        entry=100,
        stop=98,
        tp1=104,
        resolution_minutes=15,
    )
    assert result is not None
    assert result["directional_return_pct"] == -3.0
    assert result["stop_hit_by_horizon"] is True
    assert result["expansion_class"] == "DIRECTION_WRONG"
