from app.services.paper_reset import _is_resettable_table
from app.services.paper_portfolio import RISK_PER_TRADE


def test_clean_reset_targets_only_history_state_tables():
    assert _is_resettable_table("paper_positions") is True
    assert _is_resettable_table("validation_observations") is True
    assert _is_resettable_table("edge_observations") is True
    assert _is_resettable_table("verdict_memory") is True
    assert _is_resettable_table("heart_shadow_forecasts") is True
    assert _is_resettable_table("trade_theses") is True
    assert _is_resettable_table("macro_cycle_snapshots") is True
    assert _is_resettable_table("scanner_runs") is True


def test_clean_reset_preserves_reference_and_marker_tables():
    assert _is_resettable_table("symbols") is False
    assert _is_resettable_table("system_reset_markers") is False
    assert _is_resettable_table("market_snapshots") is False


def test_new_paper_baseline_uses_three_percent_target_risk():
    assert RISK_PER_TRADE == 0.03
