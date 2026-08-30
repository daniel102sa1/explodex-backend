from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "plan_lifecycle_guard_v1"
COOLDOWN_MINUTES = 10


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def plan_target_exhausted(thesis: dict[str, Any], current_price: float) -> bool:
    """Return True only when an unentered frozen plan has already passed TP3.

    Passing TP3 means the opportunity happened without us. It is not a reason
    to chase the old direction and it is not, by itself, evidence for the
    opposite direction.
    """
    if not thesis or not bool(thesis.get("frozen_plan")):
        return False
    if str(thesis.get("status") or "") == "IN_POSITION":
        return False
    if current_price <= 0:
        return False

    direction = str(thesis.get("direction") or "").upper()
    tp3 = _f(thesis.get("tp3"))
    if tp3 <= 0:
        return False
    if direction == "LONG":
        return current_price >= tp3
    if direction == "SHORT":
        return current_price <= tp3
    return False


async def expire_exhausted_plan(
    db: AsyncSession,
    *,
    thesis: dict[str, Any],
    current_price: float,
) -> dict[str, Any]:
    """Close a missed plan after TP3 and return a terminal thesis payload."""
    if not plan_target_exhausted(thesis, current_price):
        return thesis

    thesis_id = str(thesis.get("id") or "")
    if not thesis_id:
        return thesis

    now = datetime.now(timezone.utc)
    cooldown = now + timedelta(minutes=COOLDOWN_MINUTES)
    patch = {
        "close_reason": "TARGETS_PASSED_WITHOUT_ENTRY",
        "exhausted_at_price": current_price,
        "lifecycle_guard_version": VERSION,
        "rule": "Pasar TP3 sin entrada cierra el plan; no autoriza un giro automático al lado contrario.",
    }
    await db.execute(text("""
        UPDATE trade_theses
        SET status='EXPIRED', closed_at=NOW(), updated_at=NOW(),
            cooldown_until=:cooldown, last_price=:price,
            metadata=metadata || CAST(:patch AS JSONB)
        WHERE id=CAST(:id AS UUID)
          AND status IN ('WAITING_ENTRY','ENTER_NOW','NO_CHASE')
    """), {
        "id": thesis_id,
        "cooldown": cooldown,
        "price": current_price,
        "patch": json.dumps(patch),
    })
    await db.commit()

    out = dict(thesis)
    out.update({
        "status": "EXPIRED",
        "action": "PLAN_COMPLETADO_SIN_ENTRADA_BUSCAR_NUEVO_SETUP",
        "cooldown_until": cooldown.isoformat(),
        "last_price": current_price,
        "message": "El movimiento ya pasó TP3 sin entrada. Este plan terminó; no perseguir ni convertirlo automáticamente en SHORT/LONG contrario.",
        "lifecycle_guard": patch,
    })
    return out
