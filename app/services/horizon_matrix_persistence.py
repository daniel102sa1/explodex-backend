from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.horizon_forecast_matrix import build_horizon_forecast_matrix

VERSION = "horizon_matrix_persistence_v1"


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


async def persist_horizon_matrix_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.state,
               s.setup_score, s.risk_score, s.current_price,
               s.expected_duration_min_minutes, s.expected_duration_max_minutes,
               s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.created_at ASC
    """), {"run_id": run_id})).mappings().all()

    updated = 0
    conflicts = 0
    consensus = {"LONG": 0, "SHORT": 0, "MIXED": 0}

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart or not _d(heart.get("execution_contract")):
            continue

        score = {
            "symbol": row.get("symbol"),
            "direction": row.get("direction"),
            "state": row.get("state"),
            "setup_score": _f(row.get("setup_score")),
            "risk_score": _f(row.get("risk_score"), 100.0),
            "current_price": _f(row.get("current_price")),
            "expected_duration_min_minutes": row.get("expected_duration_min_minutes"),
            "expected_duration_max_minutes": row.get("expected_duration_max_minutes"),
            "metrics": _d(reason.get("metrics")),
        }
        matrix = build_horizon_forecast_matrix(score=score, prediction=prediction, heart=heart)
        contract = _d(heart.get("execution_contract"))
        contract["forecast_matrix"] = matrix
        contract["horizon_conflict"] = bool(matrix.get("horizon_conflict"))
        contract["horizon_consensus"] = matrix.get("consensus")
        heart["execution_contract"] = contract
        heart["forecast_matrix"] = matrix
        heart["single_source_of_truth"] = True
        reason["explodex_heart"] = heart
        if prediction:
            prediction["explodex_heart"] = heart
            reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals SET reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {"signal_id": row["signal_id"], "reason": json.dumps(reason)})
        updated += 1
        if matrix.get("horizon_conflict"):
            conflicts += 1
        key = str(matrix.get("consensus") or "MIXED")
        consensus[key] = consensus.get(key, 0) + 1

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "updated": updated,
        "horizon_conflicts": conflicts,
        "consensus": consensus,
    }
