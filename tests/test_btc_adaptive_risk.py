from app.services.paper_regime_router import btc_adaptive_overlay, btc_side_risk_multiplier
from app.services.structure_retest_strategy import detect_structure_retest


def _kline(i, o, h, l, c, v=1000.0):
    ts = 1_700_000_000_000 + i * 300_000
    return [ts, str(o), str(h), str(l), str(c), str(v), ts + 299_999, str(v * c), 0, 0, 0, 0]


def _steady(count=240, start=100.0, step=0.01):
    rows = []
    price = start
    for i in range(count):
        nxt = price + step
        rows.append(_kline(i, price, max(price, nxt) + 0.03, min(price, nxt) - 0.03, nxt, 1000))
        price = nxt
    return rows


def _steady_15m(count=160):
    return _steady(count=count, start=100.0, step=0.02)


def test_btc_overlay_stays_normal_on_stable_market():
    overlay = btc_adaptive_overlay(_steady(), _steady_15m())

    assert overlay["stress"] in {"NORMAL", "ELEVATED"}
    assert overlay["risk_multiplier"] >= 0.75
    assert overlay["block_new_entries"] is False
    assert 1.0 <= overlay["stop_buffer_multiplier"] <= 1.08


def test_btc_overlay_blocks_new_entries_on_shock():
    rows5 = _steady()
    last = float(rows5[-1][4])
    rows5.append(_kline(241, last, last * 1.026, last * 0.998, last * 1.022, 5000))
    rows15 = _steady_15m()
    last15 = float(rows15[-1][4])
    rows15.append(_kline(161, last15, last15 * 1.03, last15 * 0.997, last15 * 1.025, 8000))

    overlay = btc_adaptive_overlay(rows5, rows15)

    assert overlay["stress"] == "SHOCK"
    assert overlay["block_new_entries"] is True
    assert overlay["risk_multiplier"] == 0.0
    assert overlay["force_defensive"] is True


def test_countertrend_risk_is_reduced_when_btc_is_high_stress():
    overlay = {
        "stress": "HIGH",
        "direction": "BULLISH",
        "countertrend_multiplier": 0.35,
        "block_new_entries": False,
    }

    aligned, aligned_reason = btc_side_risk_multiplier("LONG", overlay)
    counter, counter_reason = btc_side_risk_multiplier("SHORT", overlay)

    assert aligned == 1.0
    assert aligned_reason is None
    assert counter == 0.35
    assert "countertrend_reduced" in str(counter_reason)


def test_extreme_countertrend_is_blocked():
    overlay = {
        "stress": "EXTREME",
        "direction": "BEARISH",
        "countertrend_multiplier": 0.0,
        "block_new_entries": False,
    }

    multiplier, reason = btc_side_risk_multiplier("LONG", overlay)

    assert multiplier == 0.0
    assert "countertrend_block" in str(reason)


def _retest_rows():
    rows = []
    for i in range(50):
        center = 99.7 + (i % 4) * 0.04
        rows.append(_kline(i, center, 100.35, 99.25, center + 0.05, 1000 + (i % 5) * 20))
    rows.append(_kline(50, 100.0, 101.35, 99.95, 101.10, 2600))
    rows.append(_kline(51, 101.05, 101.15, 100.25, 100.75, 1500))
    rows.append(_kline(52, 100.75, 101.35, 100.55, 101.20, 1600))
    rows.append(_kline(53, 101.20, 102.10, 100.95, 101.90, 1900))
    rows.append(_kline(54, 101.90, 102.35, 101.45, 102.15, 1800))
    rows.append(_kline(55, 102.15, 102.30, 101.90, 102.20, 900))
    return rows


def _bullish_1h():
    rows = []
    price = 96.0
    for i in range(80):
        nxt = price + 0.08
        rows.append(_kline(i, price, nxt + 0.25, price - 0.20, nxt, 3000 + i * 10))
        price = nxt
    return rows


def test_btc_stress_can_widen_future_retest_stop_but_not_change_structure():
    base = detect_structure_retest(
        direction="LONG",
        current_price=101.95,
        klines_15m=_retest_rows(),
        klines_1h=_bullish_1h(),
        btc_stop_buffer_multiplier=1.0,
    )
    stressed = detect_structure_retest(
        direction="LONG",
        current_price=101.95,
        klines_15m=_retest_rows(),
        klines_1h=_bullish_1h(),
        btc_stop_buffer_multiplier=1.30,
    )

    assert stressed["structural_level"] == base["structural_level"]
    assert stressed["structural_stop"] < base["structural_stop"]
    assert stressed["stop_policy"]["btc_stop_buffer_multiplier"] == 1.3
    assert stressed["stop_policy"]["stop_never_widens_after_entry"] is True
