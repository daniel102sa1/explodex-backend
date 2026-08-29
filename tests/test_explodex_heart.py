from app.services.explodex_heart import _canonical_gate, _market_event


def _score(state="READY", direction="LONG"):
    return {
        "direction": direction,
        "state": state,
        "risk_score": 25,
        "current_price": 100.0,
        "entry_low": 99.8,
        "entry_high": 100.2,
        "stop_loss": 98.5,
        "tp1": 102.0,
        "tp2": 103.0,
        "tp3": 105.0,
        "metrics": {
            "change_5m_pct": 0.1,
            "change_15m_pct": 0.2,
            "volume_acceleration": 1.3,
            "relative_volume": 1.4,
            "oi_change_pct": 0.3,
            "futures_delta_ratio": 0.08,
            "spot_delta_ratio": 0.06,
            "order_book_imbalance": 0.08,
        },
    }


def test_heart_downgrades_ready_when_trigger_not_activated():
    prediction = {
        "direction": "LONG",
        "phase": "PREACTIVACION",
        "preactivation_score": 82,
        "sequence": {"chase_risk": False, "risk_guard_pass": True},
    }
    out = _canonical_gate(_score(), prediction)
    assert out["state"] == "PREPARING"
    assert "heart_trigger_not_activated" in out["metrics"]["reject_reasons"]


def test_heart_blocks_chasing_even_with_high_score():
    prediction = {
        "direction": "LONG",
        "phase": "ACTIVADO",
        "preactivation_score": 91,
        "sequence": {"chase_risk": True, "risk_guard_pass": True},
    }
    out = _canonical_gate(_score(), prediction)
    assert out["state"] == "PREPARING"
    assert "heart_no_chase" in out["metrics"]["reject_reasons"]


def test_heart_marks_pre_explosion_loading_without_calling_it_probability():
    prediction = {
        "direction": "LONG",
        "type": "IMPULSO_LONG",
        "phase": "PREACTIVACION",
        "preactivation_score": 80,
        "sequence": {"chase_risk": False},
        "premove_fingerprint": {"fingerprint_score": 84},
        "prediction_stack_v5": {"master_decision": {"state": "YES"}},
    }
    event = _market_event(_score(state="PREPARING"), prediction)
    assert event["event"] == "PRE_EXPLOSION_LOADING"
    assert event["direction"] == "LONG"
    assert event["index_is_probability"] is False
    assert "open_interest_expanding" in event["evidence"]


def test_heart_distinguishes_sweep_rebound():
    prediction = {
        "direction": "LONG",
        "type": "REBOTE_LONG",
        "phase": "VIGILAR_CONFIRMACION",
        "preactivation_score": 76,
        "sequence": {"sweep_low": True, "sell_absorption_rebound": True},
        "premove_fingerprint": {"fingerprint_score": 75},
        "prediction_stack_v5": {"master_decision": {"state": "YES"}},
    }
    event = _market_event(_score(state="PREPARING"), prediction)
    assert event["event"] == "LIQUIDITY_SWEEP_REBOUND"
    assert "sweep_low_reclaimed" in event["evidence"]
    assert "seller_absorption" in event["evidence"]
