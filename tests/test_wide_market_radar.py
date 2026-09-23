from app.services.wide_market_radar import score_radar_candidate


def k(i, o, h, l, c, qv):
    start = 1_700_000_000_000 + i * 300_000
    base_v = qv / max(c, 1e-9)
    return [start, str(o), str(h), str(l), str(c), str(base_v), start + 299_999, str(qv), 0, 0, 0, 0]


def quiet_rows():
    rows = []
    p = 100.0
    for i in range(36):
        c = p + (0.03 if i % 2 == 0 else -0.02)
        rows.append(k(i, p, max(p, c) + 0.08, min(p, c) - 0.08, c, 100_000))
        p = c
    return rows


def early_pump_rows():
    rows = quiet_rows()
    p = float(rows[-4][4])
    rows[-3] = k(33, p, p + 0.18, p - 0.05, p + 0.14, 240_000)
    p = float(rows[-3][4])
    rows[-2] = k(34, p, p + 0.22, p - 0.04, p + 0.17, 330_000)
    p = float(rows[-2][4])
    rows[-1] = k(35, p, p + 0.25, p - 0.03, p + 0.19, 430_000)
    return rows


def test_wide_radar_rewards_early_volume_and_momentum():
    quiet = score_radar_candidate(
        {"symbol": "QUIETUSDT", "priceChangePercent": "0.3", "quoteVolume": "20000000"},
        quiet_rows(),
    )
    pump = score_radar_candidate(
        {"symbol": "PUMPUSDT", "priceChangePercent": "2.1", "quoteVolume": "20000000"},
        early_pump_rows(),
    )
    assert pump["available"] is True
    assert pump["bias"] == "LONG"
    assert pump["priority_score"] > quiet["priority_score"]
    assert pump["relative_volume"] > 1.5
    assert pump["volume_acceleration"] > 1.2


def test_wide_radar_is_ranking_only_not_entry_engine():
    result = score_radar_candidate(
        {"symbol": "TESTUSDT", "priceChangePercent": "1", "quoteVolume": "20000000"},
        early_pump_rows(),
    )
    assert result["can_create_entry"] is False
    assert result["score_is_probability"] is False


def test_wide_radar_penalizes_already_extended_24h_move():
    base = score_radar_candidate(
        {"symbol": "AUSDT", "priceChangePercent": "2", "quoteVolume": "20000000"},
        early_pump_rows(),
    )
    extended = score_radar_candidate(
        {"symbol": "BUSDT", "priceChangePercent": "15", "quoteVolume": "20000000"},
        early_pump_rows(),
    )
    assert extended["priority_score"] < base["priority_score"]
