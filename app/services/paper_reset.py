from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import text

from app.database import engine

OPEN_RESET_ENV = "EXPLODEX_OPEN_PAPER_RESET_TOKEN"
MARKER_TABLE = "system_reset_markers"
MARKER_PREFIX = "OPEN_PAPER_ONLY"


def _reset_marker_key(token: str) -> str:
    return f"{MARKER_PREFIX}::{str(token or '').strip()}"


def _is_open_position_reset_target(name: str) -> bool:
    """Guardrail: an open-position reset may touch only the canonical PAPER ledger."""
    return str(name or "").strip().lower() == "paper_positions"


async def maybe_reset_open_paper_positions() -> dict[str, Any]:
    """One-shot reset of *open* canonical PAPER positions only.

    This deliberately preserves:
    - CLOSED PAPER history and realized PnL
    - account balances / fees
    - signals and scanner history
    - VNext / shadow / formula / verdict / edge learning
    - all market and research tables

    Open positions are marked CANCELLED instead of being hard-deleted. That makes
    them disappear from OPEN and CLOSED performance views while preserving the
    signal_id tombstone so the same old signal cannot be reopened immediately.
    """
    token = str(os.getenv(OPEN_RESET_ENV, "") or "").strip()
    if not token:
        return {"requested": False, "applied": False, "reason": "no_open_reset_token"}

    marker_key = _reset_marker_key(token)

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
            {"token": marker_key},
        )
        if seen.scalar_one_or_none():
            return {
                "requested": True,
                "applied": False,
                "reason": "open_reset_token_already_applied",
            }

        positions_exists = await conn.execute(text("SELECT to_regclass('public.paper_positions')"))
        if not positions_exists.scalar_one_or_none():
            return {
                "requested": True,
                "applied": False,
                "reason": "paper_positions_not_ready",
            }

        open_before = int(
            (
                await conn.execute(
                    text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'")
                )
            ).scalar_one()
            or 0
        )

        updated = await conn.execute(
            text("""
                UPDATE paper_positions
                SET
                    status='CANCELLED',
                    closed_at=COALESCE(closed_at, NOW()),
                    exit_reason='RESET_OPEN_ONLY',
                    metadata=COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                        'open_reset_cancelled', TRUE,
                        'open_reset_at', NOW()
                    )
                WHERE status='OPEN'
            """)
        )
        cancelled = int(updated.rowcount or 0)

        details = {
            "mode": "OPEN_PAPER_POSITIONS_ONLY",
            "canonical_table": "paper_positions",
            "open_before": open_before,
            "positions_cancelled": cancelled,
            "closed_history_preserved": True,
            "account_preserved": True,
            "signals_preserved": True,
            "learning_preserved": True,
        }
        await conn.execute(
            text(f"INSERT INTO {MARKER_TABLE} (token, details) VALUES (:token, CAST(:details AS JSONB))"),
            {"token": marker_key, "details": json.dumps(details)},
        )

    return {
        "requested": True,
        "applied": True,
        "reason": "open_paper_positions_cancelled",
        **details,
    }


# Backward-compatible import name. Its behavior is intentionally no longer a
# broad baseline wipe. Keeping the alias prevents stale callers from restoring
# the dangerous semantics accidentally.
async def maybe_reset_paper_baseline() -> dict[str, Any]:
    return await maybe_reset_open_paper_positions()
