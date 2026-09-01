from app.services.paper_unified_heart_executor import (
    DEFENSIVE_RISK_CAP,
    _defensive_lane_check,
)


def test_defensive_blocks_aggressive_lane():
    allowed, reason = _defensive_lane_check(
        lane_name="AGGRESSIVE_PAPER",
        lane={"ignition_score": 92},
        row={"risk_score": 10},
    )
    assert allowed is False
    assert reason == "defensive_aggressive_disabled"


def test_defensive_allows_strong_tactical_lane():
    allowed, reason = _defensive_lane_check(
        lane_name="TACTICAL",
        lane={"eligible": True},
        row={"risk_score": 40},
    )
    assert allowed is True
    assert reason is None


def test_defensive_rejects_high_risk_tactical_lane():
    allowed, reason = _defensive_lane_check(
        lane_name="TACTICAL",
        lane={"eligible": True},
        row={"risk_score": 80},
    )
    assert allowed is False
    assert reason == "defensive_tactical_risk_too_high"


def test_defensive_allows_strong_swing_lane():
    allowed, reason = _defensive_lane_check(
        lane_name="SWING_PAPER",
        lane={"trajectory_score": 74, "direction_edge": 22},
        row={"risk_score": 45},
    )
    assert allowed is True
    assert reason is None


def test_defensive_rejects_weak_swing_edge():
    allowed, reason = _defensive_lane_check(
        lane_name="SWING_PAPER",
        lane={"trajectory_score": 74, "direction_edge": 10},
        row={"risk_score": 45},
    )
    assert allowed is False
    assert reason == "defensive_swing_edge_below_16"


def test_defensive_risk_cap_is_quarter_normal():
    assert DEFENSIVE_RISK_CAP == 0.25
