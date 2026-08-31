from app.services.horizon_forecast_matrix import build_horizon_forecast_matrix


def _base_score():
    return {
        "direction": "LONG",
        "current_price": 100.0,
        "metrics": {
            "futures_delta_ratio": 0.18,
            "spot_delta_ratio": 0.14,
            "order_book_imbalance": 0.12,
            "change_5m_pct": 0.8,
            "change_15m_pct": 1.2,
            "change_1h_pct": 2.0,
            "oi_change_pct": 0.9,
        },
    }


def _heart(long_term="BULLISH"):
    return {
        "direction": "LONG",
        "ignition": {"score": 88},
        "trajectory_forecast": {
            "long_trajectory_score": 82,
            "short_trajectory_score": 28,
        },
        "higher_timeframe_context": {
            "frames": {
                "4h": {"trend": long_term, "trend_strength_signed": 0.7 if long_term == "BULLISH" else -0.7},
                "6h": {"trend": long_term, "trend_strength_signed": 0.65 if long_term == "BULLISH" else -0.65},
                "1d": {"trend": long_term, "trend_strength_signed": 0.6 if long_term == "BULLISH" else -0.6},
            }
        },
        "liquidity_intelligence": {"attraction_direction": "UP" if long_term == "BULLISH" else "DOWN"},
    }


def test_matrix_reports_long_consensus_when_micro_and_htf_align():
    matrix = build_horizon_forecast_matrix(score=_base_score(), prediction={"direction": "LONG"}, heart=_heart())
    assert matrix["consensus"] == "LONG"
    assert matrix["horizon_conflict"] is False
    assert matrix["horizons"]["15m"]["direction"] == "LONG"
    assert matrix["horizons"]["24h"]["direction"] == "LONG"


def test_matrix_detects_short_term_long_vs_long_term_short_conflict():
    heart = _heart("BEARISH")
    heart["trajectory_forecast"] = {"long_trajectory_score": 30, "short_trajectory_score": 80}
    matrix = build_horizon_forecast_matrix(score=_base_score(), prediction={"direction": "LONG"}, heart=heart)
    assert matrix["horizons"]["15m"]["direction"] == "LONG"
    assert matrix["horizons"]["24h"]["direction"] == "SHORT"
    assert matrix["horizon_conflict"] is True


def test_matrix_is_context_only_not_execution_authority():
    matrix = build_horizon_forecast_matrix(score=_base_score(), prediction={"direction": "LONG"}, heart=_heart())
    assert "should_enter" not in matrix
    assert "permitted_paper_lane" not in matrix
    assert "never authorizes" in matrix["use"].lower()
