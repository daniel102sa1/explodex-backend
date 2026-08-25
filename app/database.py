from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

from app.config import settings

engine = create_async_engine(settings.async_database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with SessionLocal() as session:
        yield session


async def check_database() -> bool:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def ensure_runtime_schema() -> None:
    """Keep the live Railway schema compatible with current ExplodeX engines."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_severity_check"))
        await conn.execute(
            text(
                """
                ALTER TABLE alerts
                ADD CONSTRAINT alerts_severity_check
                CHECK (severity IN (
                    'INFO', 'WARNING', 'CRITICAL',
                    'EARLY', 'ACTIVATED', 'READY',
                    'ENTRY', 'EXIT', 'STOP'
                ))
                """
            )
        )

        await conn.execute(text("ALTER TABLE trade_events DROP CONSTRAINT IF EXISTS trade_events_event_type_check"))
        await conn.execute(
            text(
                """
                ALTER TABLE trade_events
                ADD CONSTRAINT trade_events_event_type_check
                CHECK (event_type IN (
                    'OPEN', 'TP1', 'TP2', 'TP3', 'STOP',
                    'MOVE_STOP', 'CLOSE', 'TIME_EXIT'
                ))
                """
            )
        )

        # Edge Engine V2: each scanner prediction becomes a training example.
        # The row is labeled later, after enough future candles exist, avoiding
        # look-ahead leakage in the live decision itself.
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS edge_observations (
                    signal_id UUID PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
                    symbol_id UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
                    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    due_at TIMESTAMPTZ NOT NULL,
                    direction VARCHAR(8) NOT NULL,
                    prediction_type VARCHAR(40),
                    phase VARCHAR(40),
                    setup_score NUMERIC(8,3),
                    preactivation_score NUMERIC(8,3),
                    risk_score NUMERIC(8,3),
                    entry_price NUMERIC(30,12),
                    stop_loss NUMERIC(30,12),
                    tp1 NUMERIC(30,12),
                    tp2 NUMERIC(30,12),
                    tp3 NUMERIC(30,12),
                    btc_regime VARCHAR(24),
                    market_source VARCHAR(32),
                    features JSONB NOT NULL DEFAULT '{}'::jsonb,
                    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
                    label VARCHAR(24),
                    barrier_hit VARCHAR(24),
                    barrier_hit_at TIMESTAMPTZ,
                    end_price NUMERIC(30,12),
                    mfe_pct NUMERIC(12,6),
                    mae_pct NUMERIC(12,6),
                    outcome_r NUMERIC(12,6),
                    labeled_at TIMESTAMPTZ
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_edge_due ON edge_observations(status, due_at)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_edge_cohort ON edge_observations(direction, prediction_type, btc_regime, status)"))
