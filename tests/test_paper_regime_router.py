from app.services.paper_regime_router import classify_regime


def _rows(start: float, step: float, count: int = 80, wick: float = 0.15):
    rows = []
    price = start
    for index in range(count):
        open_price = price
        close = price + step
        high = max(open_price, close) + wick
        low = min(open_price, close) - wick
        rows.append([index * 300_000, open_price, high, low, close, 1000.0])
        price = close
    return rows


def _flat(start: float, count: int = 80):
    rows = []
    for index in range(count):
        drift = 0.04 if index % 2 == 0 else -0.04
        open_price = start + drift
        close = start - drift
        rows.append([index * 300_000, open_price, start + 0.12, start - 0.12, close, 1000.0])
    return rows


def test_strong_aligned_trend_disables_range():
    btc5 = _rows(100.0, 0.22)
    btc15 = _rows(100.0, 0.55, 50)
    eth5 = _rows(50.0, 0.11)
    eth15 = _rows(50.0, 0.28, 50)
    result = classify_regime(btc5, btc15, eth5, eth15)
    assert result["regime"] == "TREND_UP"
    assert result["policy"]["range_micro"]["enabled"] is False
    assert result["policy"]["micro_scalp"]["enabled"] is True


def test_flat_market_routes_to_range():
    btc5 = _flat(100.0)
    btc15 = _flat(100.0, 50)
    eth5 = _flat(50.0)
    eth15 = _flat(50.0, 50)
    result = classify_regime(btc5, btc15, eth5, eth15)
    assert result["regime"] == "RANGE"
    assert result["policy"]["range_micro"]["enabled"] is True


def test_high_volatility_blocks_secondary_scalps():
    btc5 = _rows(100.0, 1.6, wick=1.2)
    btc15 = _rows(100.0, 2.4, 50, wick=1.8)
    eth5 = _rows(50.0, 0.9, wick=0.8)
    eth15 = _rows(50.0, 1.4, 50, wick=1.1)
    result = classify_regime(btc5, btc15, eth5, eth15)
    assert result["regime"] == "HIGH_VOLATILITY"
    assert result["policy"]["range_micro"]["enabled"] is False
    assert result["policy"]["micro_scalp"]["enabled"] is False
