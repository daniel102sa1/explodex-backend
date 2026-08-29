from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "paper_thesis_gate_v1"
COOLDOWN_MINUTES = 20
THESIS_TTL_MINUTES = 240


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def ensure_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_trade_theses (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(32) NOT NULL,
            direction VARCHAR(8) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'WAITING_ENTRY',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            cooldown_until TIMESTAMPTZ,
            signal_id UUID,
            entry_price NUMERIC(30,12) NOT NULL,
            entry_low NUMERIC(30,12) NOT NULL,
            entry_high NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            take_profit NUMERIC(30,12) NOT NULL,
            fingerprint_score NUMERIC(10,4),
            contradiction_count INTEGER NOT NULL DEFAULT 0,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_paper_thesis_symbol_time ON paper_trade_theses(symbol, created_at DESC)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_paper_thesis_status ON paper_trade_theses(status, updated_at DESC)"))
    await db.commit()


def classify_price_state(*, direction: str, price: float, entry_low: float, entry_high: float, stop: float) -> str:
    direction = str(direction or "").upper()
    if direction == "LONG" and price <= stop:
        return "INVALIDATED"
    if direction == "SHORT" and price >= stop:
        return "INVALIDATED"
    if entry_low <= price <= entry_high:
        return "ENTER_NOW"
    width = max(entry_high - entry_low, price * 0.0015)
    if direction == "LONG" and price > entry_high + width * 2.0:
        return "NO_CHASE"
    if direction == "SHORT" and price < entry_low - width * 2.0:
        return "NO_CHASE"
    return "WAITING_ENTRY"


async def _latest(db: AsyncSession, symbol: str) -> dict[str, Any] | None:
    row = (await db.execute(text("""
        SELECT * FROM paper_trade_theses
        WHERE symbol=:symbol
        ORDER BY created_at DESC
        LIMIT 1
    """), {"symbol": symbol})).mappings().first()
    return dict(row) if row else None


async def gate_candidate(
    db: AsyncSession,
    *,
    symbol: str,
    signal_id: str,
    direction: str,
    current_price: float,
    planned_entry: float,
    stop_loss: float,
    take_profit: float,
    fingerprint_score: float,
) -> dict[str, Any]:
    await ensure_schema(db)
    now = datetime.now(timezone.utc)
    direction = str(direction or "").upper()
    latest = await _latest(db, symbol)

    if latest:
        status = str(latest.get("status") or "")
        cooldown_until = latest.get("cooldown_until")
        expires_at = latest.get("expires_at")
        existing_direction = str(latest.get("direction") or "").upper()

        if cooldown_until and cooldown_until > now:
            return {
                "version": VERSION,
                "allowed": False,
                "status": "COOLDOWN",
                "locked_direction": existing_direction,
                "reason": "cooldown_after_invalidated_or_closed_thesis",
                "thesis_id": latest.get("id"),
            }

        if status in {"WAITING_ENTRY", "ENTER_NOW", "NO_CHASE", "IN_POSITION"}:
            if expires_at and expires_at <= now and status != "IN_POSITION":
                await db.execute(text("UPDATE paper_trade_theses SET status='EXPIRED', updated_at=NOW() WHERE id=:id"), {"id": latest["id"]})
                await db.commit()
            else:
                if direction != existing_direction:
                    await db.execute(text("""
                        UPDATE paper_trade_theses
                        SET contradiction_count=contradiction_count+1, updated_at=NOW(),
                            metadata=metadata || CAST(:patch AS JSONB)
                        WHERE id=:id
                    """), {
                        "id": latest["id"],
                        "patch": json.dumps({"last_opposite_candidate": direction, "last_opposite_at": now.isoformat()}),
                    })
                    await db.commit()
                    return {
                        "version": VERSION,
                        "allowed": False,
                        "status": status,
                        "locked_direction": existing_direction,
                        "reason": "opposite_signal_cannot_flip_active_thesis",
                        "thesis_id": latest.get("id"),
                        "frozen_entry": _f(latest.get("entry_price")),
                        "frozen_stop": _f(latest.get("stop_loss")),
                        "frozen_tp": _f(latest.get("take_profit")),
                    }

                state = classify_price_state(
                    direction=existing_direction,
                    price=current_price,
                    entry_low=_f(latest.get("entry_low")),
                    entry_high=_f(latest.get("entry_high")),
                    stop=_f(latest.get("stop_loss")),
                )
                if state == "INVALIDATED" and status != "IN_POSITION":
                    cooldown = now + timedelta(minutes=COOLDOWN_MINUTES)
                    await db.execute(text("""
                        UPDATE paper_trade_theses
                        SET status='INVALIDATED', cooldown_until=:cooldown, updated_at=NOW()
                        WHERE id=:id
                    """), {"id": latest["id"], "cooldown": cooldown})
                    await db.commit()
                    return {
                        "version": VERSION,
                        "allowed": False,
                        "status": "INVALIDATED",
                        "locked_direction": existing_direction,
                        "reason": "original_thesis_invalidated_wait_cooldown",
                        "thesis_id": latest.get("id"),
                    }

                if status != "IN_POSITION" and state != status:
                    await db.execute(text("UPDATE paper_trade_theses SET status=:status, updated_at=NOW() WHERE id=:id"), {"id": latest["id"], "status": state})
                    await db.commit()
                return {
                    "version": VERSION,
                    "allowed": state == "ENTER_NOW" and status != "IN_POSITION",
                    "status": "IN_POSITION" if status == "IN_POSITION" else state,
                    "locked_direction": existing_direction,
                    "reason": (
                        "inside_frozen_entry_zone" if state == "ENTER_NOW"
                        else "do_not_chase_wait_retest" if state == "NO_CHASE"
                        else "keep_waiting_for_frozen_entry"
                    ),
                    "thesis_id": latest.get("id"),
                    "frozen_entry": _f(latest.get("entry_price")),
                    "frozen_stop": _f(latest.get("stop_loss")),
                    "frozen_tp": _f(latest.get("take_profit")),
                }

    # Create a new thesis from the first qualified candidate. We deliberately use
    # a narrow zone around the planned observable entry and freeze it thereafter.
    width = max(abs(planned_entry - stop_loss) * 0.12, planned_entry * 0.0015)
    entry_low = planned_entry - width
    entry_high = planned_entry + width
    state = classify_price_state(
        direction=direction,
        price=current_price,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop_loss,
    )
    expires_at = now + timedelta(minutes=THESIS_TTL_MINUTES)
    row = (await db.execute(text("""
        INSERT INTO paper_trade_theses (
            symbol, direction, status, expires_at, signal_id,
            entry_price, entry_low, entry_high, stop_loss, take_profit,
            fingerprint_score, metadata
        ) VALUES (
            :symbol, :direction, :status, :expires_at, CAST(:signal_id AS UUID),
            :entry_price, :entry_low, :entry_high, :stop_loss, :take_profit,
            :fingerprint_score, CAST(:metadata AS JSONB)
        ) RETURNING id
    """), {
        "symbol": symbol,
        "direction": direction,
        "status": state,
        "expires_at": expires_at,
        "signal_id": signal_id,
        "entry_price": planned_entry,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "fingerprint_score": fingerprint_score,
        "metadata": json.dumps({"version": VERSION, "paper_only": True}),
    })).mappings().one()
    await db.commit()
    return {
        "version": VERSION,
        "allowed": state == "ENTER_NOW",
        "status": state,
        "locked_direction": direction,
        "reason": "new_thesis_frozen_inside_entry" if state == "ENTER_NOW" else "new_thesis_frozen_wait_for_entry",
        "thesis_id": row["id"],
        "frozen_entry": planned_entry,
        "frozen_stop": stop_loss,
        "frozen_tp": take_profit,
    }


async def mark_in_position(db: AsyncSession, *, symbol: str, thesis_id: int | None) -> None:
    if thesis_id is None:
        return
    await db.execute(text("""
        UPDATE paper_trade_theses
        SET status='IN_POSITION', updated_at=NOW()
        WHERE id=:id AND symbol=:symbol
    """), {"id": thesis_id, "symbol": symbol})
    await db.commit()


async def close_for_symbol(db: AsyncSession, *, symbol: str, reason: str) -> None:
    await ensure_schema(db)
    cooldown = datetime.now(timezone.utc) + timedelta(minutes=15)
    await db.execute(text("""
        UPDATE paper_trade_theses
        SET status='CLOSED', cooldown_until=:cooldown, updated_at=NOW(),
            metadata=metadata || CAST(:patch AS JSONB)
        WHERE id=(
            SELECT id FROM paper_trade_theses
            WHERE symbol=:symbol AND status='IN_POSITION'
            ORDER BY created_at DESC LIMIT 1
        )
    """), {
        "symbol": symbol,
        "cooldown": cooldown,
        "patch": json.dumps({"close_reason": reason}),
    })
    await db.commit()
