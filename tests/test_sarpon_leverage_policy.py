from app.services.paper_unified_heart_executor import _sarpon_leverage_policy


def _green_heart():
    return {
        "chati_sarpon_612_monitor": {
            "phase": "GREEN_CONFIRMATION",
            "contradictions": [],
            "btc": {"hard_conflict": False},
            "sarpon": {
                "classic": {
                    "stage": "GREEN_CONFIRMATION",
                }
            },
        }
    }


def test_full_sarpon_confluence_adds_only_one_leverage_step():
    result = _sarpon_leverage_policy(
        lane_name="TACTICAL",
        lane={"max_leverage": 3},
        heart=_green_heart(),
        conviction={"tier": "HIGH"},
        defensive=False,
        btc_overlay={"stress": "NORMAL"},
    )
    assert result["eligible"] is True
    assert result["base_cap"] == 3
    assert result["selected_leverage"] == 4
    assert result["risk_budget_unchanged"] is True


def test_swing_full_confluence_caps_at_three_x():
    result = _sarpon_leverage_policy(
        lane_name="SWING_PAPER",
        lane={"max_leverage": 2},
        heart=_green_heart(),
        conviction={"tier": "MAX_CONVICTION"},
        defensive=False,
        btc_overlay={"stress": "NORMAL"},
    )
    assert result["eligible"] is True
    assert result["selected_leverage"] == 3


def test_yellow_sarpon_or_defensive_mode_never_boosts_leverage():
    heart = _green_heart()
    heart["chati_sarpon_612_monitor"]["sarpon"]["classic"]["stage"] = "YELLOW_FORMING"
    result = _sarpon_leverage_policy(
        lane_name="TACTICAL",
        lane={"max_leverage": 3},
        heart=heart,
        conviction={"tier": "HIGH"},
        defensive=False,
        btc_overlay={"stress": "NORMAL"},
    )
    assert result["eligible"] is False
    assert result["selected_leverage"] == 3

    defensive = _sarpon_leverage_policy(
        lane_name="TACTICAL",
        lane={"max_leverage": 3},
        heart=_green_heart(),
        conviction={"tier": "HIGH"},
        defensive=True,
        btc_overlay={"stress": "NORMAL"},
    )
    assert defensive["eligible"] is False
    assert defensive["selected_leverage"] == 3


def test_extreme_btc_stress_disables_boost():
    result = _sarpon_leverage_policy(
        lane_name="TACTICAL",
        lane={"max_leverage": 3},
        heart=_green_heart(),
        conviction={"tier": "HIGH"},
        defensive=False,
        btc_overlay={"stress": "EXTREME"},
    )
    assert result["eligible"] is False
    assert result["selected_leverage"] == 3
