from app.services.heart_persistence import _stack_checks


def test_persisted_heart_uses_one_real_timing_trigger_not_three_duplicate_labels():
    prediction = {
        "phase": "PREACTIVACION",
        "premove_fingerprint": {"trade_now_ready": True, "trade_class": "TRADE_NOW"},
        "sequence": {"chase_risk": False, "risk_guard_pass": True},
        "decision_guard": {"risk_guard_pass": True},
        "prediction_stack_v5": {
            "master_decision": {"state": "WAIT"},
            "entry_timing": {"state": "WATCH"},
            "risk_veto": {"blocked": False, "hard_block": False, "invalidated": False, "chase": False},
        },
    }
    ready, missing = _stack_checks(prediction)
    assert ready is True
    assert missing == []


def test_persisted_heart_still_requires_independent_hard_safety():
    prediction = {
        "phase": "ACTIVADO",
        "premove_fingerprint": {"trade_now_ready": False},
        "sequence": {"chase_risk": True, "risk_guard_pass": True},
        "prediction_stack_v5": {
            "risk_veto": {"blocked": False, "hard_block": False, "invalidated": False, "chase": True},
        },
    }
    ready, missing = _stack_checks(prediction)
    assert ready is False
    assert "not_chasing" in missing
