from app.services.premove_fingerprint import build_premove_fingerprint


def test_strong_coherent_setup_can_reach_trade_now():
    prediction = {
        "direction": "LONG",
        "phase": "PREACTIVATION",
        "trigger_hit": True,
        "verdict_fusion": {
            "technical_confidence": 86,
            "mtf_strength": 82,
            "flow_strength": 78,
            "trap_risk": 24,
            "decay_risk": 22,
            "acceleration_score": 74,
            "entry_quality": 84,
            "pass_count": 6,
            "hard_block": False,
            "invalidated": False,
        },
        "entry_zone_engine": {
            "state": "OPTIMAL",
            "action": "ENTER_ZONE",
            "quality_score": 84,
            "distance_to_entry_atr": 0.15,
        },
        "sequence": {
            "chase_risk": False,
            "compressed": True,
            "sequential_microstructure_ready": True,
            "sequential_absorption": 0.6,
            "ofi": 0.5,
            "replenishment": 0.4,
        },
        "context_engine": {"microstructure": {}},
        "path_forecast": {
            "final_bias": "LONG",
            "clarity": "CLEAR",
        },
    }

    result = build_premove_fingerprint({}, {}, prediction)

    assert result["trade_now_ready"] is True
    assert result["trade_class"] == "TRADE_NOW"
    assert result["steps_to_yes"] == 0
    assert result["trigger_passes"] >= 7


def test_invalidated_setup_cannot_reach_trade_now():
    prediction = {
        "direction": "LONG",
        "trigger_hit": True,
        "verdict_fusion": {
            "technical_confidence": 90,
            "mtf_strength": 90,
            "flow_strength": 90,
            "trap_risk": 10,
            "decay_risk": 10,
            "acceleration_score": 90,
            "entry_quality": 90,
            "pass_count": 6,
            "hard_block": False,
            "invalidated": True,
        },
        "entry_zone_engine": {"state": "OPTIMAL", "action": "ENTER_ZONE", "quality_score": 90},
        "sequence": {"chase_risk": False, "compressed": True},
        "path_forecast": {"final_bias": "LONG", "clarity": "CLEAR"},
    }

    result = build_premove_fingerprint({}, {}, prediction)
    assert result["trade_class"] == "NO_TRADE"
