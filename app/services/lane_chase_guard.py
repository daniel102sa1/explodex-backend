from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "lane_chase_guard_v1"
ACTIVE_STATUSES = {"ACTIVE", "NO_CHASE"}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def ensure_lane_anchor_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS heart_lane_anchors (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            symbol VARCHAR(32) NOT NULL,
            lane VARCHAR(32) NOT NULL,
            direction VARCHAR(8) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'ACTIVE',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            anchor_price NUMERIC(30,12) NOT NULL,
            entry_low NUMERIC(30,12) NOT NULL,
            entry_high NUMERIC(30,12) NOT NULL,
            chase_limit NUMERIC(30,12) NOT NULL,
            invalidation_price NUMERIC(30,12),
            last_price NUMERIC(30,12),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_lane_anchor_symbol_lane_time "
        "ON heart_lane_anchors(symbol, lane, created_at DESC)"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_lane_anchor_status "
        "ON heart_lane_anchors(status, expires_at)"
    ))
    await db.commit()


def build_anchor_geometry(
    *,
    direction: str,
    anchor_price: float,
    entry_low: float,
    entry_high: float,
    atr_pct: float = 0.8,
) -> dict[str, float]:
    direction = str(direction or "").upper()
    anchor_price = _f(anchor_price)
    low, high = sorted((_f(entry_low), _f(entry_high)))
    atr_pct = max(0.10, _f(atr_pct, 0.8))
    zone_width = max(0.0, high - low)
    atr_abs = anchor_price * atr_pct / 100.0 if anchor_price > 0 else 0.0

    # Give the original setup some normal breathing room, but never let the
    # chase threshold recenter with future scans.
    chase_buffer = max(zone_width * 0.75, atr_abs * 0.55, anchor_price * 0.0015)
    if direction == "LONG":
        chase_limit = high + chase_buffer
    elif direction == "SHORT":
        chase_limit = low - chase_buffer
    else:
        chase_limit = 0.0

    return {
        "anchor_price": anchor_price,
        "entry_low": low,
        "entry_high": high,
        "chase_limit": chase_limit,
        "chase_buffer": chase_buffer,
    }


def classify_anchor_price(
    *,
    direction: str,
    current_price: float,
    entry_low: float,
    entry_high: float,
    chase_limit: float,
    invalidation_price: float = 0.0,
) -> dict[str, Any]:
    direction = str(direction or "").upper()
    current = _f(current_price)
    low, high = sorted((_f(entry_low), _f(entry_high)))
    chase = _f(chase_limit)
    invalidation = _f(invalidation_price)

    invalidated = False
    if invalidation > 0:
        invalidated = (
            current <= invalidation if direction == "LONG"
            else current >= invalidation if direction == "SHORT"
            else False
        )
    if invalidated:
        return {"status": "INVALIDATED", "eligible": False, "reason": "anchor_invalidated"}

    if low > 0 and high > 0 and low <= current <= high:
        return {"status": "ACTIVE", "eligible": True, "reason": "inside_original_anchor_zone"}

    if direction == "LONG" and chase > 0 and current > chase:
        return {"status": "NO_CHASE", "eligible": False, "reason": "price_above_original_chase_limit"}
    if direction == "SHORT" and chase > 0 and current < chase:
        return {"status": "NO_CHASE", "eligible": False, "reason": "price_below_original_chase_limit"}

    return {"status": "ACTIVE", "eligible": False, "reason": "waiting_original_anchor_zone"}


def _serialize(row: dict[str, Any], current_price: float) -> dict[str, Any]:
    state = classify_anchor_price(
        direction=str(row.get("direction") or ""),
        current_price=current_price,
        entry_low=_f(row.get("entry_low")),
        entry_high=_f(row.get("entry_high")),
        chase_limit=_f(row.get("chase_limit")),
        invalidation_price=_f(row.get("invalidation_price")),
    )
    return {
        "version": VERSION,
        "id": str(row.get("id") or ""),
        "symbol": row.get("symbol"),
        "lane": row.get("lane"),
        "direction": row.get("direction"),
        "status": state["status"],
        "eligible_now": bool(state["eligible"]),
        "reason": state["reason"],
        "anchor_price": _f(row.get("anchor_price")),
        "entry_low": _f(row.get("entry_low")),
        "entry_high": _f(row.get("entry_high")),
        "chase_limit": _f(row.get("chase_limit")),
        "invalidation_price": _f(row.get("invalidation_price")),
        "created_at": row.get("created_at").isoformat() if hasattr(row.get("created_at"), "isoformat") else None,
        "expires_at": row.get("expires_at").isoformat() if hasattr(row.get("expires_at"), "isoformat") else None,
        "zone_is_frozen": True,
        "recenter_on_new_scan": False,
        "rule": "The first valid lane zone is frozen. Future scans may update evidence, but cannot move the entry band behind price.",
    }


async def _latest_anchor(db: AsyncSession, symbol: str, lane: str) -> dict[str, Any] | None:
    row = (await db.execute(text("""
        SELECT *
        FROM heart_lane_anchors
        WHERE symbol=:symbol AND lane=:lane
        ORDER BY created_at DESC
        LIMIT 1
    """), {"symbol": symbol, "lane": lane})).mappings().first()
    return dict(row) if row else None


async def get_or_create_lane_anchor(
    db: AsyncSession,
    *,
    symbol: str,
    lane: str,
    direction: str,
    current_price: float,
    proposed_entry_low: float,
    proposed_entry_high: float,
    invalidation_price: float = 0.0,
    atr_pct: float = 0.8,
    ttl_minutes: int = 240,
    create_allowed: bool = True,
) -> dict[str, Any]:
    await ensure_lane_anchor_schema(db)
    now = datetime.now(timezone.utc)
    symbol = str(symbol or "").upper()
    lane = str(lane or "").upper()
    direction = str(direction or "").upper()
    existing = await _latest_anchor(db, symbol, lane)

    if existing:
        status = str(existing.get("status") or "")
        expires_at = existing.get("expires_at")
        same_direction = str(existing.get("direction") or "").upper() == direction

        if status in ACTIVE_STATUSES and expires_at and expires_at > now and same_direction:
            state = classify_anchor_price(
                direction=direction,
                current_price=current_price,
                entry_low=_f(existing.get("entry_low")),
                entry_high=_f(existing.get("entry_high")),
                chase_limit=_f(existing.get("chase_limit")),
                invalidation_price=_f(existing.get("invalidation_price")),
            )
            await db.execute(text("""
                UPDATE heart_lane_anchors
                SET status=:status, last_price=:price, updated_at=NOW()
                WHERE id=:id
            """), {
                "id": existing["id"],
                "status": state["status"],
                "price": current_price,
            })
            await db.commit()
            existing["status"] = state["status"]
            existing["last_price"] = current_price
            return _serialize(existing, current_price)

        # Do not instantly replace a chased anchor with another one at the new
        # price. The old opportunity must expire or materially change direction.
        if status == "NO_CHASE" and expires_at and expires_at > now and same_direction:
            return _serialize(existing, current_price)

        if status in ACTIVE_STATUSES and expires_at and expires_at <= now:
            await db.execute(text("""
                UPDATE heart_lane_anchors
                SET status='EXPIRED', updated_at=NOW()
                WHERE id=:id
            """), {"id": existing["id"]})
            await db.commit()

        if status in ACTIVE_STATUSES and not same_direction:
            await db.execute(text("""
                UPDATE heart_lane_anchors
                SET status='SUPERSEDED', updated_at=NOW(),
                    metadata=metadata || CAST(:patch AS JSONB)
                WHERE id=:id
            """), {
                "id": existing["id"],
                "patch": json.dumps({"superseded_by_direction": direction}),
            })
            await db.commit()

    if not create_allowed:
        return {
            "version": VERSION,
            "symbol": symbol,
            "lane": lane,
            "direction": direction,
            "status": "NO_ANCHOR",
            "eligible_now": False,
            "reason": "anchor_creation_not_authorized",
            "zone_is_frozen": False,
            "recenter_on_new_scan": False,
        }

    geometry = build_anchor_geometry(
        direction=direction,
        anchor_price=current_price,
        entry_low=proposed_entry_low,
        entry_high=proposed_entry_high,
        atr_pct=atr_pct,
    )
    if (
        direction not in {"LONG", "SHORT"}
        or min(geometry["anchor_price"], geometry["entry_low"], geometry["entry_high"], geometry["chase_limit"]) <= 0
    ):
        return {
            "version": VERSION,
            "symbol": symbol,
            "lane": lane,
            "direction": direction,
            "status": "NO_ANCHOR",
            "eligible_now": False,
            "reason": "invalid_anchor_geometry",
            "zone_is_frozen": False,
            "recenter_on_new_scan": False,
        }

    expires_at = now + timedelta(minutes=max(15, int(ttl_minutes)))
    row = (await db.execute(text("""
        INSERT INTO heart_lane_anchors (
            symbol, lane, direction, status, expires_at, anchor_price,
            entry_low, entry_high, chase_limit, invalidation_price,
            last_price, metadata
        ) VALUES (
            :symbol, :lane, :direction, 'ACTIVE', :expires_at, :anchor_price,
            :entry_low, :entry_high, :chase_limit, :invalidation_price,
            :last_price, CAST(:metadata AS JSONB)
        )
        RETURNING *
    """), {
        "symbol": symbol,
        "lane": lane,
        "direction": direction,
        "expires_at": expires_at,
        "anchor_price": geometry["anchor_price"],
        "entry_low": geometry["entry_low"],
        "entry_high": geometry["entry_high"],
        "chase_limit": geometry["chase_limit"],
        "invalidation_price": _f(invalidation_price),
        "last_price": current_price,
        "metadata": json.dumps({
            "created_from_first_valid_lane": True,
            "atr_pct_at_creation": _f(atr_pct),
            "chase_buffer": geometry["chase_buffer"],
        }),
    })).mappings().one()
    await db.commit()
    return _serialize(dict(row), current_price)


async def mark_lane_anchor_used(db: AsyncSession, *, symbol: str, lane: str) -> None:
    await ensure_lane_anchor_schema(db)
    await db.execute(text("""
        UPDATE heart_lane_anchors
        SET status='USED', used_at=COALESCE(used_at, NOW()), updated_at=NOW()
        WHERE id=(
            SELECT id FROM heart_lane_anchors
            WHERE symbol=:symbol AND lane=:lane AND status IN ('ACTIVE','NO_CHASE')
            ORDER BY created_at DESC LIMIT 1
        )
    """), {"symbol": str(symbol or "").upper(), "lane": str(lane or "").upper()})
    await db.commit()
