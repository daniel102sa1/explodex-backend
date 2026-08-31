from app.services import paper_portfolio as base
from app.services.paper_swing_trajectory import swing_candidate_ok
from app.services.trajectory_forecast import build_trajectory_forecast


def _htf(trends):
    return {
        "frames": {
            "4h": {"trend": trends[0], "atr_pct": 1.5, "swing_high": 104.0, "swing_low": 96.0},
            "6h": {"trend": trends[1]},
            "1d": {"trend": trends[2]},
        },
        "bias": "BEARISH" if trends.count("BEARISH") >= 2 else "BULLISH" if trends.count("BULLISH") >= 2 else "NEUTRAL",
    }


def test_bearish_market_can_create_swing_short_without_tactical_enter():
    score = {
        "direction": "SHORT",
        "state": "NO_TRADE",
        "setup_score": 60.0,
        "risk_score": 55.0,
        "current_price": 100.0,
        "metrics": {
            "trend_1h": "BEARISH",
            "trend_15m": "BEARISH",
            "futures_delta_ratio": -0.16,
            "spot_delta_ratio": -0.12,
            "order_book_imbalance": -0.10,
            "change_1h_pct": -1.2,
            "oi_change_pct": 0.6,
            "funding_rate": 0.0003,
            "atr_pct": 0.8,
        },
    }
    prediction = {"direction": "SHORT", "phase": "PREACTIVACION"}
    liquidity = {"attraction_direction": "DOWN"}
    result = build_trajectory_forecast(score, prediction, _htf(["BEARISH", "BEARISH", "BEARISH"]), liquidity)
    assert result["direction"] == "SHORT"
    assert result["trajectory_score"] >= 72.0
    assert result["direction_edge"] >= 12.0
    assert result["should_enter_paper_swing"] is True
    assert result["swing_plan"]["structural_stop"] > 100.0
    assert result["swing_plan"]["target1"] < 100.0
    ok, blockers = swing_candidate_ok(signal_state="NO_TRADE", trajectory=result)
    assert ok is True
    assert blockers == []


def test_mixed_higher_timeframes_do_not_force_swing_entry():
    score = {
        "direction": "LONG",
        "state": "WATCH",
        "setup_score": 68.0,
        "risk_score": 40.0,
        "current_price": 100.0,
        "metrics": {
            "trend_1h": "BULLISH",
            "trend_15m": "BULLISH",
            "futures_delta_ratio": 0.1,
            "spot_delta_ratio": 0.08,
            "order_book_imbalance": 0.08,
            "change_1h_pct": 0.7,
            "oi_change_pct": 0.4,
            "atr_pct": 1.0,
        },
    }
    result = build_trajectory_forecast(
        score,
        {"direction": "LONG", "phase": "PREACTIVACION"},
        _htf(["BULLISH", "BEARISH", "NEUTRAL"]),
        {"attraction_direction": "UP"},
    )
    assert result["aligned_htf_frames"] < 2
    assert result["should_enter_paper_swing"] is False
    assert "insufficient_htf_alignment" in result["blockers"]


def test_wider_structural_stop_reduces_quantity_instead_of_increasing_loss_budget():
    balance = 1000.0
    narrow = base.size_position(balance, 100.0, 99.0, 2)
    wide = base.size_position(balance, 100.0, 95.0, 2)
    assert wide["quantity"] < narrow["quantity"]
    # Base risk budget remains 1%; swing lane multiplies all sizing by 0.5.
    swing_risk = wide["risk_usdt"] * 0.5
    assert swing_risk == 5.0
