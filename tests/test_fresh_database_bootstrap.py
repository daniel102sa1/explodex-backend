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
        "paper_micro_signals",
        "paper_range_signals",
        "paper_trade_theses",
        "paper_trade_audits",
    }
    assert required <= FOUNDATIONAL_TABLES


def test_bootstrap_sql_has_no_unbound_sqlalchemy_parameters() -> None:
    import asyncio

    from app.bootstrap_schema import ensure_fresh_database_schema

    class FakeConnection:
        async def execute(self, statement):
            assert not statement._bindparams, (
                f"Unexpected SQLAlchemy bind parameters: {sorted(statement._bindparams)}"
            )

    asyncio.run(ensure_fresh_database_schema(FakeConnection()))
