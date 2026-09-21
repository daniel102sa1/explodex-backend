from app.services.paper_fast_cycle import _probation_risk_multiplier
from app.services.paper_unified_heart_executor import (
    PROBATION_MAX_NEW_POSITIONS,
    PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP,
    _probation_lane_check,
)


def test_probation_risk_is_tiny_and_never_exceeds_ten_percent_of_normal_multiplier():
    assert _probation_risk_multiplier(1.0) == 0.10
    assert _probation_risk_multiplier(0.5) == 0.05
    assert _probation_risk_multiplier(0.0) == 0.0
    assert PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP == 0.10
    assert PROBATION_MAX_NEW_POSITIONS == 1


def test_probation_disables_aggressive_lane():
    allowed, reason = _probation_lane_check(
        lane_name="AGGRESSIVE_PAPER",
        lane={"ignition_score": 95},
        row={"risk_score": 10},
    )
    assert allowed is False
    assert reason == "probation_aggressive_disabled"


def test_probation_allows_only_clean_tactical_signal():
    allowed, reason = _probation_lane_check(
        lane_name="TACTICAL",
        lane={"eligible": True},
        row={"risk_score": 40},
    )
    assert allowed is True
    assert reason is None

    allowed, reason = _probation_lane_check(
        lane_name="TACTICAL",
        lane={"eligible": True},
        row={"risk_score": 60},
    )
    assert allowed is False
    assert reason == "probation_risk_above_48"


def test_probation_requires_stronger_swing_quality():
    allowed, reason = _probation_lane_check(
        lane_name="SWING_PAPER",
        lane={"trajectory_score": 72, "direction_edge": 20},
        row={"risk_score": 40},
    )
    assert allowed is True
    assert reason is None

    allowed, reason = _probation_lane_check(
        lane_name="SWING_PAPER",
        lane={"trajectory_score": 68, "direction_edge": 20},
        row={"risk_score": 40},
    )
    assert allowed is False
    assert reason == "probation_swing_score_below_70"
