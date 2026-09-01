from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.event_risk_engine import build_event_risk

VERSION = "event_risk_persistence_v2_lane_risk"


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
        return float(value)
    except (TypeError, ValueError):
        return default


async def persist_event_risk_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
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
    blocked = 0
    abnormal = 0
    counts: dict[str, int] = {}

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart:
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
        event = build_event_risk(reason=reason, score=score)
        event_type = str(event.get("event_type") or "NORMAL")
        counts[event_type] = counts.get(event_type, 0) + 1
        if event_type != "NORMAL":
            abnormal += 1

        contract = _d(heart.get("execution_contract"))
        lanes = _d(contract.get("lanes"))
        risk_mult = max(0.0, min(1.0, _f(event.get("risk_multiplier"), 1.0)))
        for key in ("tactical", "aggressive_paper", "swing_paper"):
            lane = _d(lanes.get(key))
            if lane:
                lane["event_risk_multiplier"] = risk_mult
                lane["event_type"] = event_type
                lane["event_severity"] = event.get("severity")
                lane["event_directional_bias"] = event.get("directional_bias")
                lane["event_requires_extra_confirmation"] = event.get("require_extra_confirmation")
                lanes[key] = lane
        contract["lanes"] = lanes

        original_lane = contract.get("permitted_paper_lane")
        if bool(event.get("block_new_entries")):
            contract["event_original_permitted_paper_lane"] = original_lane
            contract["permitted_paper_lane"] = None
            contract["event_blocked_entry"] = True
            blocked += 1
        else:
            contract["event_blocked_entry"] = False

        contract["event_risk"] = event
        contract["event_risk_multiplier"] = risk_mult
        contract["event_requires_extra_confirmation"] = event.get("require_extra_confirmation")
        heart["event_risk"] = event
        heart["execution_contract"] = contract
        reason["explodex_heart"] = heart
        if prediction:
            prediction["explodex_heart"] = heart
            reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals SET reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {"signal_id": row["signal_id"], "reason": json.dumps(reason)})
        updated += 1

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "updated": updated,
        "abnormal": abnormal,
        "blocked": blocked,
        "counts": counts,
        "creates_entry": False,
    }
