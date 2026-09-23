from app.services.paper_reset import (
    MARKER_PREFIX,
    _is_open_position_reset_target,
    _reset_marker_key,
)
from app.services.paper_portfolio import RISK_PER_TRADE


def test_open_reset_targets_only_canonical_open_position_ledger():
    assert _is_open_position_reset_target("paper_positions") is True

    # Regression guard: these were incorrectly wiped by the old broad reset.
    for protected in (
        "signals",
        "alerts",
        "trades",
        "trade_events",
        "scanner_runs",
        "heart_shadow_forecasts",
        "trade_theses",
        "validation_observations",
        "edge_observations",
        "verdict_memory",
        "formula_observations",
        "macro_cycle_snapshots",
        "paper_accounts",
        "paper_equity_curve",
    ):
        assert _is_open_position_reset_target(protected) is False


def test_open_reset_marker_is_namespaced_from_old_baseline_reset():
    assert MARKER_PREFIX == "OPEN_PAPER_ONLY"
    assert _reset_marker_key("abc") == "OPEN_PAPER_ONLY::abc"


def test_current_three_percent_target_risk_is_unchanged():
    # The correction changes reset scope only; the current PAPER risk experiment stays intact.
    assert RISK_PER_TRADE == 0.03
