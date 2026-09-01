from app.services.event_risk_engine import build_event_risk
from app.services.risk_conviction_engine import build_risk_conviction


def test_normal_market_stays_normal():
    event = build_event_risk(
        reason={"metrics": {"change_5m_pct": 0.1, "change_15m_pct": 0.2, "change_1h_pct": 0.3, "atr_pct": 0.8, "relative_volume": 1.0}},
        score={"symbol": "BTCUSDT"},
    )
    assert event["event_type"] == "NORMAL"
    assert event["block_new_entries"] is False
    assert event["risk_multiplier"] == 1.0


def test_long_squeeze_detects_downside_pressure():
    event = build_event_risk(
        reason={
            "metrics": {
                "change_5m_pct": -2.2,
                "change_15m_pct": -3.0,
                "atr_pct": 0.9,
                "relative_volume": 3.1,
                "volume_acceleration": 2.3,
                "oi_change_pct": -1.2,
                "futures_delta_ratio": -0.25,
            },
            "coinglass": {"long_liquidations_1h": 10_000_000, "short_liquidations_1h": 1_000_000, "liquidation_imbalance": -0.7},
        },
        score={"symbol": "ETHUSDT"},
    )
    assert event["scores"]["LONG_SQUEEZE"] >= 60
    assert event["directional_bias"] in {"SHORT", "NEUTRAL"}
    assert event["event_type"] in {"LONG_SQUEEZE", "LIQUIDATION_CASCADE", "BLACK_SWAN_PROXY", "STRESS"}


def test_short_squeeze_detects_upside_pressure():
    event = build_event_risk(
        reason={
            "metrics": {
                "change_5m_pct": 2.0,
                "change_15m_pct": 2.8,
                "atr_pct": 0.9,
                "relative_volume": 3.0,
                "volume_acceleration": 2.0,
                "oi_change_pct": -1.0,
                "futures_delta_ratio": 0.24,
            },
            "coinglass": {"short_liquidations_1h": 9_000_000, "long_liquidations_1h": 1_000_000, "liquidation_imbalance": 0.65},
        },
        score={"symbol": "SOLUSDT"},
    )
    assert event["scores"]["SHORT_SQUEEZE"] >= 60
    assert event["event_type"] in {"SHORT_SQUEEZE", "LIQUIDATION_CASCADE", "BLACK_SWAN_PROXY", "STRESS"}


def test_depeg_risk_can_block_new_entries():
    event = build_event_risk(
        reason={"stablecoin_distance_pct": 1.2, "metrics": {"change_15m_pct": -1.1, "change_1h_pct": -1.4}},
        score={"symbol": "USDCUSDT"},
    )
    assert event["scores"]["DEPEG_RISK"] >= 60
    assert event["block_new_entries"] is True
    assert event["risk_multiplier"] <= 0.2


def test_true_black_swan_is_not_claimed_as_predicted():
    event = build_event_risk(
        reason={"metrics": {"change_15m_pct": -7.0, "change_1h_pct": -9.0, "atr_pct": 1.0, "relative_volume": 6.0, "volume_acceleration": 3.0}},
        score={"symbol": "BTCUSDT"},
    )
    assert event["black_swan_is_proxy"] is True
    assert event["predicts_true_black_swan"] is False
    assert event["scores"]["BLACK_SWAN_PROXY"] > 0


def test_event_multiplier_reduces_conviction_position_risk():
    lane = {
        "direction": "LONG",
        "ignition_score": 90,
        "execution_math": {"chosen_target": {"net_rr": 3.8}},
        "event_risk_multiplier": 0.4,
        "event_type": "LIQUIDATION_CASCADE",
        "event_severity": "HIGH",
        "event_directional_bias": "LONG",
    }
    matrix = {
        "consensus": "LONG",
        "horizon_conflict": False,
        "horizons": {h: {"direction": "LONG", "edge": 25} for h in ("15m", "1h", "4h", "6h", "24h")},
    }
    result = build_risk_conviction(
        lane_name="TACTICAL",
        lane=lane,
        setup_score=90,
        risk_score=20,
        forecast_matrix=matrix,
        elliott_structure={},
    )
    assert result["risk_budget_multiplier"] <= 0.6
    assert result["event_risk"]["event_type"] == "LIQUIDATION_CASCADE"


def test_event_engine_never_creates_entry_or_changes_direction():
    event = build_event_risk(
        reason={"metrics": {"change_5m_pct": -3.0, "atr_pct": 0.8, "relative_volume": 4.0}},
        score={"symbol": "XRPUSDT"},
    )
    assert event["creates_entry"] is False
    assert event["changes_direction"] is False
