from __future__ import annotations

"""Idempotent bootstrap for a brand-new ExplodeX PostgreSQL database.

The production database is intentionally disposable for PAPER/demo use.  This
module creates only the foundational relations that many feature modules assume
already exist.  Feature-specific modules keep ownership of their own extra
tables and migrations.

The bootstrap is safe to run against an existing database because every object
is created with IF NOT EXISTS and no user/trading data is deleted.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


FOUNDATIONAL_TABLES = {
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


async def ensure_fresh_database_schema(conn: AsyncConnection) -> None:
    """Create the minimum complete ExplodeX schema on an empty PostgreSQL DB."""

    # gen_random_uuid() is used by learning/memory tables created later.
    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS symbols (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            symbol VARCHAR(32) UNIQUE NOT NULL,
            base_asset VARCHAR(32) NOT NULL,
            quote_asset VARCHAR(16) NOT NULL DEFAULT 'USDT',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS scanner_runs (
            id UUID PRIMARY KEY,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            symbols_scanned INTEGER NOT NULL DEFAULT 0,
            candidates_found INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(24) NOT NULL DEFAULT 'running',
            error_message TEXT
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id BIGSERIAL PRIMARY KEY,
            symbol_id UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            price NUMERIC(30,12),
            change_24h_pct NUMERIC(14,6),
            volume_24h_usdt NUMERIC(30,6),
            open_interest NUMERIC(30,6),
            open_interest_change_pct NUMERIC(14,6),
            taker_buy_sell_ratio NUMERIC(14,8),
            funding_rate NUMERIC(16,10),
            long_short_ratio NUMERIC(14,8),
            volume_5m NUMERIC(30,6),
            relative_volume NUMERIC(14,8),
            atr_pct NUMERIC(14,8),
            btc_trend VARCHAR(24),
            raw_data JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS signals (
            id UUID PRIMARY KEY,
            symbol_id UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
            scanner_run_id UUID REFERENCES scanner_runs(id) ON DELETE SET NULL,
            direction VARCHAR(8) NOT NULL,
            state VARCHAR(24) NOT NULL,
            setup_type VARCHAR(64),
            timeframe VARCHAR(16),
            setup_score NUMERIC(10,4),
            risk_score NUMERIC(10,4),
            confidence_pct NUMERIC(10,4),
            current_price NUMERIC(30,12),
            entry_low NUMERIC(30,12),
            entry_high NUMERIC(30,12),
            invalidation_price NUMERIC(30,12),
            stop_loss NUMERIC(30,12),
            tp1 NUMERIC(30,12),
            tp2 NUMERIC(30,12),
            tp3 NUMERIC(30,12),
            expected_move_min_pct NUMERIC(14,6),
            expected_move_max_pct NUMERIC(14,6),
            expected_duration_min_minutes INTEGER,
            expected_duration_max_minutes INTEGER,
            reason JSONB NOT NULL DEFAULT '{}'::jsonb,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS signal_metrics (
            signal_id UUID PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
            structure_score NUMERIC(10,4),
            oi_score NUMERIC(10,4),
            taker_score NUMERIC(10,4),
            volume_score NUMERIC(10,4),
            funding_score NUMERIC(10,4),
            btc_score NUMERIC(10,4),
            absorption_score NUMERIC(10,4),
            volatility_score NUMERIC(10,4),
            liquidity_score NUMERIC(10,4),
            oi_change_pct NUMERIC(14,6),
            taker_ratio NUMERIC(14,8),
            funding_rate NUMERIC(16,10),
            relative_volume NUMERIC(14,8),
            absorption_detected BOOLEAN NOT NULL DEFAULT FALSE,
            breakout_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
            btc_filter_passed BOOLEAN NOT NULL DEFAULT TRUE,
            notes JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key VARCHAR(80) PRIMARY KEY,
            value JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    await conn.execute(text("""
        INSERT INTO system_settings (key, value)
        VALUES
            ('scanner', '{"min_setup_score":80,"max_risk_score":35,"max_open_trades":2,"max_daily_loss_pct":3,"risk_per_trade_pct":0.5}'::jsonb),
            ('paper_account', '{"starting_equity_usdt":1000,"max_leverage":3,"estimated_fee_rate":0.0005,"target_policy":"TP2_FULL"}'::jsonb)
        ON CONFLICT (key) DO NOTHING
    """))

    # Legacy PAPER ledger remains readable for dashboards/backward compatibility.
    # The canonical visible simulator uses paper_positions below.
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS trades (
            id UUID PRIMARY KEY,
            signal_id UUID REFERENCES signals(id) ON DELETE SET NULL,
            symbol_id UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
            mode VARCHAR(16) NOT NULL DEFAULT 'PAPER',
            direction VARCHAR(8) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
            leverage NUMERIC(12,4) NOT NULL DEFAULT 1,
            risk_pct NUMERIC(12,6),
            entry_price NUMERIC(30,12) NOT NULL,
            quantity NUMERIC(30,12) NOT NULL,
            notional_usdt NUMERIC(30,8) NOT NULL,
            stop_loss NUMERIC(30,12),
            tp1 NUMERIC(30,12),
            tp2 NUMERIC(30,12),
            tp3 NUMERIC(30,12),
            opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            closed_at TIMESTAMPTZ,
            exit_price NUMERIC(30,12),
            pnl_usdt NUMERIC(24,8),
            pnl_pct NUMERIC(16,8),
            r_multiple NUMERIC(16,8),
            fees_usdt NUMERIC(24,8) NOT NULL DEFAULT 0,
            close_reason VARCHAR(64),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS trade_events (
            id BIGSERIAL PRIMARY KEY,
            trade_id UUID NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
            event_type VARCHAR(24) NOT NULL,
            price NUMERIC(30,12),
            setup_score NUMERIC(10,4),
            risk_score NUMERIC(10,4),
            message TEXT,
            data JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS alerts (
            id BIGSERIAL PRIMARY KEY,
            signal_id UUID REFERENCES signals(id) ON DELETE CASCADE,
            trade_id UUID REFERENCES trades(id) ON DELETE SET NULL,
            channel VARCHAR(24) NOT NULL DEFAULT 'APP',
            severity VARCHAR(24) NOT NULL DEFAULT 'INFO',
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            is_sent BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS validation_observations (
            signal_id UUID PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
            symbol_id UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
            symbol VARCHAR(32) NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            direction VARCHAR(8) NOT NULL,
            entry_price NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12),
            tp1 NUMERIC(30,12),
            trade_class VARCHAR(24),
            grade VARCHAR(8),
            master_state VARCHAR(16),
            fingerprint_score NUMERIC(10,4),
            locks_passed INTEGER,
            catalyst_state VARCHAR(24),
            path_bias VARCHAR(12),
            data_quality VARCHAR(24),
            atr_pct NUMERIC(12,6),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS validation_horizon_results (
            signal_id UUID NOT NULL REFERENCES validation_observations(signal_id) ON DELETE CASCADE,
            horizon_minutes INTEGER NOT NULL,
            evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            end_price NUMERIC(30,12),
            mfe_pct NUMERIC(14,6),
            mae_pct NUMERIC(14,6),
            directional_return_pct NUMERIC(14,6),
            mfe_atr NUMERIC(14,6),
            mae_atr NUMERIC(14,6),
            barrier_hit VARCHAR(16),
            barrier_hit_at TIMESTAMPTZ,
            PRIMARY KEY (signal_id, horizon_minutes)
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_accounts (
            id INTEGER PRIMARY KEY DEFAULT 1,
            starting_balance NUMERIC(18,6) NOT NULL DEFAULT 1000,
            cash_balance NUMERIC(18,6) NOT NULL DEFAULT 1000,
            realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
            total_fees NUMERIC(18,6) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (id = 1)
        )
    """))
    await conn.execute(text("""
        INSERT INTO paper_accounts (id, starting_balance, cash_balance)
        VALUES (1, 1000, 1000)
        ON CONFLICT (id) DO NOTHING
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_positions (
            id BIGSERIAL PRIMARY KEY,
            signal_id UUID UNIQUE,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            status VARCHAR(12) NOT NULL DEFAULT 'OPEN',
            grade VARCHAR(8),
            fingerprint_score NUMERIC(10,4),
            leverage INTEGER NOT NULL,
            entry_price NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            take_profit NUMERIC(30,12) NOT NULL,
            quantity NUMERIC(30,12) NOT NULL,
            notional NUMERIC(24,8) NOT NULL,
            margin_used NUMERIC(24,8) NOT NULL,
            risk_usdt NUMERIC(24,8) NOT NULL,
            opened_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ,
            exit_price NUMERIC(30,12),
            exit_reason VARCHAR(64),
            gross_pnl NUMERIC(24,8),
            net_pnl NUMERIC(24,8),
            fees NUMERIC(24,8) DEFAULT 0,
            slippage NUMERIC(24,8) DEFAULT 0,
            funding_estimate NUMERIC(24,8) DEFAULT 0,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT paper_positions_signal_id_signals_fkey
                FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE SET NULL
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_equity_curve (
            id BIGSERIAL PRIMARY KEY,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            cash_balance NUMERIC(18,6) NOT NULL,
            unrealized_pnl NUMERIC(18,6) NOT NULL,
            equity NUMERIC(18,6) NOT NULL,
            open_positions INTEGER NOT NULL
        )
    """))

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_orders (
            id BIGSERIAL PRIMARY KEY,
            signal_id UUID REFERENCES signals(id) ON DELETE SET NULL,
            position_id BIGINT REFERENCES paper_positions(id) ON DELETE SET NULL,
            symbol VARCHAR(32) NOT NULL,
            position_side VARCHAR(8) NOT NULL,
            action VARCHAR(8) NOT NULL,
            order_role VARCHAR(20) NOT NULL,
            order_type VARCHAR(32) NOT NULL,
            status VARCHAR(16) NOT NULL,
            requested_price NUMERIC(30,12),
            trigger_price NUMERIC(30,12),
            fill_price NUMERIC(30,12),
            quantity NUMERIC(30,12) NOT NULL,
            leverage INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            filled_at TIMESTAMPTZ,
            canceled_at TIMESTAMPTZ,
            cancel_reason VARCHAR(64),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (position_id, order_role)
        )
    """))

    # Small, high-value indexes only. Avoid indexing large JSON payloads so the
    # demo database remains cheap and compact.
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time ON market_snapshots(symbol_id, captured_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_signals_active_time ON signals(is_active, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_signals_symbol_time ON signals(symbol_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_trades_status_time ON trades(mode, status, opened_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_trade_events_time ON trade_events(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_validation_obs_time ON validation_observations(observed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_validation_class ON validation_observations(trade_class, observed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_validation_horizon ON validation_horizon_results(horizon_minutes, evaluated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status, opened_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_symbol ON paper_orders(symbol, created_at DESC)",
    ]
    for ddl in indexes:
        await conn.execute(text(ddl))
