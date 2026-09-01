from app.services.market_breadth_persistence import _alignment, _short_frame
from app.services.risk_conviction_engine import build_risk_conviction


def _matrix(direction="LONG"):
    return {
        "consensus": direction,
        "horizon_conflict": False,
        "horizons": {
            h: {"direction": direction, "edge": 20}
            for h in ("15m", "1h", "4h", "6h", "24h")
        },
    }


def _lane(**extra):
    value = {
        "direction": "LONG",
        "ignition_score": 80,
        "execution_math": {"chosen_target": {"net_rr": 3.0}},
        "event_risk_multiplier": 1.0,
    }
    value.update(extra)
    return value


def test_breadth_frame_detects_broad_decline():
    frame = _short_frame([-1.2, -0.7, -0.4, -0.2, 0.01])
    assert frame["decline_pct"] >= 80
    assert frame["score"] < 0


def test_strong_bearish_breadth_conflicts_with_long():
    alignment = _alignment("LONG", -70, "PANIC_DOWN")
    assert alignment["state"] == "STRONG_CONFLICT"
    assert alignment["risk_multiplier"] <= 0.45


def test_calibrating_shadow_history_does_not_change_conviction():
    base = build_risk_conviction(
        lane_name="TACTICAL",
        lane=_lane(),
        setup_score=80,
        risk_score=30,
        forecast_matrix=_matrix(),
        elliott_structure={},
    )
    immature = build_risk_conviction(
        lane_name="TACTICAL",
        lane=_lane(
            shadow_calibration_status="CALIBRATING",
            shadow_calibration_sample=29,
            shadow_conviction_adjustment=5,
        ),
        setup_score=80,
        risk_score=30,
        forecast_matrix=_matrix(),
        elliott_structure={},
    )
    assert immature["conviction_score"] == base["conviction_score"]


def test_mature_shadow_adjustment_is_bounded_and_breadth_still_dominates():
    positive = build_risk_conviction(
        lane_name="TACTICAL",
        lane=_lane(
            shadow_calibration_status="USABLE",
            shadow_calibration_sample=60,
            shadow_conviction_adjustment=50,  # must be clipped to +5
            breadth_risk_multiplier=1.0,
        ),
        setup_score=78,
        risk_score=35,
        forecast_matrix=_matrix(),
        elliott_structure={},
    )
    conflict = build_risk_conviction(
        lane_name="TACTICAL",
        lane=_lane(
            shadow_calibration_status="USABLE",
            shadow_calibration_sample=60,
            shadow_conviction_adjustment=50,
            breadth_risk_multiplier=0.45,
            market_breadth_regime="PANIC_DOWN",
            market_breadth_score=-75,
            breadth_alignment="STRONG_CONFLICT",
        ),
        setup_score=78,
        risk_score=35,
        forecast_matrix=_matrix(),
        elliott_structure={},
    )
    assert positive["shadow_calibration"]["bounded_adjustment"] == 5.0
    assert conflict["risk_budget_multiplier"] < positive["risk_budget_multiplier"]
