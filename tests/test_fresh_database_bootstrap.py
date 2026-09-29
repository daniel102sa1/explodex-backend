from app.bootstrap_schema import FOUNDATIONAL_TABLES


def test_fresh_database_bootstrap_has_explodex_foundations() -> None:
    required = {
        "symbols",
        "scanner_runs",
        "market_snapshots",
        "signals",
        "signal_metrics",
        "system_settings",
        "trades",
        "trade_events",
        "alerts",
        "validation_observations",
        "validation_horizon_results",
        "paper_accounts",
        "paper_positions",
        "paper_equity_curve",
        "paper_orders",
    }
    assert required <= FOUNDATIONAL_TABLES
