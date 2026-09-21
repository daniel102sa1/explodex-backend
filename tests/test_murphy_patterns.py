from app.services.murphy_patterns import (
    build_murphy_pattern_context,
    murphy_pattern_registry,
)


def _row(i: int, open_: float, high: float, low: float, close: float, quote_volume: float = 100000.0):
    ts = i * 300_000
    base_volume = quote_volume / max(close, 1e-9)
    return [ts, open_, high, low, close, base_volume, ts + 299_999, quote_volume, 10, 0, 0, 0]


def _ascending_triangle_rows():
    rows = []
    # Broad earlier context.
    for i in range(90):
        center = 100.0 + i * 0.05
        rows.append(_row(i, center - 0.2, center + 1.2, center - 1.0, center + 0.1, 130000.0))

    # Last 30 bars: flat resistance near 110 with clearly rising lows.
    for j in range(30):
        i = 90 + j
        high = 110.0 + (0.015 if j % 4 == 0 else -0.01 if j % 5 == 0 else 0.0)
        low = 104.8 + j * 0.145
        close = min(high - 0.08, low + 3.2)
        open_ = close - 0.08
        volume = 95000.0 if j < 15 else 65000.0
        rows.append(_row(i, open_, high, low, close, volume))
    return rows


def test_registry_contains_core_murphy_pattern_families_and_is_shadow_only():
    registry = murphy_pattern_registry()
    names = {item["name"] for item in registry["patterns"]}

    assert "ASCENDING_TRIANGLE" in names
    assert "DESCENDING_TRIANGLE" in names
    assert "SYMMETRICAL_TRIANGLE" in names
    assert "BULL_FLAG_OR_PENNANT" in names
    assert "DOUBLE_TOP" in names
    assert "DOUBLE_BOTTOM" in names
    assert "HEAD_AND_SHOULDERS" in names
    assert registry["policy"]["can_create_entry"] is False
    assert registry["policy"]["can_raise_leverage"] is False


def test_ascending_triangle_is_detected_as_forming_long_evidence():
    result = build_murphy_pattern_context(
        {"metrics": {"atr_pct": 0.7}},
        {"klines": _ascending_triangle_rows()},
        {"direction": "LONG"},
    )

    assert result["available"] is True
    names = [p["name"] for p in result["patterns"]]
    assert "ASCENDING_TRIANGLE" in names
    triangle = next(p for p in result["patterns"] if p["name"] == "ASCENDING_TRIANGLE")
    assert triangle["bias"] == "LONG"
    assert triangle["state"] in {"FORMING", "BREAKOUT_CONFIRMED"}
    assert "flat_resistance" in triangle["evidence"]
    assert "rising_lows" in triangle["evidence"]
    assert result["policy"]["can_create_entry"] is False


def test_pattern_library_refuses_to_invent_signal_with_too_little_data():
    rows = [_row(i, 100, 101, 99, 100.2) for i in range(20)]
    result = build_murphy_pattern_context({}, {"klines": rows}, {})

    assert result["available"] is False
    assert result["reason"] == "insufficient_klines"
