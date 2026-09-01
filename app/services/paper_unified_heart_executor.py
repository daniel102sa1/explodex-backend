from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.risk_conviction_engine import build_risk_conviction
from app.services.trade_thesis import mark_thesis_entered

VERSION = "paper_unified_heart_executor_v4_elliott_conviction"
LANE_PRIORITY = {"TACTICAL": 0, "AGGRESSIVE_PAPER": 1, "SWING_PAPER": 2}

DEFENSIVE_RISK_CAP = 0.25
DEFENSIVE_MAX_NEW_POSITIONS = 1
DEFENSIVE_TACTICAL_MAX_RISK_SCORE = 65.0
DEFENSIVE_SWING_MAX_RISK_SCORE = 60.0
DEFENSIVE_SWING_MIN_SCORE = 68.0
DEFENSIVE_SWING_MIN_EDGE = 16.0


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


def _defensive_lane_check(*, lane_name: str, lane: dict[str, Any], row: dict[str, Any]) -> tuple[bool, str | None]:
    risk_score = _f(row.get("risk_score"), 100.0)
    if lane_name == "AGGRESSIVE_PAPER":
        return False, "defensive_aggressive_disabled"
    if lane_name == "TACTICAL":
        if risk_score > DEFENSIVE_TACTICAL_MAX_RISK_SCORE:
            return False, "defensive_tactical_risk_too_high"
        return True, None
    if lane_name == "SWING_PAPER":
        if risk_score > DEFENSIVE_SWING_MAX_RISK_SCORE:
            return False, "defensive_swing_risk_too_high"
        if _f(lane.get("trajectory_score")) < DEFENSIVE_SWING_MIN_SCORE:
            return False, "defensive_swing_score_below_68"
        if _f(lane.get("direction_edge")) < DEFENSIVE_SWING_MIN_EDGE:
            return False, "defensive_swing_edge_below_16"
        return True, None
    return False, "defensive_unknown_lane"


async def execute_unified_heart_contracts(
    db: AsyncSession,
    *,
    defensive: bool = False,
    risk_multiplier: float = 1.0,
) -> dict[str, Any]:
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one() or 0)
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if defensive:
        slots = min(slots, DEFENSIVE_MAX_NEW_POSITIONS)
    if slots <= 0:
        return {"version": VERSION, "opened": 0, "reason": "max_open_positions", "rejected": {}, "defensive": defensive, "defensive_learning_enabled": defensive}

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

    candidates: list[tuple[int, float, float, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        contract = _d(heart.get("execution_contract"))
        lane_name = str(contract.get("permitted_paper_lane") or "")
        if lane_name not in LANE_PRIORITY:
            reject("heart_no_permitted_lane")
            continue
        lane_key = {"TACTICAL": "tactical", "AGGRESSIVE_PAPER": "aggressive_paper", "SWING_PAPER": "swing_paper"}[lane_name]
        lane = _d(_d(contract.get("lanes")).get(lane_key))
        if not lane.get("eligible"):
            reject(f"{lane_name.lower()}_not_eligible")
            continue
        if defensive:
            allowed, defensive_reason = _defensive_lane_check(lane_name=lane_name, lane=lane, row=row)
            if not allowed:
                reject(defensive_reason or "defensive_rejected")
                continue
        quality = _f(lane.get("trajectory_score")) if lane_name == "SWING_PAPER" else _f(lane.get("ignition_score"), _f(row.get("setup_score")))
        candidates.append((LANE_PRIORITY[lane_name], -quality, _f(row.get("risk_score"), 100.0), row, heart, lane))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    opened_items: list[dict[str, Any]] = []

    for _, _, _, row, heart, lane in candidates:
        if len(opened_items) >= slots:
            break
        symbol = str(row["symbol"])
        lane_name = str(lane.get("lane") or "")
        side = str(lane.get("direction") or "").upper()
        entry_low = _f(lane.get("entry_low"))
        entry_high = _f(lane.get("entry_high"))
        stop = _f(lane.get("stop_loss"))
        target = _f(lane.get("target_price"))
        fill = await base._latest_price(symbol)

        if min(fill, entry_low, entry_high, stop, target) <= 0:
            reject("invalid_contract_geometry")
            continue
        lo, hi = min(entry_low, entry_high), max(entry_low, entry_high)
        if not (lo <= fill <= hi):
            reject("stale_fill_outside_contract_zone")
            continue
        if not _geometry_ok(side, fill, stop, target):
            reject("invalid_live_fill_geometry")
            continue

        contract = _d(heart.get("execution_contract"))
        matrix = _d(contract.get("forecast_matrix")) or _d(heart.get("forecast_matrix"))
        elliott = _d(contract.get("elliott_structure")) or _d(heart.get("elliott_structure"))
        conviction = build_risk_conviction(
            lane_name=lane_name,
            lane=lane,
            setup_score=_f(row.get("setup_score")),
            risk_score=_f(row.get("risk_score"), 100.0),
            forecast_matrix=matrix,
            elliott_structure=elliott,
        )
        conviction_multiplier = max(0.25, min(1.50, _f(conviction.get("risk_budget_multiplier"), 0.25)))

        lane_leverage = int(max(1, min(3, _f(lane.get("max_leverage"), 3.0))))
        sizing = base.size_position(balance, fill, stop, lane_leverage)
        portfolio_multiplier = max(0.0, min(1.0, risk_multiplier))
        if defensive:
            portfolio_multiplier = min(portfolio_multiplier, DEFENSIVE_RISK_CAP)
        scale = conviction_multiplier * portfolio_multiplier
        for key in ("quantity", "notional", "margin", "risk_usdt"):
            sizing[key] = round(_f(sizing.get(key)) * scale, 10)
        if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
            reject("position_size_zero")
            continue

        metadata = {
            "execution_version": VERSION,
            "strategy_mode": lane_name,
            "canonical_source": "UNIFIED_EXPLODEX_HEART",
            "heart_version": heart.get("version"),
            "execution_contract_version": contract.get("version"),
            "contract_lane": lane,
            "primary_prediction": heart.get("primary_prediction"),
            "primary_action": contract.get("primary_action"),
            "risk_conviction": conviction,
            "elliott_structure": elliott,
            "conviction_risk_multiplier": conviction_multiplier,
            "portfolio_risk_multiplier": portfolio_multiplier,
            "target_account_risk_pct_before_portfolio_brakes": conviction.get("target_account_risk_pct_before_portfolio_brakes"),
            "actual_stop_risk_usdt": sizing.get("risk_usdt"),
            "max_hold_minutes": lane.get("max_hold_minutes"),
            "experimental": bool(lane.get("paper_only")),
            "portfolio_mode": "DEFENSIVE_LEARNING" if defensive else "NORMAL",
            "defensive_learning": defensive,
            "executor_cannot_change_direction": True,
            "executor_cannot_upgrade_wait": True,
        }

        result = await db.execute(text("""
            INSERT INTO paper_positions (
                signal_id, symbol, side, grade, fingerprint_score, leverage,
                entry_price, stop_loss, take_profit, quantity, notional,
                margin_used, risk_usdt, opened_at, metadata
            ) VALUES (
                CAST(:signal_id AS UUID), :symbol, :side, :grade, :score, :leverage,
                :entry, :stop, :target, :quantity, :notional,
                :margin, :risk_usdt, :opened_at, CAST(:metadata AS JSONB)
            ) ON CONFLICT (signal_id) DO NOTHING
        """), {
            "signal_id": row["signal_id"], "symbol": symbol, "side": side,
            "grade": "HEART" if lane_name == "TACTICAL" else "EARLY" if lane_name == "AGGRESSIVE_PAPER" else "SWING",
            "score": _f(lane.get("ignition_score"), _f(lane.get("trajectory_score"), _f(row.get("setup_score")))),
            "leverage": lane_leverage, "entry": fill, "stop": stop, "target": target,
            "quantity": sizing["quantity"], "notional": sizing["notional"], "margin": sizing["margin"],
            "risk_usdt": sizing["risk_usdt"], "opened_at": datetime.now(timezone.utc), "metadata": json.dumps(metadata),
        })
        if not result.rowcount:
            reject("duplicate_signal")
            continue

        if lane_name == "TACTICAL":
            await mark_thesis_entered(db, symbol)

        opened_items.append({
            "symbol": symbol, "lane": lane_name, "side": side, "entry": fill, "stop": stop, "target": target,
            "target_name": lane.get("target_name"), "risk_usdt": sizing["risk_usdt"], "leverage": lane_leverage,
            "max_hold_minutes": lane.get("max_hold_minutes"), "defensive_learning": defensive,
            "conviction_score": conviction.get("conviction_score"), "conviction_tier": conviction.get("tier"),
            "conviction_risk_multiplier": conviction_multiplier, "portfolio_risk_multiplier": portfolio_multiplier,
            "elliott": conviction.get("elliott"),
        })

    await db.commit()
    if opened_items:
        reason = "opened_defensive_learning" if defensive else "opened_from_unified_heart"
    else:
        reason = "no_defensive_learning_candidate" if defensive else "no_executable_heart_contract"
    return {
        "version": VERSION, "opened": len(opened_items), "trades": opened_items, "reason": reason,
        "signals_checked": len(rows), "candidates": len(candidates), "rejected": rejected,
        "defensive": defensive, "defensive_learning_enabled": defensive,
        "risk_policy": {
            "base_account_risk_pct": 1.0, "min_conviction_multiplier": 0.25, "max_conviction_multiplier": 1.50,
            "max_target_account_risk_pct": 1.50, "defensive_cap_multiplier": DEFENSIVE_RISK_CAP,
            "aggressive_max_multiplier": 0.50, "swing_max_multiplier": 1.25,
            "elliott_is_bounded_evidence": True,
        },
    }
