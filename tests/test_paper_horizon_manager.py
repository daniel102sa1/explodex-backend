from app.services.paper_horizon_manager import planned_max_hold_minutes


def test_explicit_max_hold_always_wins():
    assert planned_max_hold_minutes({
        "strategy_mode": "SWING_PAPER",
        "max_hold_minutes": 1800,
        "planned_horizon": "24-48h",
    }) == 1800


def test_legacy_swing_without_metadata_is_not_cut_at_two_hours():
    assert planned_max_hold_minutes({"strategy_mode": "SWING_PAPER"}) == 720
    assert planned_max_hold_minutes({"strategy_mode": "SWING_TRAJECTORY_PAPER"}) == 720


def test_horizon_text_restores_day_and_two_day_holds():
    assert planned_max_hold_minutes({
        "strategy_mode": "SWING_PAPER",
        "planned_horizon": "8-24h",
    }) == 1440
    assert planned_max_hold_minutes({
        "strategy_mode": "SWING_PAPER",
        "planned_horizon": "24-48h",
    }) == 2880


def test_short_horizon_profiles_keep_shorter_timeouts():
    assert planned_max_hold_minutes({"strategy_mode": "MICRO_SCALP"}) == 35
    assert planned_max_hold_minutes({"strategy_mode": "AGGRESSIVE_PAPER"}) == 120
    assert planned_max_hold_minutes({"strategy_mode": "TACTICAL"}) == 180
