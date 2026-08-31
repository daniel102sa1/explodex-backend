from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.trajectory_forecast import build_trajectory_forecast

VERSION = "trajectory_persistence_v1"


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


async def persist_trajectory_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.state,
               s.setup_score, s.risk_score, s.current_price, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.setup_score DESC NULLS LAST
    """), {"run_id": run_id})).mappings().all()

    updated = 0
    swing_ready = 0
    by_direction = {"LONG": 0, "SHORT": 0}

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        htf = _d(heart.get("higher_timeframe"))
        liquidity = _d(heart.get("liquidity_intelligence"))
        metrics = _d(reason.get("metrics"))
        if not htf or str(htf.get("bias") or "") in {"NOT_FETCHED", "UNKNOWN", ""}:
            continue

        score = {
            "direction": row.get("direction"),
            "state": row.get("state"),
            "setup_score": _f(row.get("setup_score")),
            "risk_score": _f(row.get("risk_score"), 100.0),
            "current_price": _f(row.get("current_price")),
            "metrics": metrics,
        }
        trajectory = build_trajectory_forecast(score, prediction, htf, liquidity)
        heart["trajectory_forecast"] = trajectory
        heart["trajectory_lane"] = {
            "paper_only": True,
            "independent_from_tactical_enter": True,
            "action": (
                f"SWING_{trajectory.get('direction')}"
                if trajectory.get("should_enter_paper_swing")
                else "OBSERVAR_TRAYECTORIA"
            ),
            "horizon": trajectory.get("horizon"),
            "message": "La trayectoria no obliga al Heart táctico a entrar; sirve para PAPER de 4h-48h.",
        }
        reason["explodex_heart"] = heart
        prediction["explodex_heart"] = heart
        reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals SET reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {"signal_id": row["signal_id"], "reason": json.dumps(reason)})
        updated += 1
        if trajectory.get("should_enter_paper_swing"):
            swing_ready += 1
            direction = str(trajectory.get("direction") or "")
            if direction in by_direction:
                by_direction[direction] += 1

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "updated": updated,
        "swing_ready": swing_ready,
        "by_direction": by_direction,
    }
