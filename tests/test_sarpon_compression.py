from app.services.prediction_engine import build_pre_move_prediction
from app.services.sarpon_compression import build_sarpon_compression_context


def _row(i: int, open_: float, high: float, low: float, close: float, quote_volume: float):
    ts = i * 300_000
    base_volume = quote_volume / max(close, 1e-9)
    return [
        ts, open_, high, low, close, base_volume,
        ts + 299_999, quote_volume, 10, 0, 0, 0,
    ]


def _ascending_compression_rows():
    rows = []
    # Older broad range gives the squeeze something to contract from.
    for i in range(36):
        center = 103.0 + (i % 6) * 0.35
        rows.append(_row(i, center, center + 2.6, center - 2.4, center + 0.1, 160_000.0))

    # Mid-stage tightening.
    for j in range(12):
        i = 36 + j
        low = 104.2 + j * 0.08
        high = 108.2
        close = min(high - 0.18, low + 2.8)
        rows.append(_row(i, close - 0.12, high, low, close, 115_000.0))

    # Final pressure: flat resistance, rising lows, smaller bodies/volume.
    for j in range(12):
        i = 48 + j
        low = 105.8 + j * 0.16
        high = 108.05 + (0.01 if j % 3 == 0 else 0.0)
        close = min(high - 0.05, low + 1.55)
        open_ = close - 0.05
        rows.append(_row(i, open_, high, low, close, 65_000.0))
    return rows


def _flat_low_vol_rows():
    rows = []
    for i in range(60):
        center = 100.0 + (0.03 if i % 2 == 0 else -0.03)
        rows.append(_row(i, center, 100.25, 99.75, center + 0.01, 80_000.0))
    return rows


def test_directional_compression_is_armed_early():
    rows = _ascending_compression_rows()
    result = build_sarpon_compression_context(
        rows,
        {
            "atr_pct": 0.55,
            "futures_delta_ratio": 0.01,
            "spot_delta_ratio": 0.01,
        },
    )

    assert result["available"] is True
    assert result["direction"] == "LONG"
    assert result["stage"] == "ARMED_EARLY"
    assert result["compression_score"] >= 72
    assert result["directional_pressure_score"] >= 68
    assert result["priority_bonus"] >= 22
    assert result["policy"]["can_create_entry_alone"] is False


def test_low_volatility_without_directional_pressure_does_not_get_priority_bonus():
    rows = _flat_low_vol_rows()
    result = build_sarpon_compression_context(rows, {"atr_pct": 0.5})

    assert result["available"] is True
    assert result["direction"] == "NEUTRAL"
    assert result["priority_bonus"] == 0.0
    assert result["stage"] in {"SQUEEZE_NEUTRAL", "NO_COMPRESSION_EDGE"}


def test_prediction_surfaces_clean_compression_before_breakout():
    rows = _ascending_compression_rows()
    scored = {
        "direction": "LONG",
        "metrics": {
            "atr_pct": 0.55,
            "compressed": True,
            "compression_ratio": 0.40,
            "volume_acceleration": 0.90,
            "relative_volume": 0.85,
            "futures_delta_ratio": 0.01,
            "spot_delta_ratio": 0.01,
            "order_book_imbalance": 0.02,
            "change_5m_pct": 0.05,
            "change_15m_pct": 0.12,
            "taker_avg_3": 1.0,
            "btc_trend": "NEUTRAL",
            "trend_15m": "BULLISH",
            "trend_1h": "BULLISH",
        },
    }

    prediction = build_pre_move_prediction(scored, {"klines": rows}, {})

    assert prediction["direction"] == "LONG"
    assert prediction["sarpon_compression"]["stage"] == "ARMED_EARLY"
    assert prediction["sequence"]["compression_priority_stage"] == "ARMED_EARLY"
    assert prediction["phase"] in {"PREACTIVACION", "ACTIVADO"}
    assert prediction["preactivation_score"] >= 60
    assert "COMPRESIÓN SARPON LONG" in prediction["title"]
