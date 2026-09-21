from app.services.coinglass_confirmation import apply_coinglass_confirmation


def test_missing_optional_coinglass_does_not_erase_local_ready():
    score = {
        "direction": "LONG",
        "state": "READY",
        "setup_score": 90,
        "risk_score": 20,
        "metrics": {"change_15m_pct": 0.4, "confirmations": 5},
        "components": {},
    }
    result = apply_coinglass_confirmation(score, {"available": False, "critical_complete": False})

    assert result["state"] == "READY"
    assert result["coinglass"]["available"] is False
