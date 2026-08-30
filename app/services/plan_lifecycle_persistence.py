from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.plan_lifecycle_guard import expire_exhausted_plan

VERSION = "plan_lifecycle_persistence_v1"


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


async def expire_exhausted_plans_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    """Close missed plans that already passed TP3 and persist NO_ENTRAR clearly."""
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.current_price, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.created_at ASC
    """), {"run_id": run_id})).mappings().all()

    exhausted = 0
    symbols: list[str] = []
    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        thesis = _d(heart.get("thesis"))
        if not thesis or str(thesis.get("status") or "") == "IN_POSITION":
            continue

        current = _f(row.get("current_price"))
        updated = await expire_exhausted_plan(db, thesis=thesis, current_price=current)
        lifecycle = _d(updated.get("lifecycle_guard"))
        if not lifecycle:
            continue

        exhausted += 1
        symbols.append(str(row.get("symbol")))
        heart["thesis"] = updated
        heart["execution_allowed"] = False
        heart["state"] = "NO_TRADE"
        decision = _d(heart.get("action_decision"))
        decision.update({
            "action": "NO_ENTRAR",
            "should_enter": False,
            "via": "PLAN_EXHAUSTED",
            "reason": "El movimiento ya pasó TP3 sin entrada. El plan terminó; esperar un setup nuevo.",
            "plan_exhausted": True,
            "do_not_auto_reverse": True,
        })
        heart["action_decision"] = decision
        plan = _d(heart.get("plan"))
        plan.update({
            "status": "EXPIRED",
            "action": "PLAN_COMPLETADO_SIN_ENTRADA_BUSCAR_NUEVO_SETUP",
            "do_not_recalculate": True,
        })
        heart["plan"] = plan
        heart["lifecycle_guard"] = lifecycle

        if prediction:
            prediction["trade_thesis"] = updated
            prediction["plan_action"] = updated.get("action")
            prediction["explodex_heart"] = {
                **_d(prediction.get("explodex_heart")),
                "execution_allowed": False,
                "action_decision": decision,
                "plan": plan,
                "lifecycle_guard": lifecycle,
            }
            reason["prediction"] = prediction
        reason["explodex_heart"] = heart

        await db.execute(text("""
            UPDATE signals
            SET state='NO_TRADE', reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {"signal_id": row["signal_id"], "reason": json.dumps(reason)})

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "exhausted": exhausted,
        "symbols": symbols[:20],
        "rule": "TP3 pasado sin entrada termina el plan; nunca crea automáticamente la operación contraria.",
    }
