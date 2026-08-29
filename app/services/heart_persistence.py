from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.trade_thesis import apply_trade_thesis, apply_thesis_to_score

HEART_PERSISTENCE_VERSION = "heart_persistence_v1"


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
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


def _execution_allowed(score: dict[str, Any], prediction: dict[str, Any], thesis: dict[str, Any]) -> bool:
    seq = prediction.get("sequence") if isinstance(prediction.get("sequence"), dict) else {}
    return (
        str(score.get("state") or "") == "READY"
        and str(prediction.get("phase") or "") == "ACTIVADO"
        and not bool(seq.get("chase_risk"))
        and (
            not thesis.get("frozen_plan")
            or str(thesis.get("status") or "") == "ENTER_NOW"
        )
    )


async def canonicalize_scanner_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    """Freeze/propagate one thesis into every persisted signal of a scanner run."""
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.state,
               s.setup_score, s.risk_score, s.confidence_pct, s.current_price,
               s.entry_low, s.entry_high, s.invalidation_price, s.stop_loss,
               s.tp1, s.tp2, s.tp3, s.expected_move_min_pct, s.expected_move_max_pct,
               s.expected_duration_min_minutes, s.expected_duration_max_minutes,
               s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.created_at ASC
    """), {"run_id": run_id})).mappings().all()

    updated = 0
    frozen = 0
    blocked = 0
    execution_ready = 0
    actions: dict[str, int] = {}

    for raw in rows:
        row = dict(raw)
        reason = _json(row.get("reason"))
        prediction = _json(reason.get("prediction"))
        if not prediction:
            continue

        score = {
            "symbol": row["symbol"],
            "direction": row.get("direction"),
            "state": row.get("state"),
            "setup_score": _f(row.get("setup_score")),
            "risk_score": _f(row.get("risk_score"), 100.0),
            "confidence_pct": row.get("confidence_pct"),
            "current_price": _f(row.get("current_price")),
            "entry_low": _f(row.get("entry_low")),
            "entry_high": _f(row.get("entry_high")),
            "invalidation_price": _f(row.get("invalidation_price")),
            "stop_loss": _f(row.get("stop_loss")),
            "tp1": _f(row.get("tp1")),
            "tp2": _f(row.get("tp2")),
            "tp3": _f(row.get("tp3")),
            "expected_move_min_pct": _f(row.get("expected_move_min_pct")),
            "expected_move_max_pct": _f(row.get("expected_move_max_pct")),
            "expected_duration_min_minutes": row.get("expected_duration_min_minutes"),
            "expected_duration_max_minutes": row.get("expected_duration_max_minutes"),
            "metrics": _json(reason.get("metrics")),
            "components": _json(reason.get("components")),
            "coinglass": _json(reason.get("coinglass")),
        }

        thesis = await apply_trade_thesis(
            db,
            symbol=row["symbol"],
            score=score,
            prediction=prediction,
        )
        score, prediction = apply_thesis_to_score(score, prediction, thesis)
        allowed = _execution_allowed(score, prediction, thesis)
        action = str(thesis.get("action") or "OBSERVAR")
        actions[action] = actions.get(action, 0) + 1
        if thesis.get("frozen_plan"):
            frozen += 1
        if str(score.get("state")) == "NO_TRADE":
            blocked += 1
        if allowed:
            execution_ready += 1

        heart = {
            "version": "explodex_heart_v1",
            "persistence_version": HEART_PERSISTENCE_VERSION,
            "symbol": row["symbol"],
            "direction": score.get("direction"),
            "state": score.get("state"),
            "execution_allowed": allowed,
            "prediction_phase": prediction.get("phase"),
            "prediction_type": prediction.get("type"),
            "thesis": thesis,
            "plan": {
                "source": "FROZEN_THESIS" if thesis.get("frozen_plan") else "CURRENT_HEART",
                "action": thesis.get("action") if thesis.get("frozen_plan") else "ESPERAR_CONFIRMACION",
                "entry_low": score.get("entry_low"),
                "entry_high": score.get("entry_high"),
                "invalidation_price": score.get("invalidation_price", score.get("stop_loss")),
                "stop_loss": score.get("stop_loss"),
                "tp1": score.get("tp1"),
                "tp2": score.get("tp2"),
                "tp3": score.get("tp3"),
                "do_not_recalculate": bool(thesis.get("frozen_plan")),
            },
            "score_is_probability": False,
        }
        reason["prediction"] = prediction
        reason["metrics"] = score.get("metrics") or {}
        reason["explodex_heart"] = heart

        await db.execute(text("""
            UPDATE signals
            SET direction=:direction, state=:state,
                entry_low=:entry_low, entry_high=:entry_high,
                invalidation_price=:invalidation_price, stop_loss=:stop_loss,
                tp1=:tp1, tp2=:tp2, tp3=:tp3,
                reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {
            "signal_id": row["signal_id"],
            "direction": score.get("direction"),
            "state": score.get("state"),
            "entry_low": score.get("entry_low"),
            "entry_high": score.get("entry_high"),
            "invalidation_price": score.get("invalidation_price", score.get("stop_loss")),
            "stop_loss": score.get("stop_loss"),
            "tp1": score.get("tp1"),
            "tp2": score.get("tp2"),
            "tp3": score.get("tp3"),
            "reason": json.dumps(reason),
        })
        updated += 1

    await db.commit()
    return {
        "version": HEART_PERSISTENCE_VERSION,
        "seen": len(rows),
        "updated": updated,
        "frozen_theses": frozen,
        "blocked": blocked,
        "execution_ready": execution_ready,
        "actions": actions,
    }
