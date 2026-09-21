from app.services.macro_cycle_engine import build_macro_cycle


def _daily(i, o, h, l, c, qv):
    ts = 1_650_000_000_000 + i * 86_400_000
    return [ts, str(o), str(h), str(l), str(c), "1000", ts + 86_399_999, str(qv), 0, 0, 0, 0]


def _long_base(count=1095):
    rows = []
    for i in range(count):
        if i < 600:
            price = 200.0 - i * 0.23
        else:
            j = i - 600
            floor = 48.0 + j * 0.012
            wave = (j % 30) / 30.0 * 2.5
            price = floor + wave
        o = price * 0.998
        c = price
        h = price * 1.012
        l = price * 0.988
        qv = max(250_000.0, 2_000_000.0 - i * 1200.0)
        rows.append(_daily(i, o, h, l, c, qv))
    return rows


def _btc(count=1095):
    rows = []
    price = 20000.0
    for i in range(count):
        price *= 1.00045
        rows.append(_daily(i, price * 0.998, price * 1.01, price * 0.99, price, 5_000_000))
    return rows


def test_macro_cycle_detects_multi_year_base_without_creating_entry():
    result = build_macro_cycle(_long_base(), btc_rows=_btc(), source="TEST")

    assert result["available"] is True
    assert result["history_days"] >= 1000
    assert result["complete_3y"] is True
    assert result["can_create_entry"] is False
    assert result["score_is_probability"] is False
    assert result["state"] in {"ACCUMULATION", "ACCUMULATION_LATE", "RANGE_OR_TRANSITION", "MARKUP"}
    assert result["accumulation_score"] >= 50
    assert result["suggested_watch_horizon"] in {"3D_14D", "1D_7D"}


def test_macro_cycle_reports_short_history_as_unavailable():
    result = build_macro_cycle(_long_base(50), btc_rows=_btc(50))
    assert result["available"] is False
    assert result["reason"] == "insufficient_daily_history"
    assert result["can_create_entry"] is False


def test_macro_radar_liquidity_filter_accepts_real_usdt_universe_symbols():
    from app.services.macro_cycle_persistence import _eligible_macro_ticker

    assert _eligible_macro_ticker({"symbol": "ZECUSDT", "quoteVolume": 50_000_000}) is True
    assert _eligible_macro_ticker({"symbol": "ZEC_USDT", "quoteVolume": 50_000_000}) is False
    assert _eligible_macro_ticker({"symbol": "ZECBTC", "quoteVolume": 50_000_000}) is False
    assert _eligible_macro_ticker({"symbol": "ZECUSDT", "quoteVolume": 1}) is False
