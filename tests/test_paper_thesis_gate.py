from app.services.paper_thesis_gate import classify_price_state


def test_long_waits_inside_plan_instead_of_flipping():
    assert classify_price_state(
        direction="LONG",
        price=100.2,
        entry_low=100.0,
        entry_high=100.4,
        stop=98.8,
    ) == "ENTER_NOW"
    assert classify_price_state(
        direction="LONG",
        price=99.6,
        entry_low=100.0,
        entry_high=100.4,
        stop=98.8,
    ) == "WAITING_ENTRY"


def test_long_plan_invalidates_only_at_original_stop():
    assert classify_price_state(
        direction="LONG",
        price=99.1,
        entry_low=100.0,
        entry_high=100.4,
        stop=98.8,
    ) == "WAITING_ENTRY"
    assert classify_price_state(
        direction="LONG",
        price=98.8,
        entry_low=100.0,
        entry_high=100.4,
        stop=98.8,
    ) == "INVALIDATED"


def test_no_chase_after_price_runs_away():
    assert classify_price_state(
        direction="LONG",
        price=102.0,
        entry_low=100.0,
        entry_high=100.4,
        stop=98.8,
    ) == "NO_CHASE"
    assert classify_price_state(
        direction="SHORT",
        price=98.0,
        entry_low=99.6,
        entry_high=100.0,
        stop=101.2,
    ) == "NO_CHASE"


def test_short_waits_until_zone_or_invalidation():
    assert classify_price_state(
        direction="SHORT",
        price=100.5,
        entry_low=99.6,
        entry_high=100.0,
        stop=101.2,
    ) == "WAITING_ENTRY"
    assert classify_price_state(
        direction="SHORT",
        price=101.2,
        entry_low=99.6,
        entry_high=100.0,
        stop=101.2,
    ) == "INVALIDATED"
