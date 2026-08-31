from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.execution_math import choose_target_for_min_net_rr

VERSION = "paper_swing_trajectory_v2_expectancy"
RISK_MULTIPLIER = 0.50
MAX_LEVERAGE = 2
MAX_OPEN_SWING = 1
MIN_NET_RR = 2.6


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


def swing_candidate_ok(*, signal_state: str, trajectory: dict[str, Any]) -> tuple[bool, list[str]]:
    blockers = list(trajectory.get("blockers") or [])
    if not bool(trajectory.get("should_enter_paper_swing")):
        blockers.append("trajectory_not_ready")
    if str(signal_state or "").upper() == "NO_TRADE" and _f(trajectory.get("trajectory_score")) < 72.0:
        blockers.append("no_trade_requires_stronger_trajectory")
    if _f(trajectory.get("direction_edge")) < 12.0:
        blockers.append("direction_edge_too_small")
    return not blockers, list(dict.fromkeys(blockers))


async def open_swing_trajectory_position(
    db: AsyncSession,
    *,
    normal_opened: int,
    aggressive_opened: int,
    defensive: bool,
) -> dict[str, Any]:
    if defensive:
        return {"version": VERSION, "opened": 0, "reason": "disabled_in_defensive_mode"}
    if normal_opened > 0 or aggressive_opened > 0:
        return {"version": VERSION, "opened": 0, "reason": "shorter_horizon_trade_opened_this_cycle"}

    open_total = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one() or 0)
    if open_total >= base.MAX_OPEN_POSITIONS:
        return {"version": VERSION, "opened": 0, "reason": "max_open_positions"}

    open_swing = int((await db.execute(text("""
        SELECT COUNT(*) FROM paper_positions
        WHERE status='OPEN' AND metadata->>'strategy_mode'='SWING_TRAJECTORY_PAPER'
    """))).scalar_one() or 0)
    if open_swing >= MAX_OPEN_SWING:
        return {"version": VERSION, "opened": 0, "reason": "max_swing_positions"}

    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)

    rows = (await db.execute(text("""
        SELECT DISTINCT ON (s.symbol_id)
               s.id::text AS signal_id, sy.symbol, s.created_at, s.state, s.setup_score,
               s.risk_score, s.direction, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.is_active=TRUE
          AND s.created_at >= NOW() - INTERVAL '30 minutes'
          AND NOT EXISTS (
              SELECT 1 FROM paper_positions pp
              WHERE pp.status='OPEN' AND pp.symbol=sy.symbol
          )
          AND NOT EXISTS (
              SELECT 1 FROM paper_positions used
              WHERE used.signal_id=s.id
          )
        ORDER BY s.symbol_id, s.created_at DESC
    """))).mappings().all()

    ranked: list[tuple[float, float, dict[str, Any], dict[str, Any]]] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        trajectory = _d(heart.get("trajectory_forecast"))
        if not trajectory:
            reject("missing_trajectory_forecast")
            continue
        ok, why = swing_candidate_ok(signal_state=str(row.get("state") or ""), trajectory=trajectory)
        if not ok:
            for item in why:
                reject(item)
            continue
        ranked.append((_f(trajectory.get("trajectory_score")), _f(trajectory.get("direction_edge")), row, {"trajectory": trajectory}))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked:
        return {"version": VERSION, "opened": 0, "reason": "no_swing_trajectory_candidate", "signals_checked": len(rows), "rejected": rejected}

    _, _, row, ctx = ranked[0]
    trajectory = ctx["trajectory"]
    plan = _d(trajectory.get("swing_plan"))
    side = str(trajectory.get("direction") or "").upper()
    entry_low, entry_high = _f(plan.get("entry_low")), _f(plan.get("entry_high"))
    stop = _f(plan.get("structural_stop"))
    fill = await base._latest_price(str(row["symbol"]))
    lo, hi = min(entry_low, entry_high), max(entry_low, entry_high)

    if min(fill, lo, hi, stop) <= 0:
        return {"version": VERSION, "opened": 0, "reason": "invalid_swing_geometry"}
    if not (lo <= fill <= hi):
        return {"version": VERSION, "opened": 0, "reason": "swing_price_outside_entry_band"}

    max_hold_minutes = int(trajectory.get("max_hold_minutes") or plan.get("max_hold_minutes") or 720)
    expected_hold_hours = max(4.0, min(48.0, max_hold_minutes / 60.0))
    target_choice = choose_target_for_min_net_rr(
        side=side,
        entry=fill,
        stop=stop,
        targets=[
            ("TARGET1", _f(plan.get("target1"))),
            ("TARGET2", _f(plan.get("target2"))),
            ("TARGET3", _f(plan.get("target3"))),
        ],
        expected_hold_hours=expected_hold_hours,
        min_net_rr=MIN_NET_RR,
    )
    if not target_choice.get("accepted"):
        return {"version": VERSION, "opened": 0, "reason": "swing_expectancy_rejected", "execution_math": target_choice}
    chosen = _d(target_choice.get("chosen_target"))
    target = _f(chosen.get("price"))
    if side == "LONG" and not (stop < fill < target):
        return {"version": VERSION, "opened": 0, "reason": "invalid_swing_long_geometry"}
    if side == "SHORT" and not (target < fill < stop):
        return {"version": VERSION, "opened": 0, "reason": "invalid_swing_short_geometry"}

    sizing = base.size_position(balance, fill, stop, MAX_LEVERAGE)
    for key in ("quantity", "notional", "margin", "risk_usdt"):
        sizing[key] = round(_f(sizing.get(key)) * RISK_MULTIPLIER, 10)
    if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
        return {"version": VERSION, "opened": 0, "reason": "swing_position_size_zero"}

    metadata = {
        "execution_version": VERSION,
        "strategy_mode": "SWING_TRAJECTORY_PAPER",
        "experimental": True,
        "trajectory_direction": side,
        "trajectory_score": trajectory.get("trajectory_score"),
        "direction_edge": trajectory.get("direction_edge"),
        "horizon": trajectory.get("horizon"),
        "max_hold_minutes": max_hold_minutes,
        "structural_stop": stop,
        "stop_distance_pct": plan.get("stop_distance_pct"),
        "execution_target_name": chosen.get("name"),
        "execution_target_price": target,
        "execution_math": target_choice,
        "expected_ranges": trajectory.get("expected_ranges"),
        "risk_multiplier": RISK_MULTIPLIER,
        "max_leverage": MAX_LEVERAGE,
        "actual_stop_risk_usdt": sizing.get("risk_usdt"),
        "risk_budget_usdt": sizing.get("risk_budget_usdt"),
        "stop_widening_after_entry": False,
        "purpose": "Test 4h-48h trajectory with structural stop, horizon-matched target and positive net R/R.",
    }

    result = await db.execute(text("""
        INSERT INTO paper_positions (
            signal_id, symbol, side, grade, fingerprint_score, leverage, entry_price, stop_loss,
            take_profit, quantity, notional, margin_used, risk_usdt, opened_at, metadata
        ) VALUES (
            CAST(:signal_id AS UUID), :symbol, :side, 'SWING', :score, :leverage,
            :entry, :stop, :target, :quantity, :notional, :margin, :risk_usdt, :opened_at,
            CAST(:metadata AS JSONB)
        ) ON CONFLICT (signal_id) DO NOTHING
    """), {
        "signal_id": row["signal_id"], "symbol": row["symbol"], "side": side,
        "score": _f(trajectory.get("trajectory_score")), "leverage": MAX_LEVERAGE,
        "entry": fill, "stop": stop, "target": target,
        "quantity": sizing["quantity"], "notional": sizing["notional"], "margin": sizing["margin"],
        "risk_usdt": sizing["risk_usdt"], "opened_at": datetime.now(timezone.utc), "metadata": json.dumps(metadata),
    })
    await db.commit()
    if not result.rowcount:
        return {"version": VERSION, "opened": 0, "reason": "duplicate_swing_signal"}

    return {
        "version": VERSION, "opened": 1, "reason": "swing_trajectory_entry",
        "symbol": row["symbol"], "side": side, "entry": fill, "stop": stop,
        "target": target, "target_name": chosen.get("name"), "net_rr": chosen.get("net_rr"),
        "horizon": trajectory.get("horizon"), "max_hold_minutes": max_hold_minutes,
        "trajectory_score": trajectory.get("trajectory_score"), "direction_edge": trajectory.get("direction_edge"),
        "risk_usdt": sizing["risk_usdt"], "leverage": MAX_LEVERAGE, "experimental": True,
    }
