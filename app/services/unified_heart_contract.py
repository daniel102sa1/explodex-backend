from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.execution_math import choose_target_for_min_net_rr

VERSION = "unified_heart_contract_v1"
HEART_VERSION = "explodex_heart_v7_unified"


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


def _inside(price: float, low: float, high: float) -> bool:
    return price > 0 and low > 0 and high > 0 and min(low, high) <= price <= max(low, high)


def _hard_safety_clear(heart: dict[str, Any], prediction: dict[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    stack = _d(prediction.get("prediction_stack_v5"))
    veto = _d(stack.get("risk_veto"))
    sequence = _d(prediction.get("sequence"))
    guard = _d(prediction.get("decision_guard"))
    thesis = _d(heart.get("thesis"))
    latch = _d(heart.get("entry_latch"))

    if bool(veto.get("blocked")) or bool(veto.get("hard_block")):
        blockers.append("hard_veto")
    if bool(veto.get("invalidated")):
        blockers.append("prediction_invalidated")
    if bool(sequence.get("chase_risk")) or bool(veto.get("chase")):
        blockers.append("no_chase")
    if not bool(sequence.get("risk_guard_pass", guard.get("risk_guard_pass", True))):
        blockers.append("risk_guard")
    if str(thesis.get("status") or "") in {"INVALIDATED", "EXPIRED", "CLOSED"}:
        blockers.append("thesis_terminal")
    if str(thesis.get("action") or "") == "COOLDOWN_NO_CAMBIAR_DE_LADO":
        blockers.append("thesis_cooldown")
    if str(latch.get("status") or "") in {"TRIGGERED", "IN_POSITION"}:
        blockers.append("entry_already_triggered")
    return not blockers, blockers


def _plan_geometry(heart: dict[str, Any]) -> dict[str, float]:
    plan = _d(heart.get("plan"))
    return {
        "entry_low": _f(plan.get("entry_low")),
        "entry_high": _f(plan.get("entry_high")),
        "stop": _f(plan.get("stop_loss")),
        "tp1": _f(plan.get("tp1")),
        "tp2": _f(plan.get("tp2")),
        "tp3": _f(plan.get("tp3")),
    }


def _tactical_lane(heart: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    decision = _d(heart.get("action_decision"))
    plan = _d(heart.get("plan"))
    should_enter = bool(decision.get("should_enter")) and bool(heart.get("execution_allowed"))
    return {
        "lane": "TACTICAL",
        "paper_only": False,
        "eligible": should_enter,
        "action": decision.get("action"),
        "direction": decision.get("direction") or heart.get("direction"),
        "entry_low": plan.get("entry_low"),
        "entry_high": plan.get("entry_high"),
        "stop_loss": plan.get("stop_loss"),
        "target_name": decision.get("execution_target_name") or plan.get("execution_target_name"),
        "target_price": decision.get("execution_target_price") or plan.get("execution_target_price") or plan.get("tp1"),
        "tp1": plan.get("tp1"),
        "tp2": plan.get("tp2"),
        "tp3": plan.get("tp3"),
        "max_hold_minutes": score.get("expected_duration_max_minutes"),
        "risk_budget_pct": 1.0,
        "reason": decision.get("reason"),
        "source": "HEART_ACTION_DECISION",
    }


def _aggressive_lane(
    heart: dict[str, Any],
    score: dict[str, Any],
    prediction: dict[str, Any],
    safety_clear: bool,
    safety_blockers: list[str],
) -> dict[str, Any]:
    decision = _d(heart.get("action_decision"))
    ignition = _d(heart.get("ignition"))
    thesis = _d(heart.get("thesis"))
    geometry = _plan_geometry(heart)
    direction = str(heart.get("direction") or score.get("direction") or "").upper()
    current = _f(score.get("current_price"))
    blockers = list(safety_blockers)

    action = str(decision.get("action") or "").upper()
    stage = str(ignition.get("stage") or "").upper()
    ignition_score = _f(ignition.get("score"))
    if action != "ESPERAR":
        blockers.append("tactical_action_not_waiting")
    if not bool(thesis.get("frozen_plan")):
        blockers.append("no_frozen_thesis")
    if str(thesis.get("status") or "").upper() not in {"WAITING_ENTRY", "ENTER_NOW"}:
        blockers.append("thesis_not_waiting")
    if _f(score.get("risk_score"), 100.0) > 40.0:
        blockers.append("risk_above_40")
    if stage not in {"ARMED", "IGNITING"}:
        blockers.append("ignition_not_armed")
    if ignition_score < 76.0:
        blockers.append("ignition_below_76")
    if int(ignition.get("supporting_components") or 0) < 3:
        blockers.append("insufficient_ignition_support")
    if not _inside(current, geometry["entry_low"], geometry["entry_high"]):
        blockers.append("price_outside_entry_zone")

    expected_math = choose_target_for_min_net_rr(
        side=direction,
        entry=current,
        stop=geometry["stop"],
        targets=[("TP1", geometry["tp1"]), ("TP2", geometry["tp2"]), ("TP3", geometry["tp3"])],
        expected_hold_hours=2.0,
        min_net_rr=2.8,
    ) if current > 0 and geometry["stop"] > 0 else {"accepted": False}
    if not expected_math.get("accepted"):
        blockers.append("net_rr_below_2_8")
    chosen = _d(expected_math.get("chosen_target"))

    blockers = list(dict.fromkeys(blockers))
    return {
        "lane": "AGGRESSIVE_PAPER",
        "paper_only": True,
        "experimental": True,
        "eligible": safety_clear and not blockers,
        "direction": direction,
        "entry_low": geometry["entry_low"],
        "entry_high": geometry["entry_high"],
        "stop_loss": geometry["stop"],
        "target_name": chosen.get("name"),
        "target_price": chosen.get("price"),
        "execution_math": expected_math,
        "ignition_score": ignition_score,
        "ignition_stage": stage,
        "risk_budget_pct": 0.5,
        "max_leverage": 2,
        "max_hold_minutes": 120,
        "blockers": blockers,
        "reason": "Entrada temprana PAPER emitida por el mismo Heart; nunca sustituye la recomendación táctica principal.",
        "source": "HEART_IGNITION_EXPERIMENT",
    }


def _swing_lane(
    heart: dict[str, Any],
    score: dict[str, Any],
    safety_clear: bool,
    safety_blockers: list[str],
) -> dict[str, Any]:
    trajectory = _d(heart.get("trajectory_forecast"))
    plan = _d(trajectory.get("swing_plan"))
    direction = str(trajectory.get("direction") or "").upper()
    current = _f(score.get("current_price"))
    blockers = list(safety_blockers)

    if not trajectory:
        blockers.append("missing_trajectory")
    if not bool(trajectory.get("should_enter_paper_swing")):
        blockers.extend(str(x) for x in (trajectory.get("blockers") or []))
        blockers.append("trajectory_not_ready")
    if _f(trajectory.get("direction_edge")) < 12.0:
        blockers.append("direction_edge_below_12")
    if _f(trajectory.get("trajectory_score")) < 62.0:
        blockers.append("trajectory_score_below_62")
    if not _inside(current, _f(plan.get("entry_low")), _f(plan.get("entry_high"))):
        blockers.append("price_outside_swing_band")

    max_hold = int(trajectory.get("max_hold_minutes") or plan.get("max_hold_minutes") or 720)
    expected_math = choose_target_for_min_net_rr(
        side=direction,
        entry=current,
        stop=_f(plan.get("structural_stop")),
        targets=[
            ("TARGET1", _f(plan.get("target1"))),
            ("TARGET2", _f(plan.get("target2"))),
            ("TARGET3", _f(plan.get("target3"))),
        ],
        expected_hold_hours=max(4.0, min(48.0, max_hold / 60.0)),
        min_net_rr=2.6,
    ) if current > 0 and _f(plan.get("structural_stop")) > 0 else {"accepted": False}
    if not expected_math.get("accepted"):
        blockers.append("net_rr_below_2_6")
    chosen = _d(expected_math.get("chosen_target"))

    blockers = list(dict.fromkeys(blockers))
    return {
        "lane": "SWING_PAPER",
        "paper_only": True,
        "experimental": True,
        "eligible": safety_clear and not blockers,
        "direction": direction,
        "trajectory_score": trajectory.get("trajectory_score"),
        "direction_edge": trajectory.get("direction_edge"),
        "horizon": trajectory.get("horizon"),
        "expected_ranges": trajectory.get("expected_ranges"),
        "entry_low": plan.get("entry_low"),
        "entry_high": plan.get("entry_high"),
        "stop_loss": plan.get("structural_stop"),
        "target_name": chosen.get("name"),
        "target_price": chosen.get("price"),
        "target_zone_low": plan.get("target_zone_low"),
        "target_zone_high": plan.get("target_zone_high"),
        "execution_math": expected_math,
        "risk_budget_pct": 0.5,
        "max_leverage": 2,
        "max_hold_minutes": max_hold,
        "blockers": blockers,
        "reason": "Trayectoria 4h-48h emitida por el mismo Heart con stop estructural y tamaño reducido.",
        "source": "HEART_TRAJECTORY",
    }


def build_execution_contract(
    *,
    heart: dict[str, Any],
    score: dict[str, Any],
    prediction: dict[str, Any],
) -> dict[str, Any]:
    safety_clear, safety_blockers = _hard_safety_clear(heart, prediction)
    tactical = _tactical_lane(heart, score)
    aggressive = _aggressive_lane(heart, score, prediction, safety_clear, safety_blockers)
    swing = _swing_lane(heart, score, safety_clear, safety_blockers)

    # Tactical always has priority. Experimental lanes only exist so PAPER can
    # learn; they never overwrite the user's primary action.
    if tactical["eligible"]:
        permitted_lane = "TACTICAL"
    elif aggressive["eligible"]:
        permitted_lane = "AGGRESSIVE_PAPER"
    elif swing["eligible"]:
        permitted_lane = "SWING_PAPER"
    else:
        permitted_lane = None

    trajectory = _d(heart.get("trajectory_forecast"))
    decision = _d(heart.get("action_decision"))
    use_trajectory_forecast = (
        not tactical["eligible"]
        and _f(trajectory.get("trajectory_score")) >= 62.0
        and _f(trajectory.get("direction_edge")) >= 12.0
    )
    if use_trajectory_forecast:
        forecast = {
            "direction": trajectory.get("direction"),
            "horizon": trajectory.get("horizon"),
            "target_zone_low": _d(trajectory.get("swing_plan")).get("target_zone_low"),
            "target_zone_high": _d(trajectory.get("swing_plan")).get("target_zone_high"),
            "direction_score": trajectory.get("trajectory_score"),
            "direction_edge": trajectory.get("direction_edge"),
            "source": "TRAJECTORY_4H_48H",
        }
    else:
        plan = _d(heart.get("plan"))
        forecast = {
            "direction": heart.get("direction"),
            "horizon": f"{score.get('expected_duration_min_minutes') or 0}-{score.get('expected_duration_max_minutes') or 0}m",
            "target_zone_low": plan.get("tp1"),
            "target_zone_high": plan.get("tp3"),
            "direction_score": heart.get("ignition", {}).get("score") if isinstance(heart.get("ignition"), dict) else None,
            "direction_edge": None,
            "source": "TACTICAL_HEART",
        }

    return {
        "version": VERSION,
        "single_source_of_truth": True,
        "primary_action": decision.get("action"),
        "primary_direction": decision.get("direction") or heart.get("direction"),
        "permitted_paper_lane": permitted_lane,
        "priority": ["TACTICAL", "AGGRESSIVE_PAPER", "SWING_PAPER"],
        "hard_safety_clear": safety_clear,
        "hard_safety_blockers": safety_blockers,
        "forecast": forecast,
        "lanes": {
            "tactical": tactical,
            "aggressive_paper": aggressive,
            "swing_paper": swing,
        },
        "rule": "Scanner, coin analysis and PAPER consume this same Heart contract. Executors may reject a stale fill but cannot invent a new direction or strategy.",
    }


async def finalize_unified_contract_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
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
    lanes = {"TACTICAL": 0, "AGGRESSIVE_PAPER": 0, "SWING_PAPER": 0, "NONE": 0}
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
        }
        contract = build_execution_contract(heart=heart, score=score, prediction=prediction)
        heart["version"] = HEART_VERSION
        heart["execution_contract"] = contract
        heart["primary_prediction"] = contract.get("forecast")
        heart["single_source_of_truth"] = True
        reason["explodex_heart"] = heart
        if prediction:
            prediction["explodex_heart"] = heart
            reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals SET reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {"signal_id": row["signal_id"], "reason": json.dumps(reason)})
        lane = str(contract.get("permitted_paper_lane") or "NONE")
        lanes[lane] = lanes.get(lane, 0) + 1
        updated += 1

    await db.commit()
    return {
        "version": VERSION,
        "heart_version": HEART_VERSION,
        "seen": len(rows),
        "updated": updated,
        "permitted_lanes": lanes,
        "single_source_of_truth": True,
    }
