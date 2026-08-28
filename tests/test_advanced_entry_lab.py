from app.services.advanced_entry_lab import build_lead_lag, build_toxic_flow, combine_advanced_signals


def _trade(price: float, qty: float, seller_is_maker: bool):
    return {"p": str(price), "q": str(qty), "m": seller_is_maker}


def _klines(prices):
    rows = []
    for i, price in enumerate(prices):
        rows.append([i * 300000, str(price), str(price * 1.001), str(price * 0.999), str(price), "100"])
    return rows


def test_toxic_flow_detects_sell_pressure():
    book = {
        "bids": [["99.9", "5"], ["99.8", "4"]],
        "asks": [["100.1", "25"], ["100.2", "20"]],
    }
    futures = [_trade(100, 5, True) for _ in range(40)] + [_trade(100, 1, False) for _ in range(5)]
    spot = [_trade(100, 3, True) for _ in range(20)] + [_trade(100, 1, False) for _ in range(5)]
    result = build_toxic_flow(
        order_book=book,
        futures_trades=futures,
        spot_trades=spot,
        klines=_klines([100, 99.95, 99.93, 99.92, 99.90]),
    )
    assert result["available"] is True
    assert result["directional_score"] < 0
    assert result["state"] in {"SELL_PRESSURE", "MIXED"}


def test_lead_lag_can_identify_major_led_long_bias():
    alt = _klines([100, 100.01, 100.02, 100.03, 100.04, 100.05, 100.06, 100.07])
    btc = _klines([100, 100.1, 100.2, 100.35, 100.5, 100.7, 100.9, 101.1])
    eth = _klines([100, 100.08, 100.16, 100.28, 100.4, 100.55, 100.75, 100.95])
    result = build_lead_lag(symbol="ALTUSDT", symbol_klines=alt, btc_klines=btc, eth_klines=eth)
    assert result["available"] is True
    assert result["directional_score"] > 0


def test_failure_or_strong_conflict_can_veto():
    toxic = {"available": True, "data_quality": 1.0, "directional_score": -70}
    lead = {"available": True, "data_quality": 1.0, "directional_score": -65}
    failure = {"available": True, "failure_risk": 80, "veto": True}
    result = combine_advanced_signals(side="LONG", toxic_flow=toxic, lead_lag=lead, failure=failure)
    assert result["veto"] is True
    assert result["state"] == "VETO"
    assert result["risk_multiplier"] == 0.0
