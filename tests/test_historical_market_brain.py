from app.services.historical_market_brain import (
    POLICY,
    _distance,
    _feature_vector,
    _normalize,
    _outcomes,
    _similarity,
)


def _rows(n: int = 520):
    rows = []
    price = 100.0
    for i in range(n):
        drift = 0.03 if (i // 40) % 2 == 0 else -0.015
        o = price
        c = max(1.0, price + drift + ((i % 7) - 3) * 0.005)
        h = max(o, c) + 0.18
        l = min(o, c) - 0.18
        qv = 100_000 + (i % 13) * 2500
        rows.append([i * 300_000, o, h, l, c, qv])
        price = c
    return rows


def test_historical_features_are_point_in_time_and_ignore_future_mutation():
    rows = _rows()
    btc = _rows()
    idx = 220
    times = [int(row[0]) for row in btc]

    before = _feature_vector(rows, idx, btc_rows=btc, btc_times=times)

    # Mutate only candles AFTER the observation. Point-in-time features must not change.
    for i in range(idx + 1, len(rows)):
        rows[i][1] *= 4
        rows[i][2] *= 5
        rows[i][3] *= 0.5
        rows[i][4] *= 4
        rows[i][5] *= 20

    after = _feature_vector(rows, idx, btc_rows=btc, btc_times=times)
    assert before == after


def test_outcomes_use_future_path_and_keep_directional_excursions():
    rows = _rows()
    idx = 180
    outcomes = _outcomes(rows, idx)
    assert set(outcomes) == {"15m", "1h", "4h", "12h", "24h"}
    assert outcomes["1h"]["up_mfe_pct"] >= 0
    assert outcomes["1h"]["down_mfe_pct"] >= 0
    assert outcomes["1h"]["long_barrier_1p5atr_vs_1atr"]["outcome"] in {
        "TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS_SAME_BAR", "NONE"
    }


def test_similarity_is_highest_for_same_feature_vector():
    current = {
        "change_5m_pct": 0.3,
        "change_15m_pct": 0.8,
        "change_1h_pct": 1.4,
        "atr_pct": 0.9,
        "relative_volume": 1.8,
        "volume_acceleration": 1.4,
        "compression_ratio": 0.55,
        "ema_spread_pct": 0.3,
        "range_position_48": 0.75,
        "trend": "BULLISH",
        "btc_regime": "BULLISH",
        "pattern_name": "BULL_FLAG",
        "pattern_bias": "LONG",
    }
    same = dict(current)
    far = dict(current)
    far.update({
        "change_5m_pct": -2.5,
        "change_15m_pct": -5.0,
        "change_1h_pct": -8.0,
        "relative_volume": 0.3,
        "trend": "BEARISH",
        "btc_regime": "BEARISH",
        "pattern_name": "DOUBLE_TOP",
        "pattern_bias": "SHORT",
    })
    same_score = _similarity(_distance(current, same))
    far_score = _similarity(_distance(current, far))
    assert same_score == 100.0
    assert far_score < same_score


def test_normalize_prefers_quote_volume_when_available():
    raw = [[0, "100", "101", "99", "100.5", "3", 0, "9999"]]
    row = _normalize(raw)[0]
    assert row[5] == 9999.0


def test_historical_brain_cannot_authorize_entry_or_leverage():
    assert POLICY["shadow_only"] is True
    assert POLICY["can_create_entry"] is False
    assert POLICY["can_raise_leverage"] is False
    assert POLICY["point_in_time_features_only"] is True
