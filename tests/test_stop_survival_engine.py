from app.services.paper_horizon_manager import evaluate_survival_candle
from app.services.stop_survival_engine import build_stop_survival_plan


def _heart():
    return {
        "higher_timeframe_context": {
            "frames": {
                "4h": {
                    "atr_pct": 1.4,
                    "robust_bar_range_pct": 1.8,
                }
            }
        },
        "plan": {
            "tp1": 106.0,
            "tp2": 109.0,
            "tp3": 112.0,
        },
        "elliott_structure": {
            "status": "NO_CLEAR_COUNT",
            "best": None,
        },
    }


def test_long_wick_through_soft_stop_but_recovery_does_not_exit():
    price, reason = evaluate_survival_candle(
        side="LONG",
        high=101.0,
        low=97.8,
        close=99.1,
        hard_stop=96.5,
        soft_stop=98.5,
        target=106.0,
        survival_enabled=True,
    )
    assert price is None
    assert reason is None


def test_long_close_below_soft_stop_confirms_structural_invalidation():
    price, reason = evaluate_survival_candle(
        side="LONG",
        high=100.0,
        low=97.8,
        close=98.2,
        hard_stop=96.5,
        soft_stop=98.5,
        target=106.0,
        survival_enabled=True,
    )
    assert price == 98.2
    assert reason == "STRUCTURAL_CLOSE_INVALIDATION"


def test_hard_stop_always_exits_even_if_candle_recovers():
    price, reason = evaluate_survival_candle(
        side="LONG",
        high=101.0,
        low=96.4,
        close=100.2,
        hard_stop=96.5,
        soft_stop=98.5,
        target=106.0,
        survival_enabled=True,
    )
    assert price == 96.5
    assert reason == "HARD_STOP"


def test_short_wick_through_soft_stop_but_recovery_does_not_exit():
    price, reason = evaluate_survival_candle(
        side="SHORT",
        high=102.2,
        low=99.0,
        close=100.8,
        hard_stop=103.5,
        soft_stop=101.5,
        target=94.0,
        survival_enabled=True,
    )
    assert price is None
    assert reason is None


def test_survival_plan_places_hard_stop_farther_before_entry():
    lane = {
        "lane": "TACTICAL",
        "direction": "LONG",
        "stop_loss": 98.5,
        "tp1": 106.0,
        "tp2": 109.0,
        "tp3": 112.0,
        "target_price": 109.0,
        "max_hold_minutes": 120,
    }
    plan = build_stop_survival_plan(
        heart=_heart(),
        lane_name="TACTICAL",
        lane=lane,
        entry=100.0,
    )
    assert plan["enabled"] is True
    assert plan["soft_invalidation_stop"] == 98.5
    assert plan["hard_stop"] < 98.5
    assert plan["hard_stop_fixed_before_entry"] is True
    assert plan["widen_after_entry"] is False
    assert plan["size_from_hard_stop"] is True


def test_survival_plan_refuses_extra_room_when_rr_becomes_bad():
    lane = {
        "lane": "TACTICAL",
        "direction": "LONG",
        "stop_loss": 99.0,
        "tp1": 101.0,
        "tp2": 101.5,
        "tp3": 102.0,
        "target_price": 101.5,
        "max_hold_minutes": 120,
    }
    heart = _heart()
    heart["plan"] = {"tp1": 101.0, "tp2": 101.5, "tp3": 102.0}
    plan = build_stop_survival_plan(
        heart=heart,
        lane_name="TACTICAL",
        lane=lane,
        entry=100.0,
    )
    assert plan["enabled"] is False
    assert plan["reason"] == "survival_stop_breaks_min_net_rr"
