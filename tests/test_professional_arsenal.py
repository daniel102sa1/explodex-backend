from app.services.prediction_engine import build_pre_move_prediction
from app.services.professional_arsenal import build_professional_arsenal_context


def k(i, o, h, l, c, v=100.0):
    start = 1_700_000_000_000 + i * 300_000
    return [start, str(o), str(h), str(l), str(c), str(v), start + 299_999, str(v * c), 0, 0, 0, 0]


def flat_then_breakout_retest_rows():
    rows = []
    for i in range(44):
        close = 100.1 if i % 2 else 99.9
        rows.append(k(i, 100.0, 101.0, 99.0, close, 100 + i))
    rows.extend([
        k(44, 100.0, 102.3, 99.9, 102.0, 400),
        k(45, 102.0, 102.1, 100.95, 101.3, 350),
        k(46, 101.3, 101.8, 101.1, 101.6, 330),
        k(47, 101.6, 102.0, 101.4, 101.9, 360),
    ])
    return rows


def trending_rows(n=240):
    rows = []
    p = 100.0
    for i in range(n):
        o = p
        c = p + 0.20
        h = c + 0.10
        l = o - 0.08
        rows.append(k(i, o, h, l, c, 100 + i))
        p = c
    return rows


def buy_trades(start=100.0, n=60):
    return [
        {"p": str(start + i * 0.01), "q": "3", "m": False, "T": 1_700_000_000_000 + i * 1000}
        for i in range(n)
    ]


def sell_trades(start=100.6, n=60):
    return [
        {"p": str(start - i * 0.01), "q": "3", "m": True, "T": 1_700_000_000_000 + i * 1000}
        for i in range(n)
    ]


def base_metrics(**extra):
    out = {
        "change_5m_pct": 0.25,
        "change_15m_pct": 0.55,
        "change_1h_pct": 1.1,
        "relative_volume": 2.0,
        "volume_acceleration": 1.45,
        "oi_change_pct": 0.55,
        "funding_rate": 0.0001,
        "order_book_imbalance": 0.12,
        "order_book_spread_bps": 2.0,
        "atr_pct": 0.8,
        "compression_ratio": 0.65,
        "compressed": True,
        "btc_trend": "BULLISH",
        "btc_change_15m_pct": 0.2,
        "btc_change_1h_pct": 0.5,
        "futures_delta_ratio": 0.15,
        "spot_delta_ratio": 0.12,
        "taker_avg_3": 1.2,
    }
    out.update(extra)
    return out


def test_professional_arsenal_uses_indicators_as_confirmation_not_trigger():
    result = build_professional_arsenal_context(
        {"metrics": base_metrics()},
        {
            "klines": trending_rows(),
            "agg_trades": buy_trades(),
            "spot_agg_trades": buy_trades(),
            "premium": {"markPrice": "148.1", "indexPrice": "148.0", "lastFundingRate": "0.0001"},
        },
        {},
    )
    assert result["available"] is True
    assert result["policy"]["indicators_are_confirmation_not_standalone_triggers"] is True
    assert result["policy"]["can_create_entry_by_itself"] is False
    assert result["ema_context"]["ema200_available"] is True
    assert result["momentum"]["adx"]["available"] is True
    assert result["volatility"]["bollinger_bandwidth"]["available"] is True


def test_breakout_retest_and_pump_confluence_are_detected():
    result = build_professional_arsenal_context(
        {"metrics": base_metrics()},
        {
            "klines": flat_then_breakout_retest_rows(),
            "agg_trades": buy_trades(100.0),
            "spot_agg_trades": buy_trades(100.0),
            "premium": {"markPrice": "101.9", "indexPrice": "101.85", "lastFundingRate": "0.0001"},
        },
        {
            "liquidations": {
                "available": True,
                "short_minus_long_imbalance_1h": 0.35,
            }
        },
    )
    names = {x["name"] for x in result["patterns"]["patterns"]}
    assert "BREAKOUT_RETEST_LONG" in names
    assert result["pump_hunter"]["bias"] == "LONG"
    assert result["pump_hunter"]["state"] in {"ARMED", "STRONG"}
    assert result["pump_hunter"]["can_create_entry"] is False


def test_cvd_proxy_separates_spot_and_futures_flow():
    result = build_professional_arsenal_context(
        {"metrics": base_metrics(futures_delta_ratio=-0.15, spot_delta_ratio=0.15)},
        {
            "klines": flat_then_breakout_retest_rows(),
            "agg_trades": sell_trades(),
            "spot_agg_trades": buy_trades(),
            "premium": {},
        },
        {},
    )
    flow = result["flow"]
    assert flow["futures_cvd_proxy"]["normalized_delta"] < 0
    assert flow["spot_cvd_proxy"]["normalized_delta"] > 0
    assert flow["spot_vs_futures"] == "SPOT_BUYING_FUTURES_SELLING"


def test_prediction_engine_exposes_professional_arsenal_context():
    rows = flat_then_breakout_retest_rows()
    scored = {
        "direction": "LONG",
        "current_price": float(rows[-1][4]),
        "metrics": base_metrics(),
    }
    prediction = build_pre_move_prediction(
        scored,
        {
            "klines": rows,
            "agg_trades": buy_trades(),
            "spot_agg_trades": buy_trades(),
            "premium": {"markPrice": "101.9", "indexPrice": "101.85", "lastFundingRate": "0.0001"},
        },
        {},
    )
    assert "professional_arsenal" in prediction
    assert prediction["professional_arsenal"]["available"] is True
    assert prediction["professional_arsenal"]["score_is_probability"] is False
    assert prediction["professional_arsenal"]["policy"]["can_create_entry_by_itself"] is False
