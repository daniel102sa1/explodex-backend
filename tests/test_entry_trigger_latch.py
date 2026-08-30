from app.services.entry_trigger_latch import latched_action


def _latch(direction="LONG", status="TRIGGERED"):
    return {
        "direction": direction,
        "status": status,
        "entry_low": 100.0,
        "entry_high": 101.0,
        "stop_loss": 98.0,
        "invalidation_price": 98.0,
        "tp1": 103.0,
        "tp2": 105.0,
        "tp3": 108.0,
        "milestone": "ACTIVE",
    }


def test_triggered_long_does_not_revert_to_waiting_inside_zone():
    decision = latched_action(latch=_latch(), current_price=100.5, thesis={"status": "ENTER_NOW"})
    assert decision["action"] == "MANTENER_LONG"
    assert decision["entry_latched"] is True
    assert decision["should_enter"] is True


def test_triggered_long_outside_zone_says_hold_if_entered_but_do_not_chase():
    decision = latched_action(latch=_latch(), current_price=102.0, thesis={"status": "NO_CHASE"})
    assert decision["action"] == "MANTENER_LONG"
    assert decision["should_enter"] is False
    assert "no persigas" in decision["reason"].lower()


def test_paper_in_position_is_hold_not_waiting():
    decision = latched_action(latch=_latch(), current_price=102.0, thesis={"status": "IN_POSITION"})
    assert decision["action"] == "MANTENER_LONG"
    assert decision["entry_latch_status"] == "IN_POSITION"
    assert decision["should_enter"] is False


def test_invalidated_latch_never_turns_into_waiting_or_opposite_trade():
    latch = _latch(status="INVALIDATED")
    decision = latched_action(latch=latch, current_price=97.5, thesis={"status": "INVALIDATED"})
    assert decision["action"] == "NO_ENTRAR"
    assert decision["direction"] == "LONG"
    assert decision["should_enter"] is False


def test_completed_latch_does_not_offer_fresh_entry():
    latch = _latch(status="COMPLETED")
    latch["milestone"] = "TP3"
    decision = latched_action(latch=latch, current_price=108.5, thesis={"status": "ENTER_NOW"})
    assert decision["action"] == "PLAN_COMPLETADO"
    assert decision["should_enter"] is False
