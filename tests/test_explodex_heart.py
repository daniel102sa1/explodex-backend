from app.services.explodex_heart import _action_decision, _canonical_gate, _market_event, _stack_actionable


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


def _trade_now_prediction(direction="LONG", phase="PREACTIVACION"):
    return {
        "direction": direction,
        "type": "IMPULSO_LONG" if direction == "LONG" else "IMPULSO_SHORT",
        "phase": phase,
        "preactivation_score": 86,
        "trigger_price": 99.9,
        "entry_low": 99.8,
        "entry_high": 100.2,
        "stop_loss": 98.5 if direction == "LONG" else 101.5,
        "tp1": 102.0 if direction == "LONG" else 98.0,
        "tp2": 103.0 if direction == "LONG" else 97.0,
        "tp3": 105.0 if direction == "LONG" else 95.0,
        "sequence": {"chase_risk": False, "risk_guard_pass": True},
        "premove_fingerprint": {
            "trade_now_ready": True,
            "trade_class": "TRADE_NOW",
            "fingerprint_score": 84,
        },
        "prediction_stack_v5": {
            "master_decision": {"state": "YES"},
            "entry_timing": {"state": "ENTER_NOW"},
            "risk_veto": {
                "blocked": False,
                "chase": False,
                "invalidated": False,
                "hard_block": False,
            },
        },
    }


def test_heart_downgrades_ready_when_trigger_not_activated_and_stack_not_ready():
    prediction = {
        "direction": "LONG",
        "phase": "PREACTIVACION",
        "preactivation_score": 82,
        "sequence": {"chase_risk": False, "risk_guard_pass": True},
    }
    out = _canonical_gate(_score(), prediction)
    assert out["state"] == "PREPARING"
    assert "heart_trigger_not_activated" in out["metrics"]["reject_reasons"]


def test_trade_now_stack_can_promote_preactivation_to_ready():
    prediction = _trade_now_prediction()
    ready, missing = _stack_actionable(prediction)
    assert ready is True
    assert missing == []

    out = _canonical_gate(_score(state="PREPARING"), prediction)
    assert out["state"] == "READY"
    assert out["metrics"]["ready_via"] == "ADVANCED_STACK"


def test_heart_emits_explicit_enter_long_when_trade_now_and_in_zone():
    prediction = _trade_now_prediction()
    canonical = _canonical_gate(_score(state="PREPARING"), prediction)
    thesis = {
        "frozen_plan": True,
        "status": "WAITING_ENTRY",
        "direction": "LONG",
        "entry_low": 99.8,
        "entry_high": 100.2,
        "stop_loss": 98.5,
        "tp1": 102.0,
    }
    plan = {
        "entry_low": 99.8,
        "entry_high": 100.2,
        "stop_loss": 98.5,
        "tp1": 102.0,
    }
    decision = _action_decision(canonical=canonical, prediction=prediction, thesis=thesis, plan=plan)
    assert decision["should_enter"] is True
    assert decision["action"] == "ENTRAR_LONG"
    assert decision["price_in_entry_zone"] is True


def test_heart_blocks_chasing_even_with_high_score():
    prediction = _trade_now_prediction(phase="ACTIVADO")
    prediction["sequence"]["chase_risk"] = True
    prediction["prediction_stack_v5"]["risk_veto"]["chase"] = True
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
        "prediction_stack_v5": {"master_decision": {"state": "WAIT"}},
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
        "prediction_stack_v5": {"master_decision": {"state": "WAIT"}},
    }
    event = _market_event(_score(state="PREPARING"), prediction)
    assert event["event"] == "LIQUIDITY_SWEEP_REBOUND"
    assert "sweep_low_reclaimed" in event["evidence"]
    assert "seller_absorption" in event["evidence"]
