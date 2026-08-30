from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base

VERSION = "paper_aggressive_learning_v1"
MIN_IGNITION_SCORE = 76.0
MAX_RISK_SCORE = 40.0
RISK_MULTIPLIER = 0.50
MAX_LEVERAGE = 2
MAX_OPEN_AGGRESSIVE = 1


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


def aggressive_candidate_ok(
    *,
    signal_state: str,
    risk_score: float,
    heart: dict[str, Any],
    thesis: dict[str, Any],
    decision: dict[str, Any],
    ignition: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Reduced-risk PAPER-only early-entry gate.

    It cannot bypass hard safety checks. It only relaxes the timing threshold:
    a well-prepared ARMED/IGNITING setup may be sampled before the canonical
    Heart emits ENTER, so its outcome can be measured against the normal path.
    """
    blockers: list[str] = []
    action = str(decision.get("action") or "").upper()
    ignition_stage = str(ignition.get("stage") or "").upper()
    ignition_score = _f(ignition.get("score"))
    hard_blockers = set(str(x) for x in (ignition.get("blockers") or []))

    if str(signal_state or "").upper() == "NO_TRADE":
        blockers.append("signal_no_trade")
    if action != "ESPERAR":
        blockers.append(f"action_{action.lower() or 'missing'}")
    if not bool(thesis.get("frozen_plan")):
        blockers.append("no_frozen_thesis")
    if str(thesis.get("status") or "").upper() not in {"WAITING_ENTRY", "ENTER_NOW"}:
        blockers.append("thesis_not_waiting")
    if risk_score > MAX_RISK_SCORE:
        blockers.append("risk_too_high")
    if ignition_stage not in {"ARMED", "IGNITING"}:
        blockers.append("ignition_not_armed")
    if ignition_score < MIN_IGNITION_SCORE:
        blockers.append("ignition_below_76")
    if int(ignition.get("supporting_components") or 0) < 3:
        blockers.append("insufficient_supporting_components")

    # These are the hard checks from ignition_engine. They must all remain clear.
    for required in (
        "master_yes",
        "veto_clear",
        "not_chasing",
        "not_invalidated",
        "risk_guard_pass",
        "direction_match",
        "risk_ok",
        "base_state_ok",
    ):
        if required in hard_blockers:
            blockers.append(required)

    return not blockers, blockers


async def open_aggressive_learning_position(
    db: AsyncSession,
    *,
    normal_opened: int,
    defensive: bool,
) -> dict[str, Any]:
    """Open at most one reduced-risk experimental PAPER position.

    This runs only when the canonical path opened nothing in the cycle. It does
    not mark the canonical thesis IN_POSITION because the experiment must remain
    distinct from the user's primary Heart recommendation.
    """
    if defensive:
        return {"version": VERSION, "opened": 0, "reason": "disabled_in_defensive_mode"}
    if normal_opened > 0:
        return {"version": VERSION, "opened": 0, "reason": "canonical_trade_opened_this_cycle"}

    open_total = int((await db.execute(text(
        "SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"
    ))).scalar_one() or 0)
    if open_total >= base.MAX_OPEN_POSITIONS:
        return {"version": VERSION, "opened": 0, "reason": "max_open_positions"}

    open_aggressive = int((await db.execute(text("""
        SELECT COUNT(*) FROM paper_positions
        WHERE status='OPEN' AND metadata->>'strategy_mode'='AGGRESSIVE_EARLY_PAPER'
    """))).scalar_one() or 0)
    if open_aggressive >= MAX_OPEN_AGGRESSIVE:
        return {"version": VERSION, "opened": 0, "reason": "max_aggressive_positions"}

    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)

    rows = (await db.execute(text("""
        SELECT DISTINCT ON (s.symbol_id)
               s.id::text AS signal_id,
               sy.symbol,
               s.created_at,
               s.direction,
               s.state,
               s.setup_score,
               s.risk_score,
               s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.is_active=TRUE
          AND s.created_at >= NOW() - INTERVAL '20 minutes'
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

    ranked: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        thesis = _d(heart.get("thesis"))
        decision = _d(heart.get("action_decision"))
        ignition = _d(heart.get("ignition"))
        ok, why = aggressive_candidate_ok(
            signal_state=str(row.get("state") or ""),
            risk_score=_f(row.get("risk_score"), 100.0),
            heart=heart,
            thesis=thesis,
            decision=decision,
            ignition=ignition,
        )
        if not ok:
            for item in why:
                reject(item)
            continue
        ranked.append((_f(ignition.get("score")), row, {
            "reason": reason,
            "heart": heart,
            "thesis": thesis,
            "decision": decision,
            "ignition": ignition,
        }))

    ranked.sort(key=lambda item: (item[0], _f(item[1].get("setup_score"))), reverse=True)
    if not ranked:
        return {
            "version": VERSION,
            "opened": 0,
            "reason": "no_aggressive_candidate",
            "signals_checked": len(rows),
            "rejected": rejected,
        }

    _, row, ctx = ranked[0]
    heart, thesis, decision, ignition = ctx["heart"], ctx["thesis"], ctx["decision"], ctx["ignition"]
    plan = _d(heart.get("plan"))
    side = str(heart.get("direction") or row.get("direction") or "").upper()
    entry_low = _f(plan.get("entry_low"), _f(thesis.get("entry_low")))
    entry_high = _f(plan.get("entry_high"), _f(thesis.get("entry_high")))
    stop = _f(plan.get("stop_loss"), _f(thesis.get("stop_loss")))
    tp1 = _f(plan.get("tp1"), _f(thesis.get("tp1")))
    fill = await base._latest_price(str(row["symbol"]))
    lo, hi = min(entry_low, entry_high), max(entry_low, entry_high)

    if min(fill, lo, hi, stop, tp1) <= 0:
        return {"version": VERSION, "opened": 0, "reason": "invalid_plan_geometry"}
    if not (lo <= fill <= hi):
        return {"version": VERSION, "opened": 0, "reason": "price_not_in_entry_zone"}
    if side == "LONG" and not (stop < fill < tp1):
        return {"version": VERSION, "opened": 0, "reason": "invalid_long_geometry"}
    if side == "SHORT" and not (tp1 < fill < stop):
        return {"version": VERSION, "opened": 0, "reason": "invalid_short_geometry"}

    sizing = base.size_position(balance, fill, stop, MAX_LEVERAGE)
    for key in ("quantity", "notional", "margin", "risk_usdt"):
        sizing[key] = round(_f(sizing.get(key)) * RISK_MULTIPLIER, 10)
    if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
        return {"version": VERSION, "opened": 0, "reason": "position_size_zero"}

    metadata = {
        "execution_version": VERSION,
        "strategy_mode": "AGGRESSIVE_EARLY_PAPER",
        "experimental": True,
        "canonical_heart_action_at_entry": decision.get("action"),
        "canonical_heart_should_enter": bool(decision.get("should_enter")),
        "ignition_score": ignition.get("score"),
        "ignition_stage": ignition.get("stage"),
        "risk_multiplier": RISK_MULTIPLIER,
        "max_leverage": MAX_LEVERAGE,
        "validation_goal": "Compare early ARMED/IGNITING entries against canonical ENTER outcomes.",
        "does_not_promote_heart": True,
    }

    result = await db.execute(text("""
        INSERT INTO paper_positions (
            signal_id, symbol, side, grade, fingerprint_score, leverage, entry_price, stop_loss,
            take_profit, quantity, notional, margin_used, risk_usdt, opened_at, metadata
        ) VALUES (
            CAST(:signal_id AS UUID), :symbol, :side, 'EARLY', :score, :leverage,
            :entry, :stop, :tp1, :quantity, :notional, :margin, :risk_usdt, :opened_at,
            CAST(:metadata AS JSONB)
        ) ON CONFLICT (signal_id) DO NOTHING
    """), {
        "signal_id": row["signal_id"],
        "symbol": row["symbol"],
        "side": side,
        "score": _f(ignition.get("score")),
        "leverage": MAX_LEVERAGE,
        "entry": fill,
        "stop": stop,
        "tp1": tp1,
        "quantity": sizing["quantity"],
        "notional": sizing["notional"],
        "margin": sizing["margin"],
        "risk_usdt": sizing["risk_usdt"],
        "opened_at": datetime.now(timezone.utc),
        "metadata": json.dumps(metadata),
    })
    await db.commit()
    if not result.rowcount:
        return {"version": VERSION, "opened": 0, "reason": "duplicate_signal"}

    return {
        "version": VERSION,
        "opened": 1,
        "reason": "aggressive_learning_entry",
        "symbol": row["symbol"],
        "side": side,
        "entry": fill,
        "stop": stop,
        "tp1": tp1,
        "ignition_score": ignition.get("score"),
        "risk_usdt": sizing["risk_usdt"],
        "leverage": MAX_LEVERAGE,
        "experimental": True,
    }
