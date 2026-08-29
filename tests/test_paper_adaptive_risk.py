from app.services.paper_adaptive_risk import adaptive_geometry, adaptive_leverage, market_direction_guard


def test_blocks_long_into_market_dump():
    regime = {
        "regime": "HIGH_VOLATILITY",
        "btc": {"ret_15m_pct": -1.4, "ret_60m_pct": -2.4},
        "eth": {"ret_15m_pct": -1.0, "ret_60m_pct": -1.8},
    }
    result = market_direction_guard("LONG", regime)
    assert result["allowed"] is False
    assert result["risk_multiplier"] == 0.0


def test_rewards_short_aligned_with_dump():
    regime = {
        "regime": "TREND_DOWN",
        "btc": {"ret_15m_pct": -0.5, "ret_60m_pct": -1.0},
        "eth": {"ret_15m_pct": -0.4, "ret_60m_pct": -0.9},
    }
    result = market_direction_guard("SHORT", regime)
    assert result["allowed"] is True
    assert result["risk_multiplier"] > 1.0


def test_high_quality_aligned_setup_can_use_six_x():
    assert adaptive_leverage(
        grade="A+",
        fingerprint_score=92,
        catalyst_state="SUPPORTIVE",
        regime_aligned=True,
        defensive=False,
    ) == 6


def test_defensive_mode_forces_one_x():
    assert adaptive_leverage(
        grade="A+",
        fingerprint_score=95,
        catalyst_state="SUPPORTIVE",
        regime_aligned=True,
        defensive=True,
    ) == 1


def test_adaptive_geometry_widens_stop_without_increasing_risk_budget_and_extends_target():
    result = adaptive_geometry(
        side="LONG",
        entry=100.0,
        original_stop=99.5,
        original_tp=101.0,
        atr=0.8,
        fingerprint_score=91,
    )
    assert result["stop"] < 99.5
    assert result["tp"] > 102.0
    assert result["rr"] >= 2.4
    assert result["stop_widened"] is True


def test_stop_width_has_hard_cap():
    result = adaptive_geometry(
        side="SHORT",
        entry=100.0,
        original_stop=100.2,
        original_tp=99.5,
        atr=10.0,
        fingerprint_score=95,
    )
    assert result["stop"] <= 102.5
    assert result["rr"] >= 2.4
