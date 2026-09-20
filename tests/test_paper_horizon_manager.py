from app.services.paper_horizon_manager import (
    breakeven_profit_lock_stop,
    planned_max_hold_minutes,
    pre_tp1_protection_signal,
    pre_tp1_protective_stop,
    tp2_profit_lock_stop,
)


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



def test_tp1_profit_lock_moves_long_to_breakeven_plus_cost_buffer():
    stop = breakeven_profit_lock_stop(
        side="LONG",
        entry=100.0,
        tp1=102.0,
        current_stop=98.0,
    )
    assert 100.0 <= stop < 102.0
    assert round(stop, 2) == 100.18


def test_tp1_profit_lock_never_widens_an_already_tighter_stop():
    stop = breakeven_profit_lock_stop(
        side="LONG",
        entry=100.0,
        tp1=102.0,
        current_stop=100.60,
    )
    assert stop == 100.60


def test_tp1_profit_lock_works_for_short():
    stop = breakeven_profit_lock_stop(
        side="SHORT",
        entry=100.0,
        tp1=98.0,
        current_stop=102.0,
    )
    assert 98.0 < stop <= 100.0
    assert round(stop, 2) == 99.82


def test_after_tp2_stop_moves_to_tp1_only_in_profit_direction():
    assert tp2_profit_lock_stop(side="LONG", tp1=102.0, current_stop=100.18) == 102.0
    assert tp2_profit_lock_stop(side="SHORT", tp1=98.0, current_stop=99.82) == 98.0



def test_near_tp1_without_rejection_does_not_tighten():
    signal = pre_tp1_protection_signal(
        side="LONG",
        entry=100.0,
        tp1=104.0,
        high=103.6,
        low=102.8,
        close=103.4,
    )
    assert signal["max_progress"] >= 0.85
    assert signal["triggered"] is False


def test_near_tp1_rejection_triggers_long_protection():
    signal = pre_tp1_protection_signal(
        side="LONG",
        entry=100.0,
        tp1=104.0,
        high=103.7,
        low=101.8,
        close=102.4,
    )
    assert signal["triggered"] is True

    stop = pre_tp1_protective_stop(
        side="LONG",
        entry=100.0,
        current_stop=98.0,
        close=102.4,
    )
    assert round(stop, 2) == 100.18


def test_near_tp1_rejection_triggers_short_protection():
    signal = pre_tp1_protection_signal(
        side="SHORT",
        entry=100.0,
        tp1=96.0,
        high=98.2,
        low=96.3,
        close=97.6,
    )
    assert signal["triggered"] is True

    stop = pre_tp1_protective_stop(
        side="SHORT",
        entry=100.0,
        current_stop=102.0,
        close=97.6,
    )
    assert round(stop, 2) == 99.82


def test_pre_tp1_protection_never_widens_stop():
    assert pre_tp1_protective_stop(
        side="LONG",
        entry=100.0,
        current_stop=100.60,
        close=102.0,
    ) == 100.60
