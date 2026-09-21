from app.services.prediction_safety import apply_prediction_safety


def _scored():
    return {
        "direction": "LONG",
        "long_score": 82.0,
        "short_score": 58.0,
        "current_price": 100.0,
        "metrics": {
            "atr_pct": 0.5,
            "ema9": 100.4,
            "ema21": 100.0,
            "trend_15m": "BULLISH",
            "trend_1h": "BULLISH",
        },
    }


def _prediction(stop=98.6, tp1=102.0, tp2=103.0, tp3=106.0):
    return {
        "direction": "LONG",
        "phase": "ACTIVADO",
        "trigger_price": 100.0,
        "entry_low": 99.95,
        "entry_high": 100.05,
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "sequence": {"chase_risk": False},
        "confirmations": [],
        "conflicts": [],
    }


def test_far_tp_and_wide_but_non_extreme_structural_stop_are_soft_warnings():
    result = apply_prediction_safety(_scored(), _prediction())

    assert result["sequence"]["risk_guard_pass"] is True
    assert result["sequence"]["risk_guard_blocks"] == []
    assert "tp1_far_requires_longer_horizon" in result["sequence"]["risk_guard_warnings"]
    assert "stop_wide_structural" in result["sequence"]["risk_guard_warnings"]
    assert result["phase"] == "ACTIVADO"


def test_extreme_stop_geometry_remains_a_hard_block():
    result = apply_prediction_safety(
        _scored(),
        _prediction(stop=95.0, tp1=108.0, tp2=112.0, tp3=118.0),
    )

    assert result["sequence"]["risk_guard_pass"] is False
    assert "stop_extreme" in result["sequence"]["risk_guard_blocks"]
    assert result["phase"] == "VIGILAR_CONFLICTOS"
