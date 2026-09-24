from app.services.pump_state_machine import classify_pump_state


def _prediction(*, futures_delta=0.12, spot_delta=0.15, exhaustion="NONE", patterns=None, squeeze="NONE", crowding="NONE"):
    return {
        "professional_arsenal": {
            "flow": {
                "futures_cvd_proxy": {"normalized_delta": futures_delta},
                "spot_cvd_proxy": {"normalized_delta": spot_delta},
            },
            "derivatives": {
                "liquidation_squeeze": squeeze,
                "crowding": crowding,
            },
            "absorption_exhaustion": {"exhaustion": exhaustion},
            "patterns": {"patterns": patterns or []},
        }
    }


def test_spot_supported_abnormal_move_is_continuation_not_auto_short():
    result = classify_pump_state(
        score={"metrics": {
            "change_5m_pct": 1.1,
            "change_15m_pct": 2.3,
            "change_1h_pct": 4.0,
            "atr_pct": 0.9,
            "relative_volume": 3.0,
            "volume_acceleration": 1.6,
            "oi_change_pct": 0.45,
            "funding_rate": 0.0001,
            "order_book_imbalance": 0.12,
            "order_book_spread_bps": 2.0,
            "compression_ratio": 0.85,
        }},
        prediction=_prediction(),
        fundamental={},
    )
    assert result["state"] == "CONTINUATION"
    assert result["dominant_direction"] == "LONG"
    assert result["can_create_entry"] is False
    assert result["validated_out_of_sample"] is False


def test_post_pump_reversal_requires_structure_failure_plus_flow_reversal():
    result = classify_pump_state(
        score={"metrics": {
            "change_5m_pct": -0.8,
            "change_15m_pct": 4.0,
            "change_1h_pct": 8.0,
            "atr_pct": 1.0,
            "relative_volume": 3.5,
            "volume_acceleration": 1.1,
            "oi_change_pct": 1.2,
            "funding_rate": 0.0008,
            "order_book_imbalance": -0.12,
            "order_book_spread_bps": 11.0,
            "compression_ratio": 1.2,
        }},
        prediction=_prediction(
            futures_delta=-0.08,
            spot_delta=-0.10,
            exhaustion="UPSIDE_EXHAUSTION",
            patterns=[{"name": "FAILED_BREAKOUT_RANGE_DEVIATION", "bias": "SHORT"}],
            crowding="LONGS_CROWDED",
        ),
        fundamental={},
    )
    assert result["state"] == "REVERSAL_CONFIRMED"
    assert result["dominant_direction"] == "SHORT"
    assert "failed_breakout_plus_flow_reversal" in result["evidence"]


def test_compression_plus_multiple_anomalies_can_be_pre_ignition_without_entry():
    result = classify_pump_state(
        score={"metrics": {
            "change_5m_pct": 0.1,
            "change_15m_pct": 0.2,
            "change_1h_pct": 0.4,
            "atr_pct": 0.8,
            "relative_volume": 1.7,
            "volume_acceleration": 1.5,
            "oi_change_pct": 0.5,
            "funding_rate": 0.0,
            "order_book_imbalance": 0.10,
            "order_book_spread_bps": 2.0,
            "compression_ratio": 0.55,
        }},
        prediction=_prediction(futures_delta=0.01, spot_delta=0.01),
        fundamental={},
    )
    assert result["state"] == "PRE_IGNITION"
    assert result["can_create_entry"] is False
