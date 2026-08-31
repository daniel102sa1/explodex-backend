from app.services.explosion_classifier_v2 import classify_horizons
from app.services.explosion_intelligence import timing_memory_adjustment
from app.services.higher_timeframe_context import alignment
from app.services.liquidity_target_engine import build_liquidity_targets


def _h(fav: float, adv: float, ret: float = 0.1):
    return {"favorable_r": fav, "adverse_r": adv, "directional_return_pct": ret}


def test_fast_long_explosion_is_labeled_separately_from_delayed_move():
    result = classify_horizons("LONG", {
        "1m": _h(0.2, 0.1), "3m": _h(0.8, 0.2),
        "5m": _h(2.2, 0.4, 1.1), "4h": _h(3.0, 0.7, 2.0),
    })
    assert result is not None
    assert result["label"] == "EXPLOSION_LONG"
    assert result["timing_quality"] == "GOOD"
    assert result["explosion_horizon"] == "5m"


def test_move_that_only_explodes_hours_later_is_too_early_not_good_timing():
    result = classify_horizons("SHORT", {
        "1m": _h(0.1, 0.2, -0.05), "5m": _h(0.3, 0.5, 0.02),
        "30m": _h(0.5, 0.7, 0.01), "1h": _h(0.6, 0.8, -0.02),
        "4h": _h(1.1, 0.9, 0.4), "6h": _h(2.4, 0.9, 1.4),
    })
    assert result is not None
    assert result["label"] == "DELAYED_EXPLOSION"
    assert result["timing_quality"] == "TOO_EARLY"
    assert result["explosion_horizon"] == "6h"


def test_sweep_reversal_requires_recovery_inside_short_timing_window():
    result = classify_horizons("LONG", {
        "1m": _h(0.1, 0.9, -0.4), "5m": _h(0.3, 1.0, -0.2),
        "15m": _h(1.2, 1.0, 0.3), "1h": _h(1.7, 1.0, 0.8),
        "4h": _h(2.2, 1.0, 1.5),
    })
    assert result is not None
    assert result["label"] == "SWEEP_AND_REVERSE_TO_THESIS"


def test_memory_cannot_change_entry_before_thirty_cases():
    ignition = {"score": 81.0}
    model = {"buckets": {"76-81": {"sample": 29, "fast_explosion_rate_pct": 90.0, "fake_breakout_rate_pct": 0.0, "direction_wrong_rate_pct": 0.0, "avg_short_favorable_r": 3.0, "avg_short_adverse_r": 0.2}}}
    result = timing_memory_adjustment(ignition, model)
    assert result["can_influence_entry"] is False
    assert result["adjustment"] == 0.0
    assert result["adjusted_score"] == 81.0


def test_usable_memory_can_nudge_timing_but_only_by_small_amount():
    ignition = {"score": 81.0}
    model = {"buckets": {"76-81": {"sample": 40, "fast_explosion_rate_pct": 62.0, "fake_breakout_rate_pct": 8.0, "direction_wrong_rate_pct": 10.0, "avg_short_favorable_r": 2.1, "avg_short_adverse_r": 0.7}}}
    result = timing_memory_adjustment(ignition, model)
    assert result["can_influence_entry"] is True
    assert result["adjustment"] == 3.0
    assert result["adjusted_score"] == 84.0


def test_bad_historical_bucket_penalizes_timing_instead_of_forcing_trade():
    ignition = {"score": 84.0}
    model = {"buckets": {"82-87": {"sample": 55, "fast_explosion_rate_pct": 20.0, "fake_breakout_rate_pct": 31.0, "direction_wrong_rate_pct": 28.0, "avg_short_favorable_r": 0.6, "avg_short_adverse_r": 1.2}}}
    result = timing_memory_adjustment(ignition, model)
    assert result["can_influence_entry"] is True
    assert result["adjustment"] == -5.0
    assert result["adjusted_score"] == 79.0


def test_liquidity_intelligence_is_advisory_and_keeps_frozen_targets():
    score = {
        "direction": "LONG", "current_price": 100.0, "stop_loss": 98.0,
        "tp1": 103.0, "tp2": 105.0, "tp3": 108.0,
        "metrics": {"distance_to_high_pct": 1.0, "distance_to_low_pct": 1.5, "futures_delta_ratio": 0.15, "spot_delta_ratio": 0.10, "order_book_imbalance": 0.12, "futures_buy_sell_ratio": 1.2},
    }
    prediction = {"direction": "LONG", "stop_loss": 98.0, "tp1": 103.0, "tp2": 105.0, "tp3": 108.0}
    thesis = {"frozen_plan": True, "direction": "LONG", "stop_loss": 98.0, "tp1": 103.0, "tp2": 105.0, "tp3": 108.0}
    result = build_liquidity_targets(score, prediction, thesis)
    assert result["attraction_direction"] == "UP"
    assert result["aligned_with_thesis"] is True
    prices = {item["name"]: item["price"] for item in result["candidates"]}
    assert prices["TP1"] == 103.0
    assert prices["TP3"] == 108.0
    assert result["target_is_forecast_not_guarantee"] is True


def test_higher_timeframe_alignment_never_claims_entry_itself():
    context = {"bias": "BULLISH", "frames": {"4h": {"trend": "BULLISH"}, "6h": {"trend": "BULLISH"}, "1d": {"trend": "BEARISH"}}}
    result = alignment("LONG", context)
    assert result["strong_alignment"] is True
    assert result["strong_conflict"] is False
    assert result["aligned_frames"] == 2
