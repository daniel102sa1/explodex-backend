from app.services.paper_orders import exit_role_for_reason


def test_exit_role_maps_stop_reasons():
    assert exit_role_for_reason("STOP") == "STOP"
    assert exit_role_for_reason("AMBIGUOUS_STOP") == "STOP"


def test_exit_role_maps_take_profit():
    assert exit_role_for_reason("TP1") == "TP1"


def test_exit_role_maps_time_exit_to_market():
    assert exit_role_for_reason("TIME_EXIT") == "EXIT_MARKET"
    assert exit_role_for_reason(None) == "EXIT_MARKET"
