from app.services.fundamental_intelligence import _risk_layer


def test_high_dilution_low_float_microcap_is_high_risk_context():
    risk = _risk_layer({
        "market_cap": 30_000_000,
        "fully_diluted_valuation": 150_000_000,
        "total_volume": 150_000,
        "circulating_supply": 100_000_000,
        "total_supply": 500_000_000,
        "max_supply": 1_000_000_000,
    })
    assert risk["risk_score"] >= 60
    assert risk["state"] == "HIGH_TOKENOMICS_LIQUIDITY_RISK"
    assert risk["risk_multiplier_cap"] <= 0.65
    assert "very_high_fdv_to_market_cap" in risk["flags"]
    assert "low_circulating_to_total_supply" in risk["flags"]


def test_large_liquid_well_floated_asset_does_not_get_artificial_penalty():
    risk = _risk_layer({
        "market_cap": 10_000_000_000,
        "fully_diluted_valuation": 11_000_000_000,
        "total_volume": 900_000_000,
        "circulating_supply": 900_000_000,
        "total_supply": 1_000_000_000,
        "max_supply": 1_000_000_000,
    })
    assert risk["risk_score"] < 15
    assert risk["risk_multiplier_cap"] == 1.0
