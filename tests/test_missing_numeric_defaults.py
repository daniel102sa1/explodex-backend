from app.services.binance_position_coach import _f as coach_f
from app.services.position_continuation_engine import _f as continuation_f
from app.services.prediction_guarded import _path_forecast_input


def test_numeric_helpers_preserve_explicit_default_for_missing_values():
    for helper in (coach_f, continuation_f):
        assert helper(None, 50.0) == 50.0
        assert helper("", 50.0) == 50.0
        assert helper("bad", 50.0) == 50.0
        assert helper(0, 50.0) == 0.0
        assert helper("0", 50.0) == 0.0


def test_path_forecast_missing_metrics_are_neutral_not_bearish_zeroes():
    result = {
        "preactivation_score": None,
        "verdict_fusion": {
            "mtf_strength": None,
            "flow_strength": "",
            "trap_risk": None,
            "decay_risk": "",
            "acceleration_score": None,
            "technical_confidence": None,
        },
    }

    prepared = _path_forecast_input(result)
    fusion = prepared["verdict_fusion"]

    assert fusion["mtf_strength"] == 50.0
    assert fusion["flow_strength"] == 50.0
    assert fusion["trap_risk"] == 50.0
    assert fusion["decay_risk"] == 50.0
    assert fusion["acceleration_score"] == 50.0
    assert fusion["technical_confidence"] == 50.0


def test_path_forecast_keeps_real_zero_values():
    result = {
        "preactivation_score": 73,
        "verdict_fusion": {
            "mtf_strength": 0,
            "flow_strength": 0,
            "trap_risk": 0,
            "decay_risk": 0,
            "acceleration_score": 0,
            "technical_confidence": 0,
        },
    }

    prepared = _path_forecast_input(result)
    assert all(prepared["verdict_fusion"][key] == 0 for key in (
        "mtf_strength",
        "flow_strength",
        "trap_risk",
        "decay_risk",
        "acceleration_score",
        "technical_confidence",
    ))
