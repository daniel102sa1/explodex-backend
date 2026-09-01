from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.risk_conviction_engine import build_risk_conviction
from app.services.stop_survival_engine import build_stop_survival_plan

VERSION = "paper_pre_event_executor_v1"
MAX_NEW = 1


def _d(value: Any) -> dict[str, Any]:
    if isinstance(value, dict): return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value); return parsed if isinstance(parsed, dict) else {}
        except Exception: return {}
    return {}


def _f(value: Any, default: float = 0.0) -> float:
    try: return float(value) if value not in (None, "") else default
    except (TypeError, ValueError): return default


def _geometry_ok(side: str, entry: float, stop: float, target: float) -> bool:
    return (side == "LONG" and stop < entry < target) or (side == "SHORT" and target < entry < stop)


async def execute_pre_event_contracts(db: AsyncSession, *, defensive: bool, risk_multiplier: float) -> dict[str, Any]:
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one() or 0)
    if open_count >= base.MAX_OPEN_POSITIONS:
        return {"version": VERSION, "opened": 0, "reason": "max_open_positions", "rejected": {}}

    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = _f(account["cash_balance"] if account else base.STARTING_BALANCE)
    rows = (await db.execute(text("""
        SELECT DISTINCT ON (s.symbol_id)
               s.id::text AS signal_id, sy.symbol, s.setup_score, s.risk_score, s.reason
        FROM signals s JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.is_active=TRUE AND s.created_at >= NOW() - INTERVAL '30 minutes'
          AND NOT EXISTS (SELECT 1 FROM paper_positions pp WHERE pp.status='OPEN' AND pp.symbol=sy.symbol)
          AND NOT EXISTS (SELECT 1 FROM paper_positions used WHERE used.signal_id=s.id)
        ORDER BY s.symbol_id, s.created_at DESC
    """))).mappings().all()

    rejected: dict[str, int] = {}
    def reject(name: str) -> None: rejected[name] = rejected.get(name, 0) + 1
    candidates: list[tuple[float, float, dict[str, Any], dict[str, Any], dict[str, Any]]] = []

    for raw in rows:
        row = dict(raw); reason = _d(row.get("reason")); prediction = _d(reason.get("prediction")); heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart")); contract = _d(heart.get("execution_contract"))
        if str(contract.get("permitted_paper_lane") or "") != "PRE_EVENT_PAPER":
            reject("not_pre_event_lane"); continue
        lane = _d(_d(contract.get("lanes")).get("pre_event_paper"))
        if not lane.get("eligible"):
            reject("pre_event_not_eligible"); continue
        prep = _f(lane.get("preparation_score")); support = int(lane.get("supporting_signals") or 0); risk = _f(row.get("risk_score"),100.0)
        if defensive and (prep < 78 or support < 5 or risk > 45):
            reject("defensive_pre_event_not_strong_enough"); continue
        candidates.append((-prep, risk, row, heart, lane))

    candidates.sort(key=lambda x: (x[0], x[1]))
    opened: list[dict[str, Any]] = []
    for _, _, row, heart, lane in candidates[:MAX_NEW]:
        symbol = str(row["symbol"]); side = str(lane.get("direction") or "").upper(); fill = await base._latest_price(symbol)
        low, high = _f(lane.get("entry_low")), _f(lane.get("entry_high")); stop = _f(lane.get("stop_loss")); target = _f(lane.get("target_price"))
        if min(fill, low, high, stop, target) <= 0: reject("invalid_geometry"); continue
        if not min(low, high) <= fill <= max(low, high): reject("stale_fill_outside_pre_event_band"); continue
        if not _geometry_ok(side, fill, stop, target): reject("invalid_live_geometry"); continue

        survival = build_stop_survival_plan(heart=heart, lane_name="PRE_EVENT_PAPER", lane=lane, entry=fill)
        hard_stop = _f(survival.get("hard_stop"), stop) if survival.get("enabled") else stop
        live_target = _f(survival.get("target_price"), target) if survival.get("enabled") else target
        if not _geometry_ok(side, fill, hard_stop, live_target): reject("invalid_survival_geometry"); continue

        contract = _d(heart.get("execution_contract")); matrix = _d(contract.get("forecast_matrix")) or _d(heart.get("forecast_matrix")); elliott = _d(contract.get("elliott_structure")) or _d(heart.get("elliott_structure"))
        conviction = build_risk_conviction(lane_name="PRE_EVENT_PAPER", lane=lane, setup_score=_f(row.get("setup_score")), risk_score=_f(row.get("risk_score"),100.0), forecast_matrix=matrix, elliott_structure=elliott)
        conv_mult = min(0.25, max(0.05, _f(conviction.get("risk_budget_multiplier"),0.05)))
        portfolio_mult = max(0.0, min(1.0, risk_multiplier)); portfolio_mult = min(portfolio_mult, 0.25) if defensive else portfolio_mult
        leverage = int(max(1, min(2, _f(lane.get("max_leverage"),2.0))))
        sizing = base.size_position(balance, fill, hard_stop, leverage); scale = conv_mult * portfolio_mult
        for key in ("quantity","notional","margin","risk_usdt"): sizing[key] = round(_f(sizing.get(key))*scale,10)
        if sizing["quantity"] <= 0: reject("position_size_zero"); continue

        metadata = {
            "execution_version": VERSION, "strategy_mode": "PRE_EVENT_PAPER", "canonical_source": "UNIFIED_EXPLODEX_HEART",
            "contract_lane": lane, "pre_event_prediction": contract.get("pre_event_prediction"), "event_risk": contract.get("event_risk"),
            "risk_conviction": conviction, "stop_survival": survival, "soft_invalidation_stop": survival.get("soft_invalidation_stop") if survival.get("enabled") else stop,
            "hard_stop": hard_stop, "max_hold_minutes": lane.get("max_hold_minutes"), "experimental": True,
            "portfolio_mode": "DEFENSIVE_LEARNING" if defensive else "NORMAL", "pre_event_risk_cap": 0.25,
            "executor_cannot_change_direction": True, "executor_cannot_create_lane": True,
        }
        result = await db.execute(text("""
            INSERT INTO paper_positions (signal_id,symbol,side,grade,fingerprint_score,leverage,entry_price,stop_loss,take_profit,quantity,notional,margin_used,risk_usdt,opened_at,metadata)
            VALUES (CAST(:signal_id AS UUID),:symbol,:side,'PRE_EVENT',:score,:leverage,:entry,:stop,:target,:quantity,:notional,:margin,:risk_usdt,:opened_at,CAST(:metadata AS JSONB))
            ON CONFLICT (signal_id) DO NOTHING
        """), {"signal_id":row["signal_id"],"symbol":symbol,"side":side,"score":_f(lane.get("preparation_score")),"leverage":leverage,"entry":fill,"stop":hard_stop,"target":live_target,"quantity":sizing["quantity"],"notional":sizing["notional"],"margin":sizing["margin"],"risk_usdt":sizing["risk_usdt"],"opened_at":datetime.now(timezone.utc),"metadata":json.dumps(metadata)})
        if not result.rowcount: reject("duplicate_signal"); continue
        opened.append({"symbol":symbol,"lane":"PRE_EVENT_PAPER","side":side,"entry":fill,"hard_stop":hard_stop,"target":live_target,"risk_usdt":sizing["risk_usdt"],"preparation_score":lane.get("preparation_score"),"pre_event_type":lane.get("pre_event_type"),"defensive":defensive})

    await db.commit()
    return {"version":VERSION,"opened":len(opened),"trades":opened,"reason":"opened_pre_event_paper" if opened else "no_pre_event_fill","signals_checked":len(rows),"candidates":len(candidates),"rejected":rejected,"paper_only":True}
