from app.services.paper_edge_lab import classify_bucket, wilson_interval


def test_small_sample_stays_exploratory():
    result = classify_bucket({
        "trades": 10,
        "wins": 9,
        "net_pnl": 12.0,
        "expectancy_net": 1.2,
        "profit_factor": 5.0,
    })
    assert result["state"] == "INSUFFICIENT_SAMPLE"
    assert result["risk_multiplier"] < 1.0


def test_negative_expectancy_is_paused_after_sample():
    result = classify_bucket({
        "trades": 25,
        "wins": 12,
        "net_pnl": -8.0,
        "expectancy_net": -0.32,
        "profit_factor": 0.70,
    })
    assert result["state"] == "PAUSE"
    assert result["risk_multiplier"] == 0.0


def test_eighty_target_requires_sample_and_positive_edge():
    result = classify_bucket({
        "trades": 60,
        "wins": 50,
        "net_pnl": 22.0,
        "expectancy_net": 22.0 / 60.0,
        "profit_factor": 2.1,
    })
    assert result["state"] == "EIGHTY_TARGET_RESEARCH"
    assert result["win_rate_pct"] > 80.0
    assert result["win_rate_wilson_low_pct"] >= 65.0
    assert result["is_probability_forecast"] is False


def test_wilson_interval_is_conservative():
    low, high = wilson_interval(8, 10)
    assert low is not None and high is not None
    assert low < 0.80 < high
