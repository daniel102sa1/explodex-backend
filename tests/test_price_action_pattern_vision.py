from app.services.price_action_pattern_vision import _candles, _chart_patterns, _harmonics, detect_price_action_patterns


def _row(i, o, h, l, c, v=100.0):
    return [i * 60_000, o, h, l, c, v]


def test_harmonic_detector_accepts_gartley_like_ratios():
    pivots = [
        {"index": 1, "type": "H", "price": 100.0},
        {"index": 5, "type": "L", "price": 80.0},
        {"index": 9, "type": "H", "price": 92.36},
        {"index": 13, "type": "L", "price": 86.18},
        {"index": 17, "type": "H", "price": 95.72},
    ]
    found = _harmonics(pivots)
    assert any(item["name"] == "GARTLEY" for item in found)


def test_candle_detector_finds_bullish_engulfing():
    bars = [
        {"open": 10.0, "high": 10.2, "low": 9.8, "close": 10.1, "volume": 100, "time": 1},
        {"open": 10.1, "high": 10.15, "low": 9.75, "close": 9.8, "volume": 100, "time": 2},
        {"open": 9.75, "high": 10.3, "low": 9.7, "close": 10.2, "volume": 130, "time": 3},
        {"open": 10.2, "high": 10.25, "low": 10.1, "close": 10.22, "volume": 90, "time": 4},
    ]
    found = _candles(bars, 0.3)
    assert any(item["name"] == "BULLISH_ENGULFING" for item in found)


def test_full_detector_is_shadow_only_and_returns_levels_patterns():
    rows = []
    price = 100.0
    for i in range(70):
        drift = (i % 10 - 5) * 0.08
        o = price + drift
        c = o + (0.15 if i % 2 == 0 else -0.10)
        h = max(o, c) + 0.35
        l = min(o, c) - 0.35
        rows.append(_row(i, o, h, l, c, 100 + i))
        price += 0.02
    result = detect_price_action_patterns(rows)
    assert result["available"] is True
    assert result["policy"]["can_create_entry"] is False
    assert result["policy"]["can_raise_leverage"] is False
    assert "support_resistance" in result
    assert "candlestick_patterns" in result
    assert "harmonic_patterns" in result
    assert "market_cycle" in result



def test_chart_pattern_flat_trendline_checks_receive_atr():
    bars = [
        {"time": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100.0}
        for i in range(20)
    ]
    pivots = [
        {"index": 2, "time": 2, "type": "H", "price": 101.0},
        {"index": 4, "time": 4, "type": "L", "price": 99.0},
        {"index": 7, "time": 7, "type": "H", "price": 101.02},
        {"index": 9, "time": 9, "type": "L", "price": 99.3},
        {"index": 12, "time": 12, "type": "H", "price": 101.01},
        {"index": 14, "time": 14, "type": "L", "price": 99.6},
    ]
    found = _chart_patterns(bars, pivots, 0.5)
    assert any(item["name"] == "ASCENDING_TRIANGLE" for item in found)
