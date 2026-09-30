import pytest

from app.services.practice_trading import _clean_session_id, _clean_symbol


def test_practice_symbol_normalization() -> None:
    assert _clean_symbol("btc") == "BTCUSDT"
    assert _clean_symbol("solusdt") == "SOLUSDT"


def test_practice_session_is_namespaced_and_safe() -> None:
    assert _clean_session_id("practice-123_abcd") == "practice-123_abcd"
    with pytest.raises(ValueError):
        _clean_session_id("bad/session")
