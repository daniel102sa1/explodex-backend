from app.services.paper_unified_heart_executor import _sarpon_leverage_policy


def _green_heart():
    return {
        "chati_sarpon_612_monitor": {
            "phase": "GREEN_CONFIRMATION",
            "contradictions": [],
            "btc": {"hard_conflict": False},
            "sarpon": {"classic": {"stage": "GREEN_CONFIRMATION"}},
        }
    }


def _policy(*, tier="HIGH", lane_name="TACTICAL", defensive=False, btc_stress="NORMAL",
            quant=1.0, council=1.0, shadow=1.0, btc_side=1.0, heart=None):
    return _sarpon_leverage_policy(
        lane_name=lane_name,
        lane={"max_leverage": 3 if lane_name == "TACTICAL" else 2},
        heart=heart or _green_heart(),
        conviction={"tier": tier, "horizon_conflict": False},
        defensive=defensive,
        btc_overlay={"stress": btc_stress},
        quant_multiplier=quant,
        council_multiplier=council,
        shadow_risk_multiplier=shadow,
        btc_side_multiplier=btc_side,
    )


def test_max_conviction_tactical_can_use_20x_as_margin_tool_only():
    result = _policy(tier="MAX_CONVICTION")
    assert result["eligible"] is True
    assert result["selected_leverage"] == 20
    assert result["tier"] == "MAX_20X"
    assert result["risk_budget_unchanged"] is True
    assert result["paper_only"] is True


def test_high_conviction_tactical_uses_10x_not_20x():
    result = _policy(tier="HIGH")
    assert result["selected_leverage"] == 10
    assert result["tier"] == "HIGH_10X"


def test_swing_full_confluence_is_capped_at_8x():
    result = _policy(tier="MAX_CONVICTION", lane_name="SWING_PAPER")
    assert result["eligible"] is True
    assert result["selected_leverage"] == 8


def test_defensive_or_btc_stress_never_boosts_leverage():
    defensive = _policy(tier="MAX_CONVICTION", defensive=True)
    assert defensive["eligible"] is False
    assert defensive["selected_leverage"] == 3

    stress = _policy(tier="MAX_CONVICTION", btc_stress="EXTREME")
    assert stress["eligible"] is False
    assert stress["selected_leverage"] == 3


def test_weak_quant_council_or_history_blocks_high_leverage():
    assert _policy(tier="MAX_CONVICTION", quant=0.70)["selected_leverage"] == 3
    assert _policy(tier="MAX_CONVICTION", council=0.70)["selected_leverage"] == 3
    assert _policy(tier="MAX_CONVICTION", shadow=0.70)["selected_leverage"] == 3
    assert _policy(tier="MAX_CONVICTION", btc_side=0.70)["selected_leverage"] == 3


def test_sarpon_contradiction_blocks_boost():
    heart = _green_heart()
    heart["chati_sarpon_612_monitor"]["contradictions"] = ["example_conflict"]
    result = _policy(tier="MAX_CONVICTION", heart=heart)
    assert result["eligible"] is False
    assert result["selected_leverage"] == 3
