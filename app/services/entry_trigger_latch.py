from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "entry_trigger_latch_v1"
ACTIVE = {"TRIGGERED", "IN_POSITION"}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _d(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


async def ensure_entry_latch_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS heart_entry_latches (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            thesis_id UUID,
            symbol VARCHAR(32) NOT NULL,
            direction VARCHAR(8) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'TRIGGERED',
            triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            closed_at TIMESTAMPTZ,
            trigger_fill NUMERIC(30,12),
            entry_low NUMERIC(30,12) NOT NULL,
            entry_high NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            invalidation_price NUMERIC(30,12) NOT NULL,
            tp1 NUMERIC(30,12) NOT NULL,
            tp2 NUMERIC(30,12) NOT NULL,
            tp3 NUMERIC(30,12) NOT NULL,
            chase_limit NUMERIC(30,12),
            source VARCHAR(48),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_heart_entry_latches_symbol_time ON heart_entry_latches(symbol, triggered_at DESC)"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_heart_entry_latches_status ON heart_entry_latches(status, updated_at DESC)"
    ))
    await db.commit()


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": VERSION,
        "id": str(row.get("id") or ""),
        "thesis_id": str(row.get("thesis_id") or "") or None,
        "symbol": row.get("symbol"),
        "direction": str(row.get("direction") or "").upper(),
        "status": str(row.get("status") or ""),
        "triggered_at": row.get("triggered_at").isoformat() if hasattr(row.get("triggered_at"), "isoformat") else row.get("triggered_at"),
        "closed_at": row.get("closed_at").isoformat() if hasattr(row.get("closed_at"), "isoformat") else row.get("closed_at"),
        "trigger_fill": _f(row.get("trigger_fill")),
        "entry_low": _f(row.get("entry_low")),
        "entry_high": _f(row.get("entry_high")),
        "stop_loss": _f(row.get("stop_loss")),
        "invalidation_price": _f(row.get("invalidation_price")),
        "tp1": _f(row.get("tp1")),
        "tp2": _f(row.get("tp2")),
        "tp3": _f(row.get("tp3")),
        "chase_limit": _f(row.get("chase_limit")),
        "source": row.get("source"),
        "entry_triggered": True,
        "metadata": _d(row.get("metadata")),
    }


async def active_entry_latch(db: AsyncSession, symbol: str) -> dict[str, Any] | None:
    await ensure_entry_latch_schema(db)
    row = (await db.execute(text("""
        SELECT * FROM heart_entry_latches
        WHERE symbol=:symbol AND status IN ('TRIGGERED','IN_POSITION')
        ORDER BY triggered_at DESC
        LIMIT 1
    """), {"symbol": symbol})).mappings().first()
    return _serialize(dict(row)) if row else None


async def trigger_entry_latch(
    db: AsyncSession,
    *,
    symbol: str,
    thesis: dict[str, Any] | None,
    plan: dict[str, Any],
    direction: str,
    current_price: float,
    source: str,
) -> dict[str, Any]:
    await ensure_entry_latch_schema(db)
    existing = await active_entry_latch(db, symbol)
    thesis_id = str((thesis or {}).get("id") or "") or None
    if existing and (not thesis_id or existing.get("thesis_id") in {None, thesis_id}):
        return existing

    await db.execute(text("""
        UPDATE heart_entry_latches
        SET status='SUPERSEDED', closed_at=NOW(), updated_at=NOW()
        WHERE symbol=:symbol AND status IN ('TRIGGERED','IN_POSITION')
    """), {"symbol": symbol})

    row = (await db.execute(text("""
        INSERT INTO heart_entry_latches (
            thesis_id, symbol, direction, status, trigger_fill,
            entry_low, entry_high, stop_loss, invalidation_price,
            tp1, tp2, tp3, chase_limit, source, metadata
        ) VALUES (
            CAST(:thesis_id AS UUID), :symbol, :direction, 'TRIGGERED', :trigger_fill,
            :entry_low, :entry_high, :stop_loss, :invalidation_price,
            :tp1, :tp2, :tp3, :chase_limit, :source, CAST(:metadata AS JSONB)
        ) RETURNING *
    """), {
        "thesis_id": thesis_id,
        "symbol": symbol,
        "direction": str(direction or "").upper(),
        "trigger_fill": current_price,
        "entry_low": _f(plan.get("entry_low")),
        "entry_high": _f(plan.get("entry_high")),
        "stop_loss": _f(plan.get("stop_loss")),
        "invalidation_price": _f(plan.get("invalidation_price"), _f(plan.get("stop_loss"))),
        "tp1": _f(plan.get("tp1")),
        "tp2": _f(plan.get("tp2")),
        "tp3": _f(plan.get("tp3")),
        "chase_limit": _f(plan.get("chase_limit")),
        "source": source,
        "metadata": json.dumps({
            "rule": "Una vez que ExplodeX dice ENTRAR, no vuelve a ESPERAR confirmación. El plan queda activado hasta stop/invalidation/TP3/cierre.",
            "manual_position_unknown": True,
        }),
    })).mappings().one()
    await db.commit()
    return _serialize(dict(row))


def _milestone(latch: dict[str, Any], current: float) -> str:
    direction = str(latch.get("direction") or "").upper()
    tp1, tp2, tp3 = _f(latch.get("tp1")), _f(latch.get("tp2")), _f(latch.get("tp3"))
    if direction == "LONG":
        if tp3 > 0 and current >= tp3:
            return "TP3"
        if tp2 > 0 and current >= tp2:
            return "TP2"
        if tp1 > 0 and current >= tp1:
            return "TP1"
    elif direction == "SHORT":
        if tp3 > 0 and current <= tp3:
            return "TP3"
        if tp2 > 0 and current <= tp2:
            return "TP2"
        if tp1 > 0 and current <= tp1:
            return "TP1"
    return "ACTIVE"


def _invalidated(latch: dict[str, Any], current: float) -> bool:
    direction = str(latch.get("direction") or "").upper()
    level = _f(latch.get("invalidation_price"), _f(latch.get("stop_loss")))
    if current <= 0 or level <= 0:
        return False
    return current <= level if direction == "LONG" else current >= level


async def resolve_entry_latch(
    db: AsyncSession,
    *,
    symbol: str,
    current_price: float,
    thesis: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    latch = await active_entry_latch(db, symbol)
    if not latch:
        return None

    terminal_thesis = str((thesis or {}).get("status") or "") in {"INVALIDATED", "CLOSED"}
    if _invalidated(latch, current_price) or terminal_thesis:
        await db.execute(text("""
            UPDATE heart_entry_latches
            SET status='INVALIDATED', closed_at=NOW(), updated_at=NOW(),
                metadata=metadata || CAST(:patch AS JSONB)
            WHERE id=CAST(:id AS UUID)
        """), {
            "id": latch["id"],
            "patch": json.dumps({"closed_price": current_price, "close_reason": "FROZEN_PLAN_INVALIDATED"}),
        })
        await db.commit()
        latch.update({"status": "INVALIDATED", "closed_at": datetime.now(timezone.utc).isoformat()})
        return latch

    milestone = _milestone(latch, current_price)
    latch["milestone"] = milestone
    if milestone == "TP3":
        await db.execute(text("""
            UPDATE heart_entry_latches
            SET status='COMPLETED', closed_at=NOW(), updated_at=NOW(),
                metadata=metadata || CAST(:patch AS JSONB)
            WHERE id=CAST(:id AS UUID)
        """), {
            "id": latch["id"],
            "patch": json.dumps({"closed_price": current_price, "close_reason": "TP3_REACHED_AFTER_ENTRY_TRIGGER"}),
        })
        await db.commit()
        latch.update({"status": "COMPLETED", "closed_at": datetime.now(timezone.utc).isoformat()})
    return latch


def latched_action(
    *,
    latch: dict[str, Any],
    current_price: float,
    thesis: dict[str, Any] | None,
) -> dict[str, Any]:
    direction = str(latch.get("direction") or "").upper()
    status = str(latch.get("status") or "")
    milestone = str(latch.get("milestone") or "ACTIVE")
    low, high = _f(latch.get("entry_low")), _f(latch.get("entry_high"))
    in_zone = current_price > 0 and low > 0 and high > 0 and min(low, high) <= current_price <= max(low, high)
    thesis_status = str((thesis or {}).get("status") or "")
    paper_in_position = thesis_status == "IN_POSITION"

    if status == "INVALIDATED":
        return {
            "action": "NO_ENTRAR",
            "should_enter": False,
            "direction": direction,
            "via": "ENTRY_LATCH_INVALIDATED",
            "entry_latched": True,
            "entry_latch_status": status,
            "reason": "La entrada ya había sido activada, pero el plan congelado quedó invalidado. No abrir ni girar automáticamente.",
        }
    if status == "COMPLETED" or milestone == "TP3":
        return {
            "action": "PLAN_COMPLETADO",
            "should_enter": False,
            "direction": direction,
            "via": "ENTRY_LATCH_TP3",
            "entry_latched": True,
            "entry_latch_status": "COMPLETED",
            "milestone": "TP3",
            "reason": "La entrada fue activada anteriormente y el movimiento ya alcanzó TP3. Si entraste, el plan se considera completado; no perseguir una entrada nueva.",
        }

    # Keep PAPER executable while the original entry zone is still available.
    # The visible action is no longer a fresh recommendation: it is a latched plan.
    paper_can_fill = in_zone and not paper_in_position
    if paper_in_position:
        reason = f"Operación {direction} activa en PAPER. Mantener stop/TP originales; un recálculo débil no cancela la entrada ya tomada."
    elif in_zone:
        reason = f"ExplodeX ya activó la entrada {direction}. Si ya entraste, mantén el plan; PAPER todavía puede tomar el fill mientras siga dentro de la zona."
    else:
        reason = f"ExplodeX ya activó la entrada {direction}. Si ya entraste, mantén el plan; si no entraste, no persigas ahora que el precio salió de la zona."

    return {
        "action": f"MANTENER_{direction}",
        "should_enter": paper_can_fill,
        "direction": direction,
        "via": "ENTRY_TRIGGER_LATCH",
        "entry_latched": True,
        "entry_latch_status": "IN_POSITION" if paper_in_position else "TRIGGERED",
        "price_in_entry_zone": in_zone,
        "milestone": milestone,
        "reason": reason,
    }
