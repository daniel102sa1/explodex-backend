from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.entry_trigger_latch import latched_action, resolve_entry_latch, trigger_entry_latch

VERSION = "entry_latch_persistence_v1"


def _d(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def apply_entry_latches_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.current_price, s.state, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.created_at ASC
    """), {"run_id": run_id})).mappings().all()

    triggered = 0
    latched = 0
    completed = 0
    invalidated = 0

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart:
            continue
        thesis = _d(heart.get("thesis"))
        plan = _d(heart.get("plan"))
        decision = _d(heart.get("action_decision"))
        current = _f(row.get("current_price"))
        direction = str(heart.get("direction") or decision.get("direction") or "").upper()

        if bool(decision.get("should_enter")) and str(decision.get("action") or "") in {"ENTRAR_LONG", "ENTRAR_SHORT"}:
            latch = await trigger_entry_latch(
                db,
                symbol=str(row.get("symbol")),
                thesis=thesis,
                plan=plan,
                direction=direction,
                current_price=current,
                source=str(decision.get("via") or "HEART_ENTER"),
            )
            triggered += 1
        else:
            latch = await resolve_entry_latch(
                db,
                symbol=str(row.get("symbol")),
                current_price=current,
                thesis=thesis,
            )

        if not latch:
            continue

        status = str(latch.get("status") or "")
        if status == "COMPLETED":
            completed += 1
        elif status == "INVALIDATED":
            invalidated += 1
        else:
            latched += 1

        new_decision = latched_action(
            latch=latch,
            current_price=current,
            thesis=thesis,
        )
        heart["entry_latch"] = latch
        heart["action_decision"] = new_decision
        heart["execution_allowed"] = bool(new_decision.get("should_enter"))
        if status in {"INVALIDATED", "COMPLETED"}:
            heart["state"] = "NO_TRADE"
        elif bool(new_decision.get("should_enter")):
            heart["state"] = "READY"
        else:
            heart["state"] = "ACTIVE_PLAN"

        plan["entry_triggered"] = True
        plan["entry_latch_status"] = status
        plan["triggered_at"] = latch.get("triggered_at")
        heart["plan"] = plan

        if prediction:
            prediction["explodex_heart"] = {
                **_d(prediction.get("explodex_heart")),
                "execution_allowed": heart["execution_allowed"],
                "action_decision": new_decision,
                "entry_latch": latch,
                "plan": plan,
            }
            reason["prediction"] = prediction
        reason["explodex_heart"] = heart

        signal_state = "READY" if bool(new_decision.get("should_enter")) else "NO_TRADE" if status in {"INVALIDATED", "COMPLETED"} else "ACTIVE_PLAN"
        await db.execute(text("""
            UPDATE signals
            SET state=:state, reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {
            "signal_id": row["signal_id"],
            "state": signal_state,
            "reason": json.dumps(reason),
        })

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "triggered": triggered,
        "latched": latched,
        "completed": completed,
        "invalidated": invalidated,
        "rule": "Después de ENTRAR, el Heart no vuelve a ESPERAR confirmación; mantiene el plan hasta invalidación/TP3/cierre.",
    }
