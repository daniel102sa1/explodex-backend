from __future__ import annotations

from typing import Any

from app.services.execution_math import choose_target_for_min_net_rr

VERSION = "stop_survival_engine_v1"


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _targets_for_lane(heart: dict[str, Any], lane_name: str, lane: dict[str, Any]) -> list[tuple[str, float]]:
    lane_name = str(lane_name or "").upper()
    if lane_name == "SWING_PAPER":
        plan = _d(_d(heart.get("trajectory_forecast")).get("swing_plan"))
        return [
            ("TARGET1", _f(plan.get("target1"))),
            ("TARGET2", _f(plan.get("target2"))),
            ("TARGET3", _f(plan.get("target3"))),
        ]
    plan = _d(heart.get("plan"))
    return [
        ("TP1", _f(lane.get("tp1"), _f(plan.get("tp1")))),
        ("TP2", _f(lane.get("tp2"), _f(plan.get("tp2")))),
        ("TP3", _f(lane.get("tp3"), _f(plan.get("tp3")))),
    ]


def build_stop_survival_plan(
    *,
    heart: dict[str, Any],
    lane_name: str,
    lane: dict[str, Any],
    entry: float,
) -> dict[str, Any]:
    """Build a pre-entry anti-stop-hunt plan for PAPER.

    The original Heart stop becomes a *soft structural invalidation*. A farther
    hard stop is fixed before entry and used for position sizing. A wick through
    the soft level does not close the trade by itself; a candle close beyond the
    soft level confirms invalidation, while the hard stop always exits instantly.

    This never widens a stop after entry and never increases account risk: the
    position size is calculated from the farther hard stop.
    """
    lane_name = str(lane_name or "").upper()
    lane = _d(lane)
    direction = str(lane.get("direction") or "").upper()
    entry = _f(entry)
    soft_stop = _f(lane.get("stop_loss"))
    if direction not in {"LONG", "SHORT"} or entry <= 0 or soft_stop <= 0:
        return {
            "version": VERSION,
            "enabled": False,
            "reason": "invalid_geometry",
            "creates_entry": False,
            "changes_direction": False,
        }
    if (direction == "LONG" and soft_stop >= entry) or (direction == "SHORT" and soft_stop <= entry):
        return {
            "version": VERSION,
            "enabled": False,
            "reason": "invalid_stop_side",
            "creates_entry": False,
            "changes_direction": False,
        }

    htf = _d(heart.get("higher_timeframe_context"))
    frame4 = _d(_d(htf.get("frames")).get("4h"))
    robust4 = max(_f(frame4.get("robust_bar_range_pct")), _f(frame4.get("atr_pct")), 0.35)

    if lane_name == "SWING_PAPER":
        buffer_pct = max(0.25, min(1.50, robust4 * 0.20))
        max_total_stop_pct = 7.0
        confirmation_minutes = 15
        min_net_rr = 2.6
        hold_hours = max(4.0, min(48.0, _f(lane.get("max_hold_minutes"), 720.0) / 60.0))
    elif lane_name == "AGGRESSIVE_PAPER":
        buffer_pct = max(0.20, min(1.20, robust4 * 0.28))
        max_total_stop_pct = 4.5
        confirmation_minutes = 5
        min_net_rr = 2.8
        hold_hours = 2.0
    else:
        buffer_pct = max(0.15, min(1.00, robust4 * 0.22))
        max_total_stop_pct = 4.0
        confirmation_minutes = 5
        min_net_rr = 2.4
        hold_hours = max(0.5, min(6.0, _f(lane.get("max_hold_minutes"), 120.0) / 60.0))

    # Elliott invalidation may identify a more meaningful structural boundary.
    elliott = _d(heart.get("elliott_structure")) or _d(_d(heart.get("execution_contract")).get("elliott_structure"))
    best = _d(elliott.get("best"))
    elliott_score = _f(best.get("score"))
    elliott_direction = str(best.get("direction") or "").upper()
    elliott_invalidation = _f(best.get("invalidation"))
    reference = soft_stop
    elliott_used = False
    if elliott_score >= 72 and elliott_direction == direction and elliott_invalidation > 0:
        if direction == "LONG" and elliott_invalidation < reference:
            reference = elliott_invalidation
            elliott_used = True
        elif direction == "SHORT" and elliott_invalidation > reference:
            reference = elliott_invalidation
            elliott_used = True

    buffer_distance = entry * buffer_pct / 100.0
    proposed_hard = reference - buffer_distance if direction == "LONG" else reference + buffer_distance
    max_distance = entry * max_total_stop_pct / 100.0
    cap_stop = entry - max_distance if direction == "LONG" else entry + max_distance
    if direction == "LONG":
        hard_stop = max(proposed_hard, cap_stop)
    else:
        hard_stop = min(proposed_hard, cap_stop)

    # A hard stop must actually sit farther away than the soft invalidation.
    if (direction == "LONG" and hard_stop >= soft_stop) or (direction == "SHORT" and hard_stop <= soft_stop):
        return {
            "version": VERSION,
            "enabled": False,
            "reason": "no_safe_extra_room",
            "creates_entry": False,
            "changes_direction": False,
        }

    targets = [(name, price) for name, price in _targets_for_lane(heart, lane_name, lane) if price > 0]
    target_math = choose_target_for_min_net_rr(
        side=direction,
        entry=entry,
        stop=hard_stop,
        targets=targets,
        expected_hold_hours=hold_hours,
        min_net_rr=min_net_rr,
    ) if targets else {"accepted": False, "reason": "no_targets"}

    if not target_math.get("accepted"):
        # Do not gain stop tolerance by silently destroying expectancy. If the
        # farther stop makes all targets unattractive, keep the original plan.
        return {
            "version": VERSION,
            "enabled": False,
            "reason": "survival_stop_breaks_min_net_rr",
            "soft_invalidation_stop": round(soft_stop, 12),
            "proposed_hard_stop": round(hard_stop, 12),
            "buffer_pct": round(buffer_pct, 4),
            "execution_math": target_math,
            "creates_entry": False,
            "changes_direction": False,
        }

    chosen = _d(target_math.get("chosen_target"))
    soft_distance_pct = abs(entry - soft_stop) / entry * 100.0
    hard_distance_pct = abs(entry - hard_stop) / entry * 100.0
    return {
        "version": VERSION,
        "enabled": True,
        "mode": "CLOSE_CONFIRMATION_WITH_HARD_STOP",
        "direction": direction,
        "soft_invalidation_stop": round(soft_stop, 12),
        "hard_stop": round(hard_stop, 12),
        "soft_stop_distance_pct": round(soft_distance_pct, 4),
        "hard_stop_distance_pct": round(hard_distance_pct, 4),
        "extra_room_pct": round(max(0.0, hard_distance_pct - soft_distance_pct), 4),
        "confirmation_minutes": confirmation_minutes,
        "buffer_pct": round(buffer_pct, 4),
        "elliott_invalidation_used": elliott_used,
        "elliott_invalidation": round(elliott_invalidation, 12) if elliott_invalidation > 0 else None,
        "elliott_score": round(elliott_score, 1) if elliott_score > 0 else None,
        "target_name": chosen.get("name"),
        "target_price": chosen.get("price"),
        "execution_math": target_math,
        "hard_stop_fixed_before_entry": True,
        "widen_after_entry": False,
        "size_from_hard_stop": True,
        "creates_entry": False,
        "changes_direction": False,
        "rule": "A wick through soft invalidation is tolerated; candle close beyond it confirms exit, or hard stop exits immediately.",
    }
