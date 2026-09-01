from app.services.risk_conviction_engine import build_risk_conviction


def _matrix(direction: str, *, conflict: bool = False):
    opposite = "SHORT" if direction == "LONG" else "LONG"
    horizons = {}
    for h in ("15m", "1h", "4h", "6h", "24h"):
        hdir = direction
        if conflict and h == "15m":
            hdir = opposite
        horizons[h] = {"direction": hdir, "edge": 28.0}
    return {
        "horizons": horizons,
        "consensus": direction if not conflict else "LONG" if direction == "LONG" else "SHORT",
        "horizon_conflict": conflict,
    }


def test_high_conviction_tactical_can_reach_1_5x_but_not_more():
    result = build_risk_conviction(
        lane_name="TACTICAL",
        lane={
            "direction": "LONG",
            "ignition_score": 94,
            "execution_math": {"chosen_target": {"net_rr": 3.8}},
        },
        setup_score=92,
        risk_score=20,
        forecast_matrix=_matrix("LONG"),
    )
    assert result["risk_budget_multiplier"] == 1.5
    assert result["target_account_risk_pct_before_portfolio_brakes"] == 1.5


def test_horizon_conflict_caps_risk_at_half_percent_base_multiplier():
    result = build_risk_conviction(
        lane_name="TACTICAL",
        lane={
            "direction": "LONG",
            "ignition_score": 94,
            "execution_math": {"chosen_target": {"net_rr": 3.8}},
        },
        setup_score=92,
        risk_score=20,
        forecast_matrix=_matrix("LONG", conflict=True),
    )
    assert result["risk_budget_multiplier"] <= 0.5


def test_aggressive_lane_never_gets_large_sizing():
    result = build_risk_conviction(
        lane_name="AGGRESSIVE_PAPER",
        lane={
            "direction": "SHORT",
            "ignition_score": 96,
            "execution_math": {"chosen_target": {"net_rr": 4.2}},
        },
        setup_score=95,
        risk_score=15,
        forecast_matrix=_matrix("SHORT"),
    )
    assert result["risk_budget_multiplier"] <= 0.5


def test_swing_lane_is_capped_at_1_25x():
    result = build_risk_conviction(
        lane_name="SWING_PAPER",
        lane={
            "direction": "SHORT",
            "trajectory_score": 96,
            "execution_math": {"chosen_target": {"net_rr": 4.0}},
        },
        setup_score=90,
        risk_score=18,
        forecast_matrix=_matrix("SHORT"),
    )
    assert result["risk_budget_multiplier"] <= 1.25
