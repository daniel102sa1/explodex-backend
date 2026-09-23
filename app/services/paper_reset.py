from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import text

from app.database import engine
from app.services.paper_portfolio import STARTING_BALANCE

RESET_ENV = "EXPLODEX_PAPER_RESET_TOKEN"
MARKER_TABLE = "system_reset_markers"

_EXACT_TABLES = {
    "signals",
    "alerts",
    "trades",
    "trade_events",
    "scanner_runs",
}

_PREFIXES = (
    "paper_",
    "validation_",
    "edge_",
    "verdict_",
    "shadow_",
    "formula_",
    "entry_",
    "pre_event_",
    "structure_",
    "trajectory_",
    "horizon_",
    "macro_cycle_",
    "microstructure_",
    "quant_",
    "market_breadth_",
    "context_meta_",
    "context_veto_",
    "rolling_context_",
    "plan_lifecycle_",
    "tp1_",
    "selective_precision_",
    "fusion_edge_",
    "runner_",
    "event_risk_",
    "elliott_",
    "trade_thes",
)


def _is_resettable_table(name: str) -> bool:
    value = str(name or "").strip().lower()
    if not value or value == MARKER_TABLE:
        return False
    return value in _EXACT_TABLES or value.startswith(_PREFIXES)


async def maybe_reset_paper_baseline() -> dict[str, Any]:
    """One-shot destructive PAPER reset, keyed by an explicit environment token.

    This is intentionally startup-only so the reset occurs before scanners and
    PAPER execution resume. The marker table is preserved so the same token is
    idempotent across redeploys.
    """
    token = str(os.getenv(RESET_ENV, "") or "").strip()
    if not token:
        return {"requested": False, "applied": False, "reason": "no_reset_token"}

    async with engine.begin() as conn:
        await conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {MARKER_TABLE} (
                token TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                details JSONB NOT NULL DEFAULT '{{}}'::jsonb
            )
        """))
        seen = await conn.execute(
            text(f"SELECT 1 FROM {MARKER_TABLE} WHERE token=:token"),
            {"token": token},
        )
        if seen.scalar_one_or_none():
            return {"requested": True, "applied": False, "reason": "token_already_applied"}

        rows = (
            await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
            )
        ).scalars().all()
        tables = [str(name) for name in rows if _is_resettable_table(str(name))]

        counts: dict[str, int] = {}
        for table in tables:
            # table names come only from pg_tables + strict prefix/exact allowlist.
            result = await conn.execute(text(f'SELECT COUNT(*) FROM "{table}"'))
            counts[table] = int(result.scalar_one() or 0)

        if tables:
            quoted = ", ".join(f'"{name}"' for name in tables)
            await conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))

        paper_accounts_exists = await conn.execute(text("SELECT to_regclass('public.paper_accounts')"))
        if paper_accounts_exists.scalar_one_or_none():
            await conn.execute(
                text("""
                    INSERT INTO paper_accounts (
                        id, starting_balance, cash_balance, realized_pnl, total_fees, created_at, updated_at
                    ) VALUES (1, :balance, :balance, 0, 0, NOW(), NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        starting_balance=EXCLUDED.starting_balance,
                        cash_balance=EXCLUDED.cash_balance,
                        realized_pnl=0,
                        total_fees=0,
                        updated_at=NOW()
                """),
                {"balance": STARTING_BALANCE},
            )

        details = {
            "starting_balance": STARTING_BALANCE,
            "tables_reset": tables,
            "rows_removed": counts,
            "mode": "PAPER_ONLY_CLEAN_ARSENAL_BASELINE",
        }
        await conn.execute(
            text(f"INSERT INTO {MARKER_TABLE} (token, details) VALUES (:token, CAST(:details AS JSONB))"),
            {"token": token, "details": json.dumps(details)},
        )

    return {
        "requested": True,
        "applied": True,
        "reason": "clean_paper_baseline_created",
        "starting_balance": STARTING_BALANCE,
        "tables_reset": tables,
        "rows_removed": counts,
    }
