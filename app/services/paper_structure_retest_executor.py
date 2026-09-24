from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.execution_math import evaluate_trade_math
from app.services.paper_regime_router import btc_side_risk_multiplier
from app.services.structure_retest_persistence import mark_structure_retest_entered
from app.services.vnext_evaluation import EVALUATION_GENERATION

VERSION = "paper_structure_retest_executor_v1"

NORMAL_RISK_SCALE = 0.35
DEFENSIVE_RISK_SCALE = 0.20
NORMAL_MAX_RISK_SCORE = 60.0
DEFENSIVE_MAX_RISK_SCORE = 45.0
NORMAL_MIN_PATTERN_SCORE = 72.0
DEFENSIVE_MIN_PATTERN_SCORE = 82.0


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


async def execute_structure_retest_contracts(
    db: AsyncSession,
    *,
    defensive: bool = False,
    risk_multiplier: float = 1.0,
    btc_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one() or 0)
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if defensive:
        slots = min(slots, 1)
    if slots <= 0:
        return {"version": VERSION, "opened": 0, "reason": "max_open_positions", "trades": [], "rejected": {}}

    rows = (await db.execute(text("""
        SELECT DISTINCT ON (s.symbol_id)
               s.id::text AS signal_id, sy.symbol, s.created_at, s.setup_score,
               s.risk_score, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.is_active=TRUE
          AND s.created_at >= NOW() - INTERVAL '30 minutes'
          AND NOT EXISTS (
              SELECT 1 FROM paper_positions pp
              WHERE pp.status='OPEN' AND pp.symbol=sy.symbol
          )
          AND NOT EXISTS (
              SELECT 1 FROM paper_positions used WHERE used.signal_id=s.id
          )
        ORDER BY s.symbol_id, s.created_at DESC
    """))).mappings().all()

    rejected: dict[str, int] = {}
    opened: list[dict[str, Any]] = []

    def reject(name: str) -> None:
        rejected[name] = rejected.get(name, 0) + 1

    for raw in rows:
        if len(opened) >= slots:
            break
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        contract = _d(heart.get("execution_contract"))
        if str(contract.get("permitted_paper_lane") or "") != "STRUCTURE_RETEST_PAPER":
            continue

        lane = _d(_d(contract.get("lanes")).get("structure_retest_paper"))
        if not lane.get("eligible"):
            reject("structure_retest_not_eligible")
            continue

        risk_score = _f(row.get("risk_score"), 100.0)
        pattern_score = _f(lane.get("pattern_score"))
        if defensive:
            if risk_score > DEFENSIVE_MAX_RISK_SCORE:
                reject("defensive_structure_risk_above_45")
                continue
            if pattern_score < DEFENSIVE_MIN_PATTERN_SCORE:
                reject("defensive_pattern_score_below_82")
                continue
        else:
            if risk_score > NORMAL_MAX_RISK_SCORE:
                reject("structure_risk_above_60")
                continue
            if pattern_score < NORMAL_MIN_PATTERN_SCORE:
                reject("pattern_score_below_72")
                continue

        symbol = str(row.get("symbol") or "")
        side = str(lane.get("direction") or "").upper()
        low, high = sorted((_f(lane.get("entry_low")), _f(lane.get("entry_high"))))
        stop = _f(lane.get("stop_loss"))
        target = _f(lane.get("target_price"))
        fill = await base._latest_price(symbol)

        if min(fill, low, high, stop, target) <= 0:
            reject("invalid_structure_geometry")
            continue
        if not (low <= fill <= high):
            reject("live_price_outside_frozen_retest_zone")
            continue
        chase_limit = _f(lane.get("chase_limit"))
        if side == "LONG" and chase_limit > 0 and fill > chase_limit:
            reject("structure_retest_no_chase")
            continue
        if side == "SHORT" and chase_limit > 0 and fill < chase_limit:
            reject("structure_retest_no_chase")
            continue
        if not _geometry_ok(side, fill, stop, target):
            reject("invalid_live_structure_geometry")
            continue

        btc_side_multiplier, btc_side_reason = btc_side_risk_multiplier(side, btc_overlay)
        if btc_side_multiplier <= 0:
            reject(btc_side_reason or "btc_direction_block")
            continue

        math = evaluate_trade_math(
            side=side,
            entry=fill,
            stop=stop,
            target=target,
            expected_hold_hours=8.0,
        )
        if not math.get("valid") or _f(math.get("net_rr")) < 2.4:
            reject("live_net_rr_below_2_4")
            continue

        leverage = int(max(1, min(2, _f(lane.get("max_leverage"), 2))))
        sizing = base.size_position(balance, fill, stop, leverage)

        quant = _d(_d(row.get("reason")).get("quant_brain"))
        if not quant:
            reason_bundle = _d(row.get("reason"))
            prediction_bundle = _d(reason_bundle.get("prediction"))
            heart_bundle = _d(reason_bundle.get("explodex_heart")) or _d(prediction_bundle.get("explodex_heart"))
            quant = _d(heart_bundle.get("quant_brain"))
        quant_multiplier = max(0.20, min(1.0, _f(quant.get("risk_multiplier"), 1.0)))

        breadth_alignment = _d(lane.get("market_breadth_alignment"))
        breadth_multiplier = max(0.25, min(1.0, _f(breadth_alignment.get("risk_multiplier"), 1.0)))
        event = _d(lane.get("event_risk"))
        event_multiplier = max(0.0, min(1.0, _f(event.get("risk_multiplier"), 1.0)))
        global_multiplier = max(0.0, min(1.0, risk_multiplier))
        lane_scale = DEFENSIVE_RISK_SCALE if defensive else NORMAL_RISK_SCALE
        scale = lane_scale * breadth_multiplier * event_multiplier * global_multiplier * btc_side_multiplier * quant_multiplier

        quantity = _f(sizing.get("quantity")) * scale
        notional = quantity * fill
        margin = notional / max(1, leverage)
        actual_risk = quantity * abs(fill - stop)
        if quantity <= 0 or margin <= 0 or actual_risk <= 0:
            reject("structure_position_size_zero")
            continue

        metadata = {
            "execution_version": VERSION,
            "evaluation_generation": EVALUATION_GENERATION,
            "strategy_mode": "STRUCTURE_RETEST_PAPER",
            "canonical_source": "UNIFIED_EXPLODEX_HEART",
            "paper_only": True,
            "experimental": True,
            "setup_id": lane.get("setup_id"),
            "pattern_score": pattern_score,
            "phase": lane.get("phase"),
            "breakout_level": lane.get("breakout_level"),
            "retest_price": lane.get("retest_price"),
            "soft_invalidation_level": lane.get("soft_invalidation_level"),
            "soft_invalidation_stop": lane.get("soft_invalidation_level"),
            "structural_stop": stop,
            "hard_stop": stop,
            "initial_hard_stop": stop,
            "profit_lock": {
                "enabled": False,
                "stage": "IMMUTABLE_STRUCTURAL_STOP",
                "tp1": _f(lane.get("tp1"), target),
                "tp2": _f(lane.get("tp2")),
                "tp3": _f(lane.get("tp3")),
                "final_target": target,
                "rule": "NEVER_MOVE_STOP_AFTER_ENTRY",
            },
            "frozen_plan": {
                "entry": fill,
                "side": side,
                "structural_stop": stop,
                "target": target,
                "tp1": _f(lane.get("tp1"), target),
                "tp2": _f(lane.get("tp2")),
                "tp3": _f(lane.get("tp3")),
                "leverage": leverage,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            "stop_survival_enabled": True,
            "stop_survival": {
                "enabled": True,
                "soft_invalidation_stop": lane.get("soft_invalidation_level"),
                "hard_stop": stop,
                "confirmation_minutes": 15,
                "policy": "wick_through_soft_level_allowed_but_close_beyond_structure_invalidates",
            },
            "stop_basis": "retest_swing_plus_atr_buffer",
            "money_loss_does_not_place_stop": True,
            "position_size_calculated_after_stop": True,
            "stop_fixed_before_entry": True,
            "stop_can_widen_after_entry": False,
            "stop_can_tighten_after_entry": False,
            "stop_policy": "IMMUTABLE_STRUCTURAL_STOP",
            "entry_zone_frozen": True,
            "chase_limit": lane.get("chase_limit"),
            "execution_math_live": math,
            "lane_risk_scale": lane_scale,
            "global_risk_multiplier": global_multiplier,
            "breadth_risk_multiplier": breadth_multiplier,
            "event_risk_multiplier": event_multiplier,
            "actual_stop_risk_usdt": round(actual_risk, 8),
            "btc_overlay": btc_overlay or {},
            "btc_side_risk_multiplier": btc_side_multiplier,
            "btc_side_reason": btc_side_reason,
            "quant_brain": quant,
            "quant_risk_multiplier": quant_multiplier,
            "max_hold_minutes": lane.get("max_hold_minutes"),
        }

        result = await db.execute(text("""
            INSERT INTO paper_positions (
                signal_id, symbol, side, grade, fingerprint_score, leverage,
                entry_price, stop_loss, take_profit, quantity, notional,
                margin_used, risk_usdt, opened_at, metadata
            ) VALUES (
                CAST(:signal_id AS UUID), :symbol, :side, 'RETEST', :score, :leverage,
                :entry, :stop, :target, :quantity, :notional,
                :margin, :risk_usdt, :opened_at, CAST(:metadata AS JSONB)
            ) ON CONFLICT (signal_id) DO NOTHING
        """), {
            "signal_id": row["signal_id"],
            "symbol": symbol,
            "side": side,
            "score": pattern_score,
            "leverage": leverage,
            "entry": fill,
            "stop": stop,
            "target": target,
            "quantity": round(quantity, 10),
            "notional": round(notional, 8),
            "margin": round(margin, 8),
            "risk_usdt": round(actual_risk, 8),
            "opened_at": datetime.now(timezone.utc),
            "metadata": json.dumps(metadata),
        })
        if not result.rowcount:
            reject("duplicate_signal")
            continue

        setup_id = str(lane.get("setup_id") or "")
        if setup_id:
            await mark_structure_retest_entered(db, setup_id)

        opened.append({
            "symbol": symbol,
            "lane": "STRUCTURE_RETEST_PAPER",
            "side": side,
            "entry": fill,
            "stop": stop,
            "target": target,
            "risk_usdt": round(actual_risk, 8),
            "leverage": leverage,
            "pattern_score": pattern_score,
            "net_rr": math.get("net_rr"),
            "setup_id": setup_id,
            "stop_basis": "STRUCTURE_NOT_MONEY",
            "btc_side_risk_multiplier": btc_side_multiplier,
            "btc_stress": (btc_overlay or {}).get("stress"),
            "btc_direction": (btc_overlay or {}).get("direction"),
            "quant_risk_multiplier": quant_multiplier,
            "quant_stance": quant.get("stance"),
        })

    await db.commit()
    return {
        "version": VERSION,
        "opened": len(opened),
        "trades": opened,
        "reason": "opened_structure_retest_paper" if opened else "no_structure_retest_fill",
        "signals_checked": len(rows),
        "rejected": rejected,
        "defensive": defensive,
        "risk_policy": {
            "normal_lane_scale": NORMAL_RISK_SCALE,
            "defensive_lane_scale": DEFENSIVE_RISK_SCALE,
            "stop_is_structural": True,
            "size_adapts_to_stop": True,
            "stop_never_widens_after_entry": True,
            "btc_adaptive_risk": True,
            "quant_brain_risk": True,
        },
    }
