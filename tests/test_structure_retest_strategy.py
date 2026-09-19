from app.services.structure_retest_strategy import detect_structure_retest


def _kline(i, o, h, l, c, v=1000.0):
    ts = 1_700_000_000_000 + i * 900_000
    return [ts, str(o), str(h), str(l), str(c), str(v), ts + 899_999, str(v * c), 0, 0, 0, 0]


def _bullish_1h():
    rows = []
    price = 96.0
    for i in range(80):
        nxt = price + 0.08
        rows.append(_kline(i, price, nxt + 0.25, price - 0.20, nxt, 3000 + i * 10))
        price = nxt
    return rows


def _long_breakout_retest():
    rows = []
    # Stable base with a ceiling around 100.4.
    for i in range(50):
        center = 99.7 + (i % 4) * 0.04
        rows.append(_kline(i, center, 100.35, 99.25, center + 0.05, 1000 + (i % 5) * 20))

    # Breakout on expanding volume.
    rows.append(_kline(50, 100.0, 101.35, 99.95, 101.10, 2600))
    # Controlled pullback/retest of old resistance.
    rows.append(_kline(51, 101.05, 101.15, 100.25, 100.75, 1500))
    # Higher-low and continuation.
    rows.append(_kline(52, 100.75, 101.35, 100.55, 101.20, 1600))
    rows.append(_kline(53, 101.20, 102.10, 100.95, 101.90, 1900))
    rows.append(_kline(54, 101.90, 102.35, 101.45, 102.15, 1800))
    # Forming bar; detector intentionally ignores newest when enough data exists.
    rows.append(_kline(55, 102.15, 102.30, 101.90, 102.20, 900))
    return rows


def test_detects_breakout_retest_continuation_with_structural_stop():
    result = detect_structure_retest(
        direction="LONG",
        current_price=101.95,
        klines_15m=_long_breakout_retest(),
        klines_1h=_bullish_1h(),
    )

    assert result["phase"] == "RETEST_CONFIRMED"
    assert result["retest_confirmed"] is True
    assert result["continuation_confirmed"] is True
    assert result["pattern_score"] >= 72
    assert result["structural_stop"] < result["structural_level"]
    assert result["structural_stop"] < result["entry_low"]
    assert result["stop_policy"]["money_loss_does_not_place_stop"] is True
    assert result["stop_policy"]["position_size_must_adapt_to_stop"] is True
    assert result["stop_policy"]["stop_never_widens_after_entry"] is True


def test_does_not_create_candidate_without_retest():
    rows = _long_breakout_retest()[:51]
    rows.extend([
        _kline(51, 101.2, 102.0, 101.15, 101.8, 1700),
        _kline(52, 101.8, 102.6, 101.7, 102.4, 1800),
        _kline(53, 102.4, 103.0, 102.2, 102.8, 1700),
        _kline(54, 102.8, 103.2, 102.6, 103.0, 900),
    ])
    result = detect_structure_retest(
        direction="LONG",
        current_price=103.0,
        klines_15m=rows,
        klines_1h=_bullish_1h(),
    )

    assert result["paper_candidate"] is False
    assert result["phase"] in {"BREAKOUT_ONLY", "NO_SETUP"}


def test_chase_limit_blocks_late_entry():
    result = detect_structure_retest(
        direction="LONG",
        current_price=105.0,
        klines_15m=_long_breakout_retest(),
        klines_1h=_bullish_1h(),
    )

    assert result["chased"] is True
    assert result["paper_candidate"] is False
