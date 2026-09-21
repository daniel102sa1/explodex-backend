from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.risk_conviction_engine import build_risk_conviction
from app.services.paper_regime_router import btc_side_risk_multiplier
from app.services.stop_survival_engine import build_stop_survival_plan
from app.services.trade_thesis import mark_thesis_entered
from app.services.vnext_evaluation import EVALUATION_GENERATION

VERSION = "paper_unified_heart_executor_v5_stop_survival"
LANE_PRIORITY = {"TACTICAL": 0, "AGGRESSIVE_PAPER": 1, "SWING_PAPER": 2}

DEFENSIVE_RISK_CAP = 0.25
DEFENSIVE_MAX_NEW_POSITIONS = 1
DEFENSIVE_TACTICAL_MAX_RISK_SCORE = 65.0
DEFENSIVE_SWING_MAX_RISK_SCORE = 60.0
DEFENSIVE_SWING_MIN_SCORE = 68.0
DEFENSIVE_SWING_MIN_EDGE = 16.0

# VNext probation exists only so a bad legacy cohort cannot permanently prevent
# the new PAPER generation from collecting any real execution outcomes. It is
# intentionally tiny, one-position-at-a-time, and 1x leverage.
PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP = 0.10
PROBATION_MAX_NEW_POSITIONS = 1
PROBATION_MAX_RISK_SCORE = 48.0
PROBATION_SWING_MIN_SCORE = 70.0
PROBATION_SWING_MIN_EDGE = 18.0


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


def _sarpon_leverage_policy(
    *,
    lane_name: str,
    lane: dict[str, Any],
    heart: dict[str, Any],
    conviction: dict[str, Any],
    defensive: bool,
    btc_overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    """Allow only a small PAPER leverage step-up under full current confluence.

    This never raises the account risk budget; sizing still comes from the hard
    stop and risk multipliers. Higher leverage can only reduce margin required
    to express the already-approved risk size.
    """
    base_cap = int(max(1, min(4, _f(lane.get("max_leverage"), 2.0))))
    monitor = _d(heart.get("chati_sarpon_612_monitor"))
    sarpon = _d(monitor.get("sarpon"))
    classic = _d(sarpon.get("classic"))
    phase_green = str(monitor.get("phase") or "") == "GREEN_CONFIRMATION"
    classic_green = str(classic.get("stage") or "") == "GREEN_CONFIRMATION"
    no_contradictions = not list(monitor.get("contradictions") or [])
    tier = str(conviction.get("tier") or "")
    conviction_high = tier in {"HIGH", "MAX_CONVICTION"}
    btc = _d(btc_overlay)
    btc_safe = str(btc.get("stress") or "NORMAL").upper() not in {"EXTREME", "SHOCK"} and not bool(_d(monitor.get("btc")).get("hard_conflict"))

    eligible = bool(
        not defensive
        and lane_name in {"TACTICAL", "SWING_PAPER"}
        and phase_green
        and classic_green
        and no_contradictions
        and conviction_high
        and btc_safe
    )
    if eligible:
        boosted_cap = min(4, base_cap + 1)
        reason = "full_sarpon_chati_612_confluence"
    else:
        boosted_cap = base_cap
        reason = "base_cap"

    return {
        "eligible": eligible,
        "base_cap": base_cap,
        "selected_leverage": boosted_cap,
        "reason": reason,
        "risk_budget_unchanged": True,
        "requires": {
            "chati_sarpon_612_green": phase_green,
            "sarpon_murphy_nison_green": classic_green,
            "no_contradictions": no_contradictions,
            "conviction_high": conviction_high,
            "btc_safe": btc_safe,
            "not_defensive": not defensive,
        },
    }


def _probation_lane_check(*, lane_name: str, lane: dict[str, Any], row: dict[str, Any]) -> tuple[bool, str | None]:
    """Very small PAPER-only validation lane used while legacy history is halted."""
    risk_score = _f(row.get("risk_score"), 100.0)
    if lane_name == "AGGRESSIVE_PAPER":
        return False, "probation_aggressive_disabled"
    if risk_score > PROBATION_MAX_RISK_SCORE:
        return False, "probation_risk_above_48"
    if lane_name == "TACTICAL":
        return True, None
    if lane_name == "SWING_PAPER":
        if _f(lane.get("trajectory_score")) < PROBATION_SWING_MIN_SCORE:
            return False, "probation_swing_score_below_70"
        if _f(lane.get("direction_edge")) < PROBATION_SWING_MIN_EDGE:
            return False, "probation_swing_edge_below_18"
        return True, None
    return False, "probation_unknown_lane"


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
    btc_overlay: dict[str, Any] | None = None,
    validation_probation: bool = False,
) -> dict[str, Any]:
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one() or 0)
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if defensive:
        slots = min(slots, DEFENSIVE_MAX_NEW_POSITIONS)
    if validation_probation:
        slots = min(slots, PROBATION_MAX_NEW_POSITIONS)
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
        if validation_probation:
            allowed, probation_reason = _probation_lane_check(lane_name=lane_name, lane=lane, row=row)
            if not allowed:
                reject(probation_reason or "probation_rejected")
                continue
        elif defensive:
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
        original_stop = _f(lane.get("stop_loss"))
        original_target = _f(lane.get("target_price"))
        fill = await base._latest_price(symbol)

        if min(fill, entry_low, entry_high, original_stop, original_target) <= 0:
            reject("invalid_contract_geometry")
            continue
        lo, hi = min(entry_low, entry_high), max(entry_low, entry_high)
        if not (lo <= fill <= hi):
            reject("stale_fill_outside_contract_zone")
            continue
        if not _geometry_ok(side, fill, original_stop, original_target):
            reject("invalid_live_fill_geometry")
            continue

        btc_side_multiplier, btc_side_reason = btc_side_risk_multiplier(side, btc_overlay)
        if btc_side_multiplier <= 0:
            reject(btc_side_reason or "btc_direction_block")
            continue

        survival = build_stop_survival_plan(
            heart=heart,
            lane_name=lane_name,
            lane=lane,
            entry=fill,
            btc_context=btc_overlay,
        )
        survival_enabled = bool(survival.get("enabled"))
        hard_stop = _f(survival.get("hard_stop"), original_stop) if survival_enabled else original_stop
        target = _f(survival.get("target_price"), original_target) if survival_enabled else original_target
        if not _geometry_ok(side, fill, hard_stop, target):
            reject("invalid_survival_geometry")
            continue

        contract = _d(heart.get("execution_contract"))
        quant = _d(heart.get("quant_brain")) or _d(contract.get("quant_brain"))
        quant_multiplier = max(0.20, min(1.0, _f(quant.get("risk_multiplier"), 1.0)))
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

        leverage_policy = _sarpon_leverage_policy(
            lane_name=lane_name,
            lane=lane,
            heart=heart,
            conviction=conviction,
            defensive=defensive,
            btc_overlay=btc_overlay,
        )
        lane_leverage = 1 if validation_probation else int(leverage_policy["selected_leverage"])
        sizing = base.size_position(balance, fill, hard_stop, lane_leverage)
        portfolio_multiplier = max(0.0, min(1.0, risk_multiplier))
        if validation_probation:
            portfolio_multiplier = min(portfolio_multiplier, PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP)
        elif defensive:
            portfolio_multiplier = min(portfolio_multiplier, DEFENSIVE_RISK_CAP)
        scale = conviction_multiplier * portfolio_multiplier * btc_side_multiplier * quant_multiplier
        for key in ("quantity", "notional", "margin", "risk_usdt"):
            sizing[key] = round(_f(sizing.get(key)) * scale, 10)
        if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
            reject("position_size_zero")
            continue

        metadata = {
            "execution_version": VERSION,
            "evaluation_generation": EVALUATION_GENERATION,
            "strategy_mode": lane_name,
            "trade_profile": lane.get("trade_profile") or lane_name,
            "planned_horizon": lane.get("horizon"),
            "planned_max_hold_minutes": lane.get("max_hold_minutes"),
            "planned_max_leverage": lane.get("max_leverage"),
            "leverage_policy": leverage_policy,
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
            "btc_overlay": btc_overlay or {},
            "btc_side_risk_multiplier": btc_side_multiplier,
            "btc_side_reason": btc_side_reason,
            "quant_brain": quant,
            "quant_risk_multiplier": quant_multiplier,
            "target_account_risk_pct_before_portfolio_brakes": conviction.get("target_account_risk_pct_before_portfolio_brakes"),
            "actual_stop_risk_usdt": sizing.get("risk_usdt"),
            "stop_survival": survival,
            "soft_invalidation_stop": survival.get("soft_invalidation_stop") if survival_enabled else original_stop,
            "hard_stop": hard_stop,
            "initial_hard_stop": hard_stop,
            "stop_survival_enabled": survival_enabled,
            "profit_lock": {
                "enabled": True,
                "stage": "INITIAL",
                "tp1": _f(lane.get("tp1")),
                "tp2": _f(lane.get("tp2")),
                "tp3": _f(lane.get("tp3")),
                "final_target": target,
                "after_tp1": "MOVE_STOP_TO_BREAKEVEN_PLUS_COST_BUFFER_ON_NEXT_CANDLE",
                "after_tp2": "MOVE_STOP_TO_TP1_ON_NEXT_CANDLE",
                "never_widen_stop": True,
                "same_candle_sequence_is_not_assumed": True,
            },
            "stop_was_fixed_before_entry": True,
            "stop_can_widen_after_entry": False,
            "size_calculated_from_hard_stop": True,
            "max_hold_minutes": lane.get("max_hold_minutes"),
            "horizon_policy_fixed_before_entry": True,
            "leverage_is_margin_tool_not_profit_target": True,
            "experimental": bool(lane.get("paper_only")),
            "portfolio_mode": "VNEXT_PROBATION" if validation_probation else "DEFENSIVE_LEARNING" if defensive else "NORMAL",
            "defensive_learning": defensive,
            "validation_probation": validation_probation,
            "probation_risk_multiplier_cap": PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP if validation_probation else None,
            "probation_forces_1x_leverage": validation_probation,
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
            "leverage": lane_leverage, "entry": fill, "stop": hard_stop, "target": target,
            "quantity": sizing["quantity"], "notional": sizing["notional"], "margin": sizing["margin"],
            "risk_usdt": sizing["risk_usdt"], "opened_at": datetime.now(timezone.utc), "metadata": json.dumps(metadata),
        })
        if not result.rowcount:
            reject("duplicate_signal")
            continue

        if lane_name == "TACTICAL":
            await mark_thesis_entered(db, symbol)

        opened_items.append({
            "symbol": symbol,
            "lane": lane_name,
            "side": side,
            "entry": fill,
            "soft_invalidation_stop": metadata["soft_invalidation_stop"],
            "hard_stop": hard_stop,
            "stop_survival_enabled": survival_enabled,
            "target": target,
            "target_name": survival.get("target_name") if survival_enabled else lane.get("target_name"),
            "risk_usdt": sizing["risk_usdt"],
            "leverage": lane_leverage,
            "leverage_policy": leverage_policy,
            "max_hold_minutes": lane.get("max_hold_minutes"),
            "horizon": lane.get("horizon"),
            "trade_profile": lane.get("trade_profile") or lane_name,
            "defensive_learning": defensive,
            "validation_probation": validation_probation,
            "conviction_score": conviction.get("conviction_score"),
            "conviction_tier": conviction.get("tier"),
            "conviction_risk_multiplier": conviction_multiplier,
            "portfolio_risk_multiplier": portfolio_multiplier,
            "btc_side_risk_multiplier": btc_side_multiplier,
            "btc_stress": (btc_overlay or {}).get("stress"),
            "btc_direction": (btc_overlay or {}).get("direction"),
            "quant_risk_multiplier": quant_multiplier,
            "quant_stance": quant.get("stance"),
            "quant_directional_edge": quant.get("directional_edge"),
            "elliott": conviction.get("elliott"),
        })

    await db.commit()
    if opened_items:
        reason = "opened_vnext_probation" if validation_probation else "opened_defensive_learning" if defensive else "opened_from_unified_heart"
    else:
        reason = "no_vnext_probation_candidate" if validation_probation else "no_defensive_learning_candidate" if defensive else "no_executable_heart_contract"
    return {
        "version": VERSION,
        "opened": len(opened_items),
        "trades": opened_items,
        "reason": reason,
        "signals_checked": len(rows),
        "candidates": len(candidates),
        "rejected": rejected,
        "defensive": defensive,
        "defensive_learning_enabled": defensive,
        "validation_probation": validation_probation,
        "risk_policy": {
            "base_account_risk_pct": 1.0,
            "min_conviction_multiplier": 0.25,
            "max_conviction_multiplier": 1.50,
            "max_target_account_risk_pct": 1.50,
            "defensive_cap_multiplier": DEFENSIVE_RISK_CAP,
            "probation_cap_multiplier": PROBATION_PORTFOLIO_RISK_MULTIPLIER_CAP,
            "probation_max_new_positions": PROBATION_MAX_NEW_POSITIONS,
            "probation_forces_1x_leverage": True,
            "aggressive_max_multiplier": 0.50,
            "swing_max_multiplier": 1.25,
            "elliott_is_bounded_evidence": True,
            "stop_survival_sizes_from_hard_stop": True,
            "stop_never_widens_after_entry": True,
            "btc_adaptive_risk": True,
            "quant_brain_risk": True,
        },
    }
