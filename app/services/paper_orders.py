from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base


ORDER_LEDGER_VERSION = "paper_orders_v1"


def exit_role_for_reason(exit_reason: str | None) -> str:
    reason = str(exit_reason or "").upper()
    if reason in {"STOP", "AMBIGUOUS_STOP"}:
        return "STOP"
    if reason == "TP1":
        return "TP1"
    return "EXIT_MARKET"


def _close_action(position_side: str) -> str:
    return "SELL" if str(position_side).upper() == "LONG" else "BUY"


def _open_action(position_side: str) -> str:
    return "BUY" if str(position_side).upper() == "LONG" else "SELL"


async def ensure_paper_orders_schema(db: AsyncSession) -> None:
    # Parent PAPER tables must exist first because orders reference paper_positions.
    await base.ensure_paper_schema(db)
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_orders (
            id BIGSERIAL PRIMARY KEY,
            signal_id UUID REFERENCES validation_observations(signal_id) ON DELETE SET NULL,
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
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status, created_at DESC)"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_symbol ON paper_orders(symbol, created_at DESC)"
    ))
    await db.commit()


async def _insert_position_orders(db: AsyncSession, row: dict[str, Any]) -> None:
    common = {
        "signal_id": row.get("signal_id"),
        "position_id": row["id"],
        "symbol": row["symbol"],
        "position_side": row["side"],
        "quantity": row["quantity"],
        "leverage": row["leverage"],
        "opened_at": row["opened_at"],
    }

    await db.execute(text("""
        INSERT INTO paper_orders (
            signal_id, position_id, symbol, position_side, action, order_role, order_type, status,
            requested_price, fill_price, quantity, leverage, created_at, updated_at, filled_at, metadata
        ) VALUES (
            :signal_id, :position_id, :symbol, :position_side, :action, 'ENTRY', 'MARKET', 'FILLED',
            :entry_price, :entry_price, :quantity, :leverage, :opened_at, :opened_at, :opened_at,
            '{"paper_only":true,"source":"paper_position"}'::jsonb
        ) ON CONFLICT (position_id, order_role) DO NOTHING
    """), {
        **common,
        "action": _open_action(row["side"]),
        "entry_price": row["entry_price"],
    })

    await db.execute(text("""
        INSERT INTO paper_orders (
            signal_id, position_id, symbol, position_side, action, order_role, order_type, status,
            trigger_price, quantity, leverage, created_at, updated_at, metadata
        ) VALUES (
            :signal_id, :position_id, :symbol, :position_side, :action, 'STOP', 'STOP_MARKET', 'PENDING',
            :trigger_price, :quantity, :leverage, :opened_at, :opened_at,
            '{"paper_only":true,"protective":true}'::jsonb
        ) ON CONFLICT (position_id, order_role) DO NOTHING
    """), {
        **common,
        "action": _close_action(row["side"]),
        "trigger_price": row["stop_loss"],
    })

    await db.execute(text("""
        INSERT INTO paper_orders (
            signal_id, position_id, symbol, position_side, action, order_role, order_type, status,
            trigger_price, quantity, leverage, created_at, updated_at, metadata
        ) VALUES (
            :signal_id, :position_id, :symbol, :position_side, :action, 'TP1', 'TAKE_PROFIT_MARKET', 'PENDING',
            :trigger_price, :quantity, :leverage, :opened_at, :opened_at,
            '{"paper_only":true,"protective":true}'::jsonb
        ) ON CONFLICT (position_id, order_role) DO NOTHING
    """), {
        **common,
        "action": _close_action(row["side"]),
        "trigger_price": row["take_profit"],
    })


async def _apply_closed_state(db: AsyncSession, row: dict[str, Any]) -> None:
    role = exit_role_for_reason(row.get("exit_reason"))
    params = {
        "position_id": row["id"],
        "exit_price": row.get("exit_price"),
        "closed_at": row.get("closed_at"),
    }

    if role in {"STOP", "TP1"}:
        await db.execute(text("""
            UPDATE paper_orders
            SET status='FILLED', fill_price=:exit_price, filled_at=:closed_at,
                updated_at=:closed_at, canceled_at=NULL, cancel_reason=NULL
            WHERE position_id=:position_id AND order_role=:role
        """), {**params, "role": role})
        sibling = "TP1" if role == "STOP" else "STOP"
        await db.execute(text("""
            UPDATE paper_orders
            SET status='CANCELED', canceled_at=:closed_at, updated_at=:closed_at,
                cancel_reason=:cancel_reason
            WHERE position_id=:position_id AND order_role=:sibling AND status='PENDING'
        """), {
            "position_id": row["id"],
            "closed_at": row.get("closed_at"),
            "sibling": sibling,
            "cancel_reason": f"SIBLING_{role}_FILLED",
        })
        return

    await db.execute(text("""
        UPDATE paper_orders
        SET status='CANCELED', canceled_at=:closed_at, updated_at=:closed_at,
            cancel_reason='POSITION_EXITED_BY_MARKET'
        WHERE position_id=:position_id AND order_role IN ('STOP','TP1') AND status='PENDING'
    """), params)

    await db.execute(text("""
        INSERT INTO paper_orders (
            signal_id, position_id, symbol, position_side, action, order_role, order_type, status,
            requested_price, fill_price, quantity, leverage, created_at, updated_at, filled_at, metadata
        ) VALUES (
            :signal_id, :position_id, :symbol, :position_side, :action, 'EXIT_MARKET', 'MARKET', 'FILLED',
            :exit_price, :exit_price, :quantity, :leverage, :closed_at, :closed_at, :closed_at,
            CAST(:metadata AS JSONB)
        ) ON CONFLICT (position_id, order_role) DO NOTHING
    """), {
        "signal_id": row.get("signal_id"),
        "position_id": row["id"],
        "symbol": row["symbol"],
        "position_side": row["side"],
        "action": _close_action(row["side"]),
        "exit_price": row.get("exit_price"),
        "quantity": row["quantity"],
        "leverage": row["leverage"],
        "closed_at": row.get("closed_at"),
        "metadata": '{"paper_only":true,"exit_reason":"%s"}' % str(row.get("exit_reason") or "UNKNOWN"),
    })


async def sync_paper_orders(db: AsyncSession) -> dict[str, int]:
    """Idempotently reconstruct and persist the simulated order book from PAPER positions."""
    await ensure_paper_orders_schema(db)
    rows = (await db.execute(text("""
        SELECT id, signal_id, symbol, side, status, leverage, entry_price, stop_loss, take_profit,
               quantity, opened_at, closed_at, exit_price, exit_reason
        FROM paper_positions
        ORDER BY opened_at ASC
    """))).mappings().all()

    closed_positions = 0
    for raw in rows:
        row = dict(raw)
        await _insert_position_orders(db, row)
        if str(row.get("status") or "").upper() == "CLOSED":
            closed_positions += 1
            await _apply_closed_state(db, row)

    await db.commit()
    return {"synced_positions": len(rows), "closed_positions": closed_positions}


async def paper_order_history(
    db: AsyncSession,
    *,
    limit: int = 200,
    status: str | None = None,
) -> list[dict[str, Any]]:
    await sync_paper_orders(db)
    where = ""
    params: dict[str, Any] = {"limit": limit}
    if status:
        where = "WHERE status=:status"
        params["status"] = status.upper()
    rows = (await db.execute(text(f"""
        SELECT id, signal_id, position_id, symbol, position_side, action, order_role, order_type,
               status, requested_price, trigger_price, fill_price, quantity, leverage,
               created_at, updated_at, filled_at, canceled_at, cancel_reason, metadata
        FROM paper_orders
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT :limit
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def paper_order_stats(db: AsyncSession) -> dict[str, Any]:
    await sync_paper_orders(db)
    row = dict((await db.execute(text("""
        SELECT COUNT(*) AS total_orders,
               COUNT(*) FILTER (WHERE status='PENDING') AS pending,
               COUNT(*) FILTER (WHERE status='FILLED') AS filled,
               COUNT(*) FILTER (WHERE status='CANCELED') AS canceled,
               COUNT(*) FILTER (WHERE order_role='ENTRY') AS entries,
               COUNT(*) FILTER (WHERE order_role='STOP') AS stops,
               COUNT(*) FILTER (WHERE order_role='TP1') AS take_profits,
               COUNT(*) FILTER (WHERE order_role='EXIT_MARKET') AS market_exits
        FROM paper_orders
    """))).mappings().one())
    return {"version": ORDER_LEDGER_VERSION, "paper_only": True, **row}
