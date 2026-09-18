from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.lane_chase_guard import get_or_create_lane_anchor
from app.services.trajectory_forecast import build_trajectory_forecast

VERSION = "trajectory_persistence_v2_no_chase_anchor"


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
    no_chase = 0
    waiting_original_zone = 0
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
        base_ready = bool(trajectory.get("should_enter_paper_swing"))
        plan = _d(trajectory.get("swing_plan"))
        direction = str(trajectory.get("direction") or "").upper()
        current = _f(row.get("current_price"))
        max_hold = int(trajectory.get("max_hold_minutes") or plan.get("max_hold_minutes") or 720)

        anchor = await get_or_create_lane_anchor(
            db,
            symbol=str(row.get("symbol") or ""),
            lane="SWING_PAPER",
            direction=direction,
            current_price=current,
            proposed_entry_low=_f(plan.get("entry_low")),
            proposed_entry_high=_f(plan.get("entry_high")),
            invalidation_price=_f(plan.get("structural_stop")),
            atr_pct=_f(metrics.get("atr_pct"), _f(plan.get("robust_4h_range_pct"), 0.8)),
            ttl_minutes=min(720, max(180, max_hold // 2)),
            create_allowed=base_ready,
        )

        if anchor.get("status") not in {"NO_ANCHOR", None}:
            plan["entry_low"] = anchor.get("entry_low")
            plan["entry_high"] = anchor.get("entry_high")
            plan["anchor_price"] = anchor.get("anchor_price")
            plan["chase_limit"] = anchor.get("chase_limit")
            plan["entry_zone_frozen"] = True
            plan["entry_zone_recenter_on_scan"] = False
            trajectory["swing_plan"] = plan

        anchor_reason = str(anchor.get("reason") or "")
        anchor_status = str(anchor.get("status") or "")
        anchor_entry_ok = bool(anchor.get("eligible_now"))
        trajectory["lane_anchor"] = anchor
        trajectory["chase_risk"] = anchor_status == "NO_CHASE"
        trajectory["entry_zone_frozen"] = anchor_status not in {"NO_ANCHOR", ""}
        trajectory["entry_zone_recenter_on_scan"] = False
        trajectory["should_enter_paper_swing"] = bool(base_ready and anchor_entry_ok)

        blockers = list(trajectory.get("blockers") or [])
        if base_ready and not anchor_entry_ok:
            if anchor_status == "NO_CHASE":
                blockers.append("swing_no_chase_original_zone")
                no_chase += 1
            elif anchor_status == "INVALIDATED":
                blockers.append("swing_anchor_invalidated")
            elif anchor_reason == "waiting_original_anchor_zone":
                blockers.append("swing_wait_original_entry_zone")
                waiting_original_zone += 1
            elif anchor_status == "NO_ANCHOR":
                blockers.append("swing_anchor_unavailable")
        trajectory["blockers"] = list(dict.fromkeys(blockers))

        heart["trajectory_forecast"] = trajectory
        heart["trajectory_lane"] = {
            "paper_only": True,
            "independent_from_tactical_enter": True,
            "action": (
                f"SWING_{trajectory.get('direction')}"
                if trajectory.get("should_enter_paper_swing")
                else "NO_CHASE_ESPERAR_RETEST"
                if trajectory.get("chase_risk")
                else "OBSERVAR_TRAYECTORIA"
            ),
            "horizon": trajectory.get("horizon"),
            "anchor": anchor,
            "message": (
                "SWING usa la primera zona válida congelada; los scans posteriores no pueden moverla detrás del precio."
            ),
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
            if direction in by_direction:
                by_direction[direction] += 1

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "updated": updated,
        "swing_ready": swing_ready,
        "no_chase": no_chase,
        "waiting_original_zone": waiting_original_zone,
        "by_direction": by_direction,
        "entry_zones_frozen": True,
    }
