from app.services.paper_aggressive_learning import aggressive_candidate_ok


def _base():
    return {
        "signal_state": "PREPARING",
        "risk_score": 28.0,
        "heart": {},
        "thesis": {"frozen_plan": True, "status": "WAITING_ENTRY"},
        "decision": {"action": "ESPERAR", "should_enter": False},
        "ignition": {
            "stage": "ARMED",
            "score": 78.0,
            "supporting_components": 4,
            "blockers": [],
        },
    }


def test_aggressive_candidate_allows_armed_reduced_risk_sample():
    ok, blockers = aggressive_candidate_ok(**_base())
    assert ok is True
    assert blockers == []


def test_aggressive_candidate_rejects_canonical_no_enter():
    data = _base()
    data["decision"] = {"action": "NO_ENTRAR", "should_enter": False}
    ok, blockers = aggressive_candidate_ok(**data)
    assert ok is False
    assert "action_no_entrar" in blockers


def test_aggressive_candidate_rejects_retest_chase():
    data = _base()
    data["decision"] = {"action": "ESPERAR_RETEST", "should_enter": False}
    data["ignition"]["blockers"] = ["not_chasing"]
    ok, blockers = aggressive_candidate_ok(**data)
    assert ok is False
    assert "action_esperar_retest" in blockers
    assert "not_chasing" in blockers


def test_aggressive_candidate_rejects_hard_guard_failure():
    data = _base()
    data["ignition"]["blockers"] = ["risk_guard_pass", "veto_clear"]
    ok, blockers = aggressive_candidate_ok(**data)
    assert ok is False
    assert "risk_guard_pass" in blockers
    assert "veto_clear" in blockers


def test_aggressive_candidate_rejects_weak_ignition():
    data = _base()
    data["ignition"]["score"] = 70.0
    data["ignition"]["stage"] = "LOADING"
    ok, blockers = aggressive_candidate_ok(**data)
    assert ok is False
    assert "ignition_not_armed" in blockers
    assert "ignition_below_76" in blockers


def test_aggressive_candidate_rejects_high_risk():
    data = _base()
    data["risk_score"] = 50.0
    ok, blockers = aggressive_candidate_ok(**data)
    assert ok is False
    assert "risk_too_high" in blockers
