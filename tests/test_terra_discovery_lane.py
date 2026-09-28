from app.services.paper_unified_heart_executor import (
    DEFENSIVE_RISK_CAP,
    PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP,
    TERRA_DISCOVERY_MAX_RISK_SCORE,
    TERRA_DISCOVERY_MIN_SETUP_SCORE,
    _build_terra_discovery_lane,
    _terra_portfolio_risk_multiplier,
)


def _long_row(**overrides):
    row = {
        "direction": "LONG",
        "state": "PREPARING",
        "setup_score": TERRA_DISCOVERY_MIN_SETUP_SCORE + 5,
        "risk_score": TERRA_DISCOVERY_MAX_RISK_SCORE - 10,
        "entry_low": 100.0,
        "entry_high": 101.0,
        "stop_loss": 98.0,
        "tp1": 103.0,
        "tp2": 105.0,
        "tp3": 108.0,
    }
    row.update(overrides)
    return row


def test_clean_preparing_signal_is_reachable_by_terra_discovery():
    lane = _build_terra_discovery_lane(_long_row())
    assert lane is not None
    assert lane["terra_discovery"] is True
    assert lane["trade_profile"] == "TERRA_DISCOVERY_PAPER"
    assert lane["max_leverage"] == 1


def test_discovery_does_not_promote_hard_no_trade_or_high_risk_signal():
    assert _build_terra_discovery_lane(_long_row(state="NO_TRADE")) is None
    assert _build_terra_discovery_lane(
        _long_row(risk_score=TERRA_DISCOVERY_MAX_RISK_SCORE + 1)
    ) is None


def test_discovery_requires_valid_stop_target_geometry():
    assert _build_terra_discovery_lane(_long_row(stop_loss=102.0)) is None


def test_terra_obeys_defensive_and_probation_portfolio_brakes():
    assert _terra_portfolio_risk_multiplier(
        1.0, defensive=True, validation_probation=False
    ) == DEFENSIVE_RISK_CAP
    assert _terra_portfolio_risk_multiplier(
        1.0, defensive=False, validation_probation=True
    ) == PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP
    assert _terra_portfolio_risk_multiplier(
        0.0, defensive=False, validation_probation=False
    ) == 0.0
