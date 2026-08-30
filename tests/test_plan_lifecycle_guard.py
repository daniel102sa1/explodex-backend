from app.services.plan_lifecycle_guard import plan_target_exhausted


def _thesis(direction="LONG", status="NO_CHASE", tp3=105.0):
    return {
        "frozen_plan": True,
        "status": status,
        "direction": direction,
        "tp3": tp3,
    }


def test_long_plan_is_exhausted_after_price_passes_tp3_without_entry():
    assert plan_target_exhausted(_thesis("LONG", "NO_CHASE", 105.0), 105.01) is True


def test_short_plan_is_exhausted_after_price_passes_tp3_without_entry():
    assert plan_target_exhausted(_thesis("SHORT", "NO_CHASE", 95.0), 94.99) is True


def test_plan_before_tp3_can_still_wait_for_retest():
    assert plan_target_exhausted(_thesis("LONG", "NO_CHASE", 105.0), 103.0) is False


def test_open_position_is_never_expired_just_because_tp3_was_passed():
    assert plan_target_exhausted(_thesis("LONG", "IN_POSITION", 105.0), 110.0) is False


def test_exhausted_plan_does_not_infer_opposite_direction():
    thesis = _thesis("LONG", "WAITING_ENTRY", 105.0)
    assert plan_target_exhausted(thesis, 106.0) is True
    assert thesis["direction"] == "LONG"
