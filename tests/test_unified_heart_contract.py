from app.services.unified_heart_contract import build_execution_contract


def _prediction(chase=False, veto=False):
    return {
        "sequence": {"chase_risk": chase, "risk_guard_pass": True},
        "decision_guard": {"risk_guard_pass": True},
        "prediction_stack_v5": {
            "risk_veto": {
                "blocked": veto,
                "hard_block": veto,
                "invalidated": False,
                "chase": chase,
            }
        },
    }


def _score():
    return {
        "direction": "LONG",
        "state": "PREPARING",
        "risk_score": 25.0,
        "current_price": 100.0,
        "expected_duration_min_minutes": 20,
        "expected_duration_max_minutes": 120,
    }


def _heart(action="ESPERAR", should_enter=False):
    return {
        "direction": "LONG",
        "execution_allowed": should_enter,
        "action_decision": {
            "action": action,
            "should_enter": should_enter,
            "direction": "LONG",
            "execution_target_name": "TP2",
            "execution_target_price": 105.0,
            "reason": "test",
        },
        "plan": {
            "entry_low": 99.5,
            "entry_high": 100.5,
            "stop_loss": 98.0,
            "tp1": 103.0,
            "tp2": 105.0,
            "tp3": 108.0,
        },
        "thesis": {
            "frozen_plan": True,
            "status": "WAITING_ENTRY",
            "action": "ESPERAR_ENTRADA",
        },
        "ignition": {
            "score": 80.0,
            "stage": "ARMED",
            "supporting_components": 4,
        },
        "trajectory_forecast": {
            "direction": "LONG",
            "trajectory_score": 78.0,
            "direction_edge": 30.0,
            "horizon": "8-24h",
            "max_hold_minutes": 1440,
            "should_enter_paper_swing": True,
            "blockers": [],
            "expected_ranges": {"24h_pct": 8.0, "48h_pct": 11.0},
            "swing_plan": {
                "entry_low": 99.0,
                "entry_high": 101.0,
                "structural_stop": 96.0,
                "target1": 110.4,
                "target2": 114.0,
                "target3": 118.0,
                "target_zone_low": 114.0,
                "target_zone_high": 118.0,
                "max_hold_minutes": 1440,
            },
        },
    }


def test_tactical_enter_has_absolute_priority():
    heart = _heart("ENTRAR_LONG", True)
    result = build_execution_contract(heart=heart, score=_score(), prediction=_prediction())
    assert result["single_source_of_truth"] is True
    assert result["permitted_paper_lane"] == "TACTICAL"
    assert result["primary_action"] == "ENTRAR_LONG"


def test_waiting_heart_can_authorize_only_one_experimental_lane():
    heart = _heart()
    result = build_execution_contract(heart=heart, score=_score(), prediction=_prediction())
    assert result["primary_action"] == "ESPERAR"
    assert result["permitted_paper_lane"] in {"AGGRESSIVE_PAPER", "SWING_PAPER", None}
    eligible = [
        lane for lane in result["lanes"].values()
        if lane.get("eligible")
    ]
    # Multiple lanes can qualify as evidence, but contract emits only one lane.
    if eligible:
        assert result["permitted_paper_lane"] == eligible[0]["lane"]


def test_hard_veto_blocks_experimental_entries():
    heart = _heart()
    result = build_execution_contract(heart=heart, score=_score(), prediction=_prediction(veto=True))
    assert result["hard_safety_clear"] is False
    assert "hard_veto" in result["hard_safety_blockers"]
    assert result["lanes"]["aggressive_paper"]["eligible"] is False
    assert result["lanes"]["swing_paper"]["eligible"] is False


def test_contract_never_changes_primary_user_action_for_swing():
    heart = _heart()
    result = build_execution_contract(heart=heart, score=_score(), prediction=_prediction())
    assert result["primary_action"] == "ESPERAR"
    assert result["forecast"]["source"] in {"TACTICAL_HEART", "TRAJECTORY_4H_48H"}
    if result["permitted_paper_lane"] == "SWING_PAPER":
        assert result["lanes"]["swing_paper"]["paper_only"] is True
