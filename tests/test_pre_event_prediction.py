from app.services.pre_event_prediction import build_pre_event_prediction
from app.services.risk_conviction_engine import build_risk_conviction


def test_pre_short_squeeze_detects_precursors():
    result = build_pre_event_prediction(
        reason={
            "metrics": {
                "change_5m_pct": 0.02,
                "change_15m_pct": -0.05,
                "atr_pct": 0.8,
                "relative_volume": 1.4,
                "compression_score": 70,
                "oi_change_pct": 0.6,
                "funding_rate": -0.0005,
                "futures_delta_ratio": 0.10,
                "spot_delta_ratio": 0.12,
                "order_book_imbalance": 0.10,
            },
            "coinglass": {"oi_change_15m": 1.0, "taker_buy_sell_ratio": 1.10, "liquidation_imbalance": 0.10},
            "prediction": {"sequence": {"chase_risk": False}},
        },
        score={"symbol": "BTCUSDT"},
        event_risk={"event_type": "NORMAL", "severity": "NORMAL", "block_new_entries": False},
    )
    assert result["pre_event_type"] == "PRE_SHORT_SQUEEZE"
    assert result["direction"] == "LONG"
    assert result["paper_candidate"] is True
    assert result["supporting_signals"] >= 4


def test_weak_inputs_do_not_invent_pre_event():
    result = build_pre_event_prediction(
        reason={"metrics": {"change_5m_pct": 0.01, "change_15m_pct": 0.02, "atr_pct": 1.0, "relative_volume": 1.0}},
        score={"symbol": "ETHUSDT"},
        event_risk={"event_type": "NORMAL", "severity": "NORMAL", "block_new_entries": False},
    )
    assert result["pre_event_type"] == "NONE"
    assert result["paper_candidate"] is False


def test_active_critical_event_suppresses_pre_event():
    result = build_pre_event_prediction(
        reason={"metrics": {"change_5m_pct": 0.1, "change_15m_pct": 0.1, "atr_pct": 0.8, "relative_volume": 2.0, "compression_score": 80, "oi_change_pct": 1.0}},
        score={"symbol": "SOLUSDT"},
        event_risk={"event_type": "BLACK_SWAN_PROXY", "severity": "CRITICAL", "block_new_entries": True},
    )
    assert result["pre_event_type"] == "NONE"
    assert result["paper_candidate"] is False
    assert result["event_already_active"] is True


def test_pre_event_risk_is_capped_to_quarter_base():
    conviction = build_risk_conviction(
        lane_name="PRE_EVENT_PAPER",
        lane={
            "direction": "LONG",
            "preparation_score": 95,
            "execution_math": {"chosen_target": {"net_rr": 4.0}},
            "event_risk_multiplier": 1.0,
        },
        setup_score=90,
        risk_score=20,
        forecast_matrix={"consensus": "LONG", "horizon_conflict": False, "horizons": {h: {"direction": "LONG", "edge": 30} for h in ("15m", "1h", "4h", "6h", "24h")}},
        elliott_structure={},
    )
    assert conviction["risk_budget_multiplier"] <= 0.25
    assert conviction["tier"] == "PRE_EVENT_TINY"
