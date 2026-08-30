from app.services.heart_persistence import _canonical_action
from app.services.ignition_engine import build_ignition_signal


def _score(direction="LONG"):
    return {
        "direction": direction,
        "state": "PREPARING",
        "risk_score": 24,
        "current_price": 100.0,
        "entry_low": 99.8,
        "entry_high": 100.2,
        "stop_loss": 98.7 if direction == "LONG" else 101.3,
        "tp1": 102.0 if direction == "LONG" else 98.0,
        "metrics": {
            "futures_delta_ratio": 0.16 if direction == "LONG" else -0.16,
            "spot_delta_ratio": 0.12 if direction == "LONG" else -0.12,
            "order_book_imbalance": 0.11 if direction == "LONG" else -0.11,
            "change_5m_pct": 1.1 if direction == "LONG" else -1.1,
            "change_15m_pct": 1.4 if direction == "LONG" else -1.4,
            "oi_change_pct": 0.62,
            "volume_acceleration": 1.95,
            "relative_volume": 1.75,
        },
    }


def _prediction(direction="LONG"):
    return {
        "direction": direction,
        "phase": "PREACTIVACION",
        "preactivation_score": 91,
        "sequence": {"chase_risk": False, "risk_guard_pass": True},
        "premove_fingerprint": {"fingerprint_score": 90, "trade_now_ready": False, "trade_class": "WATCH"},
        "prediction_stack_v5": {
            "master_decision": {"state": "YES"},
            "entry_timing": {"state": "WAIT"},
            "risk_veto": {"blocked": False, "chase": False, "invalidated": False, "hard_block": False},
        },
    }


def _thesis():
    return {"frozen_plan": True, "status": "WAITING_ENTRY", "action": "ESPERAR_ENTRADA"}


def test_ignition_can_authorize_before_legacy_trade_now():
    score = _score()
    prediction = _prediction()
    ignition = build_ignition_signal(score, prediction)
    assert ignition["fast_path_ready"] is True
    decision = _canonical_action(score, prediction, _thesis(), ignition)
    assert decision["should_enter"] is True
    assert decision["action"] == "ENTRAR_LONG"
    assert decision["via"] == "IGNITION_FAST_PATH"


def test_ignition_never_bypasses_hard_veto():
    score = _score()
    prediction = _prediction()
    prediction["prediction_stack_v5"]["risk_veto"]["blocked"] = True
    prediction["prediction_stack_v5"]["risk_veto"]["hard_block"] = True
    ignition = build_ignition_signal(score, prediction)
    assert ignition["fast_path_ready"] is False
    decision = _canonical_action(score, prediction, _thesis(), ignition)
    assert decision["should_enter"] is False
    assert decision["action"] == "NO_ENTRAR"


def test_ignition_never_bypasses_risk_guard():
    score = _score("SHORT")
    prediction = _prediction("SHORT")
    prediction["sequence"]["risk_guard_pass"] = False
    ignition = build_ignition_signal(score, prediction)
    assert ignition["fast_path_ready"] is False
    decision = _canonical_action(score, prediction, _thesis(), ignition)
    assert decision["should_enter"] is False
    assert decision["action"] == "NO_ENTRAR"
