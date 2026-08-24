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
    """Keep small enum-like CHECK constraints compatible with the live engine.

    The original Railway database predates PREACTIVACION/ACTIVADO alerts and
    paper time exits.  New application code must not be able to abort an entire
    scanner cycle just because those newer labels are absent from an old CHECK.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                ALTER TABLE alerts
                DROP CONSTRAINT IF EXISTS alerts_severity_check
                """
            )
        )
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

        await conn.execute(
            text(
                """
                ALTER TABLE trade_events
                DROP CONSTRAINT IF EXISTS trade_events_event_type_check
                """
            )
        )
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
