import pytest

from app.services.practice_trading import _clean_session_id, _clean_symbol, _validate_geometry, estimated_liquidation_price


def test_practice_symbol_normalization() -> None:
    assert _clean_symbol("btc") == "BTCUSDT"
    assert _clean_symbol("solusdt") == "SOLUSDT"


def test_practice_session_is_namespaced_and_safe() -> None:
    assert _clean_session_id("practice-123_abcd") == "practice-123_abcd"
    with pytest.raises(ValueError):
        _clean_session_id("bad/session")


def test_estimated_liquidation_is_below_long_and_above_short() -> None:
    long_liq = estimated_liquidation_price(100.0, "LONG", 10)
    short_liq = estimated_liquidation_price(100.0, "SHORT", 10)
    assert 0 < long_liq < 100.0
    assert short_liq > 100.0


def test_long_and_short_geometry_validation() -> None:
    _validate_geometry(side="LONG", entry=100, stop_loss=98, take_profit=104, tp2=106, tp3=108)
    _validate_geometry(side="SHORT", entry=100, stop_loss=102, take_profit=96, tp2=94, tp3=92)
    with pytest.raises(ValueError):
        _validate_geometry(side="LONG", entry=100, stop_loss=101, take_profit=104)
    with pytest.raises(ValueError):
        _validate_geometry(side="SHORT", entry=100, stop_loss=102, take_profit=96, tp2=97)
