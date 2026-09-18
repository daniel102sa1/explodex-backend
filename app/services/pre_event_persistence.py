from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.execution_math import choose_target_for_min_net_rr
from app.services.lane_chase_guard import get_or_create_lane_anchor
from app.services.pre_event_prediction import build_pre_event_prediction

VERSION = "pre_event_persistence_v2_no_chase_anchor"


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


def _geometry_ok(side: str, entry: float, stop: float, target: float) -> bool:
    if side == "LONG":
        return stop < entry < target
    if side == "SHORT":
        return target < entry < stop
    return False


async def persist_pre_event_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
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
    candidates = 0
    lane_created = 0
    no_chase = 0
    waiting_original_zone = 0
    rejected: dict[str, int] = {}

    def reject(name: str) -> None:
        rejected[name] = rejected.get(name, 0) + 1

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart:
            reject("missing_heart")
            continue

        contract = _d(heart.get("execution_contract"))
        event = _d(contract.get("event_risk")) or _d(heart.get("event_risk"))
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
        pre = build_pre_event_prediction(reason=reason, score=score, event_risk=event)
        heart["pre_event_prediction"] = pre
        contract["pre_event_prediction"] = pre

        if bool(pre.get("paper_candidate")):
            candidates += 1

        existing_lane = contract.get("permitted_paper_lane")
        primary_direction = str(contract.get("primary_direction") or heart.get("direction") or "").upper()
        pre_direction = str(pre.get("direction") or "").upper()
        preparation = _f(pre.get("preparation_score"))
        supporting = int(pre.get("supporting_signals") or 0)
        risk_score = _f(row.get("risk_score"), 100.0)
        current = _f(row.get("current_price"))
        metrics = _d(reason.get("metrics"))
        plan = _d(heart.get("plan"))
        stop = _f(plan.get("stop_loss"))
        targets = [("TP1", _f(plan.get("tp1"))), ("TP2", _f(plan.get("tp2"))), ("TP3", _f(plan.get("tp3")))]

        # Capture the origin as soon as the precursor becomes a valid candidate
        # and agrees with the Heart, even if a higher-priority lane currently
        # exists. This prevents PRE_EVENT from anchoring later at a worse price.
        precursor_origin_valid = bool(
            pre.get("paper_candidate")
            and primary_direction in {"LONG", "SHORT"}
            and pre_direction == primary_direction
            and preparation >= 68
            and supporting >= 4
            and current > 0
            and stop > 0
        )

        half_band_pct = 0.18
        proposed_low = current * (1.0 - half_band_pct / 100.0) if current > 0 else 0.0
        proposed_high = current * (1.0 + half_band_pct / 100.0) if current > 0 else 0.0

        anchor = await get_or_create_lane_anchor(
            db,
            symbol=str(row.get("symbol") or ""),
            lane="PRE_EVENT_PAPER",
            direction=pre_direction,
            current_price=current,
            proposed_entry_low=proposed_low,
            proposed_entry_high=proposed_high,
            invalidation_price=stop,
            atr_pct=_f(metrics.get("atr_pct"), 0.8),
            ttl_minutes=240,
            create_allowed=precursor_origin_valid,
        )

        entry_low = _f(anchor.get("entry_low"), proposed_low)
        entry_high = _f(anchor.get("entry_high"), proposed_high)
        anchor_status = str(anchor.get("status") or "")
        anchor_reason = str(anchor.get("reason") or "")
        anchor_entry_ok = bool(anchor.get("eligible_now"))

        blockers: list[str] = []
        if existing_lane:
            blockers.append("higher_priority_lane_exists")
        if not bool(pre.get("paper_candidate")):
            blockers.append("pre_event_not_candidate")
        if primary_direction not in {"LONG", "SHORT"} or pre_direction != primary_direction:
            blockers.append("pre_event_not_aligned_with_primary")
        if preparation < 68:
            blockers.append("preparation_below_68")
        if supporting < 4:
            blockers.append("fewer_than_4_precursors")
        if risk_score > 55:
            blockers.append("risk_score_above_55")
        if bool(event.get("block_new_entries")):
            blockers.append("critical_event_block")
        if current <= 0 or stop <= 0:
            blockers.append("missing_price_or_stop")

        if precursor_origin_valid and not anchor_entry_ok:
            if anchor_status == "NO_CHASE":
                blockers.append("pre_event_no_chase_original_zone")
                no_chase += 1
            elif anchor_status == "INVALIDATED":
                blockers.append("pre_event_anchor_invalidated")
            elif anchor_reason == "waiting_original_anchor_zone":
                blockers.append("pre_event_wait_original_entry_zone")
                waiting_original_zone += 1
            elif anchor_status == "NO_ANCHOR":
                blockers.append("pre_event_anchor_unavailable")

        expected_math = {"accepted": False, "reason": "not_evaluated"}
        non_math_blockers = [b for b in blockers if b not in {"higher_priority_lane_exists"}]
        if not non_math_blockers:
            expected_math = choose_target_for_min_net_rr(
                side=pre_direction,
                entry=current,
                stop=stop,
                targets=targets,
                expected_hold_hours=4.0,
                min_net_rr=2.8,
            )
            if not expected_math.get("accepted"):
                blockers.append("pre_event_net_rr_below_2_8")

        chosen = _d(expected_math.get("chosen_target"))
        target = _f(chosen.get("price"))
        if target > 0 and not _geometry_ok(pre_direction, current, stop, target):
            blockers.append("invalid_pre_event_geometry")

        lane = {
            "lane": "PRE_EVENT_PAPER",
            "paper_only": True,
            "experimental": True,
            "eligible": not blockers,
            "direction": pre_direction,
            "entry_low": round(entry_low, 12),
            "entry_high": round(entry_high, 12),
            "anchor_price": anchor.get("anchor_price"),
            "chase_limit": anchor.get("chase_limit"),
            "entry_zone_frozen": anchor_status not in {"NO_ANCHOR", ""},
            "entry_zone_recenter_on_scan": False,
            "lane_anchor": anchor,
            "stop_loss": stop,
            "target_name": chosen.get("name"),
            "target_price": chosen.get("price"),
            "tp1": plan.get("tp1"),
            "tp2": plan.get("tp2"),
            "tp3": plan.get("tp3"),
            "execution_math": expected_math,
            "pre_event_type": pre.get("pre_event_type"),
            "preparation_score": preparation,
            "supporting_signals": supporting,
            "evidence": pre.get("evidence"),
            "missing": pre.get("missing"),
            "suggested_horizon": pre.get("suggested_horizon"),
            "risk_budget_pct": 0.25,
            "max_leverage": 2,
            "max_hold_minutes": 240,
            "event_risk_multiplier": _f(event.get("risk_multiplier"), 1.0),
            "blockers": list(dict.fromkeys(blockers)),
            "source": "HEART_PRE_EVENT_RESEARCH",
            "reason": "PRE_EVENT usa la primera banda precursora congelada; no puede mover la entrada detrás del precio.",
        }
        lanes = _d(contract.get("lanes"))
        lanes["pre_event_paper"] = lane
        contract["lanes"] = lanes

        if lane["eligible"] and not existing_lane:
            contract["permitted_paper_lane"] = "PRE_EVENT_PAPER"
            contract["priority"] = ["TACTICAL", "AGGRESSIVE_PAPER", "SWING_PAPER", "PRE_EVENT_PAPER"]
            lane_created += 1
        elif lane["eligible"] and existing_lane:
            reject("higher_priority_lane_exists")
        else:
            for blocker in lane["blockers"]:
                reject(str(blocker))

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
        "pre_event_candidates": candidates,
        "pre_event_lanes_created": lane_created,
        "no_chase": no_chase,
        "waiting_original_zone": waiting_original_zone,
        "rejected": rejected,
        "paper_only": True,
        "creates_real_entry": False,
        "entry_zones_frozen": True,
    }
