from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def reconcile_canonical_theses(db: AsyncSession) -> dict[str, int]:
    """Close canonical IN_POSITION theses once no PAPER position remains open."""
    result = await db.execute(text("""
        UPDATE trade_theses tt
        SET status='CLOSED', closed_at=COALESCE(closed_at, NOW()), updated_at=NOW(),
            cooldown_until=COALESCE(cooldown_until, NOW() + INTERVAL '15 minutes'),
            metadata=metadata || jsonb_build_object('paper_reconciled', TRUE)
        WHERE tt.status='IN_POSITION'
          AND NOT EXISTS (
              SELECT 1 FROM paper_positions pp
              WHERE pp.symbol=tt.symbol AND pp.status='OPEN'
          )
    """))
    await db.commit()
    return {"closed_theses": int(result.rowcount or 0)}
