from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import text

from app.database import engine
from app.services.paper_portfolio import ARSENAL_DISPLAY_START

FULL_RESET_ENV = "EXPLODEX_PAPER_RESET_TOKEN"
FULL_RESET_PREFIX = "FULL_PAPER_BASELINE"
OPEN_RESET_ENV = "EXPLODEX_OPEN_PAPER_RESET_TOKEN"
MARKER_TABLE = "system_reset_markers"
MARKER_PREFIX = "OPEN_PAPER_ONLY"
REPAIR_ENV = "EXPLODEX_REPAIR_ARSENAL_RESET_TOKEN"
REPAIR_PREFIX = "REPAIR_NEW_ARSENAL_OPEN"


def _reset_marker_key(token: str) -> str:
    return f"{MARKER_PREFIX}::{str(token or '').strip()}"


def _is_open_position_reset_target(name: str) -> bool:
    """Guardrail: an open-position reset may touch only the canonical PAPER ledger."""
    return str(name or "").strip().lower() == "paper_positions"


async def maybe_reset_full_paper_baseline() -> dict[str, Any]:
    """One-shot destructive reset of the visible PAPER simulation only.

    Clears simulated positions/orders/equity history and restores the PAPER
    account to exactly 1,000 USDT. Market/scanner/model-learning tables are not
    touched so research memory remains available for the new experiment.
    """
    token = str(os.getenv(FULL_RESET_ENV, "") or "").strip()
    if not token:
        return {"requested": False, "applied": False, "reason": "no_full_reset_token"}

    marker_key = f"{FULL_RESET_PREFIX}::{token}"
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
                "reason": "full_reset_token_already_applied",
            }

        positions_exists = bool((await conn.execute(
            text("SELECT to_regclass('public.paper_positions')")
        )).scalar_one_or_none())
        account_exists = bool((await conn.execute(
            text("SELECT to_regclass('public.paper_accounts')")
        )).scalar_one_or_none())
        curve_exists = bool((await conn.execute(
            text("SELECT to_regclass('public.paper_equity_curve')")
        )).scalar_one_or_none())
        orders_exists = bool((await conn.execute(
            text("SELECT to_regclass('public.paper_orders')")
        )).scalar_one_or_none())

        if not positions_exists or not account_exists:
            return {
                "requested": True,
                "applied": False,
                "reason": "paper_ledger_not_ready",
            }

        positions_before = int((await conn.execute(
            text("SELECT COUNT(*) FROM paper_positions")
        )).scalar_one() or 0)
        open_before = int((await conn.execute(
            text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'")
        )).scalar_one() or 0)
        orders_before = int((await conn.execute(
            text("SELECT COUNT(*) FROM paper_orders")
        )).scalar_one() or 0) if orders_exists else 0
        curve_before = int((await conn.execute(
            text("SELECT COUNT(*) FROM paper_equity_curve")
        )).scalar_one() or 0) if curve_exists else 0

        # Orders reference positions, so clear the PAPER order ledger first.
        if orders_exists:
            await conn.execute(text("DELETE FROM paper_orders"))
        await conn.execute(text("DELETE FROM paper_positions"))
        if curve_exists:
            await conn.execute(text("DELETE FROM paper_equity_curve"))

        updated = await conn.execute(text("""
            UPDATE paper_accounts
            SET starting_balance=1000,
                cash_balance=1000,
                realized_pnl=0,
                total_fees=0,
                updated_at=NOW()
            WHERE id=1
        """))
        if int(updated.rowcount or 0) == 0:
            await conn.execute(text("""
                INSERT INTO paper_accounts (
                    id, starting_balance, cash_balance, realized_pnl, total_fees,
                    created_at, updated_at
                ) VALUES (1, 1000, 1000, 0, 0, NOW(), NOW())
            """))

        details = {
            "mode": "FULL_PAPER_BASELINE_1000",
            "positions_deleted": positions_before,
            "open_positions_deleted": open_before,
            "orders_deleted": orders_before,
            "equity_points_deleted": curve_before,
            "starting_balance": 1000,
            "cash_balance": 1000,
            "realized_pnl": 0,
            "total_fees": 0,
            "signals_preserved": True,
            "market_history_preserved": True,
            "learning_preserved": True,
            "paper_only": True,
        }
        await conn.execute(
            text(f"INSERT INTO {MARKER_TABLE} (token, details) VALUES (:token, CAST(:details AS JSONB))"),
            {"token": marker_key, "details": json.dumps(details)},
        )

    return {
        "requested": True,
        "applied": True,
        "reason": "full_paper_baseline_reset_to_1000",
        **details,
    }


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


async def maybe_repair_current_arsenal_positions() -> dict[str, Any]:
    """Undo only the accidental open-reset for positions from the new arsenal cohort.

    Pre-arsenal rows stay archived/hidden. This restores only rows that were OPEN,
    were marked CANCELLED by RESET_OPEN_ONLY, and were originally opened after the
    arsenal display cutoff. The normal PAPER manager will then resolve TP/SL/time
    outcomes from market candles, so no result is fabricated here.
    """
    token = str(os.getenv(REPAIR_ENV, "") or "").strip()
    if not token:
        return {"requested": False, "applied": False, "reason": "no_repair_token"}

    marker_key = f"{REPAIR_PREFIX}::{token}"
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
            return {"requested": True, "applied": False, "reason": "repair_token_already_applied"}

        positions_exists = await conn.execute(text("SELECT to_regclass('public.paper_positions')"))
        if not positions_exists.scalar_one_or_none():
            return {"requested": True, "applied": False, "reason": "paper_positions_not_ready"}

        candidates = int((
            await conn.execute(text("""
                SELECT COUNT(*)
                FROM paper_positions
                WHERE status='CANCELLED'
                  AND exit_reason='RESET_OPEN_ONLY'
                  AND opened_at >= :cutoff
            """), {"cutoff": ARSENAL_DISPLAY_START})
        ).scalar_one() or 0)

        updated = await conn.execute(text("""
            UPDATE paper_positions
            SET
                status='OPEN',
                closed_at=NULL,
                exit_reason=NULL,
                exit_price=NULL,
                gross_pnl=NULL,
                net_pnl=NULL,
                metadata=(COALESCE(metadata, '{}'::jsonb)
                    - 'open_reset_cancelled'
                    - 'open_reset_at')
                    || jsonb_build_object(
                        'open_reset_repaired', TRUE,
                        'open_reset_repaired_at', NOW()
                    )
            WHERE status='CANCELLED'
              AND exit_reason='RESET_OPEN_ONLY'
              AND opened_at >= :cutoff
        """), {"cutoff": ARSENAL_DISPLAY_START})
        restored = int(updated.rowcount or 0)

        details = {
            "mode": "REPAIR_NEW_ARSENAL_OPEN_ONLY",
            "cutoff": ARSENAL_DISPLAY_START.isoformat(),
            "candidates": candidates,
            "positions_restored": restored,
            "pre_arsenal_positions_left_archived": True,
            "history_and_learning_untouched": True,
        }
        await conn.execute(
            text(f"INSERT INTO {MARKER_TABLE} (token, details) VALUES (:token, CAST(:details AS JSONB))"),
            {"token": marker_key, "details": json.dumps(details)},
        )

    return {
        "requested": True,
        "applied": True,
        "reason": "new_arsenal_open_positions_restored",
        **details,
    }


# Backward-compatible import name. Its behavior is intentionally no longer a
# broad baseline wipe. Keeping the alias prevents stale callers from restoring
# the dangerous semantics accidentally.
async def maybe_reset_paper_baseline() -> dict[str, Any]:
    return await maybe_reset_open_paper_positions()
