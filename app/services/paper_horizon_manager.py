from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.adaptive_profit_trail import build_adaptive_profit_trail
from app.services.binance import binance_client

VERSION = "paper_horizon_manager_v5_adaptive_profit_trailing"
DEFAULT_MAX_HOLD_MINUTES = 120
PROFIT_LOCK_COST_BUFFER_RATE = 0.0018
PRE_TP1_ARM_PROGRESS = 0.85
PRE_TP1_RETRACE_FRACTION = 0.18

STRATEGY_MAX_HOLD_FALLBACK = {
    "MICRO_SCALP": 35,
    "RANGE_MICRO": 60,
    "AGGRESSIVE_PAPER": 120,
    "TACTICAL": 180,
    "TREND_PREMOVE": 240,
    "PRE_EVENT_PAPER": 360,
    "STRUCTURE_RETEST_PAPER": 360,
    "SWING_PAPER": 720,
    "SWING_TRAJECTORY_PAPER": 720,
}


def planned_max_hold_minutes(metadata: dict[str, Any]) -> int:
    """Resolve time horizon from the plan instead of forcing every trade into 2h.

    Explicit plan metadata always wins. Older PAPER rows may lack it, so swing
    strategies get a horizon-aware fallback rather than the generic 120-minute
    timeout that previously cut them too early.
    """
    explicit = metadata.get("max_hold_minutes")
    if explicit not in {None, ""}:
        return max(30, min(int(_f(explicit, DEFAULT_MAX_HOLD_MINUTES)), 4320))

    horizon = str(
        metadata.get("planned_horizon")
        or metadata.get("horizon")
        or _d(metadata.get("contract_lane")).get("horizon")
        or ""
    ).lower()
    if "24-48" in horizon or "24–48" in horizon:
        return 2880
    if "8-24" in horizon or "8–24" in horizon:
        return 1440
    if "4-12" in horizon or "4–12" in horizon:
        return 720

    strategy = str(metadata.get("strategy_mode") or "").upper()
    return STRATEGY_MAX_HOLD_FALLBACK.get(strategy, DEFAULT_MAX_HOLD_MINUTES)


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
        return float(value)
    except (TypeError, ValueError):
        return default


def _touched(side: str, high: float, low: float, level: float) -> bool:
    if level <= 0:
        return False
    return high >= level if side == "LONG" else low <= level


def _target_beyond(side: str, final_target: float, milestone: float) -> bool:
    if final_target <= 0 or milestone <= 0:
        return False
    return final_target > milestone if side == "LONG" else final_target < milestone


def _tighten_only(side: str, current_stop: float, candidate: float) -> float:
    if candidate <= 0:
        return current_stop
    if side == "LONG":
        return max(current_stop, candidate)
    if side == "SHORT":
        return min(current_stop, candidate)
    return current_stop


def breakeven_profit_lock_stop(*, side: str, entry: float, tp1: float, current_stop: float) -> float:
    """After TP1, protect the remainder around breakeven plus estimated costs.

    The new stop is activated only after the TP1 candle completes. It never
    widens the existing stop and never assumes an intrabar sequence.
    """
    side = str(side or "").upper()
    if min(entry, tp1, current_stop) <= 0 or side not in {"LONG", "SHORT"}:
        return current_stop
    epsilon = entry * 0.00005
    if side == "LONG":
        if tp1 <= entry:
            return current_stop
        candidate = min(entry * (1.0 + PROFIT_LOCK_COST_BUFFER_RATE), tp1 - epsilon)
        candidate = max(entry, candidate)
    else:
        if tp1 >= entry:
            return current_stop
        candidate = max(entry * (1.0 - PROFIT_LOCK_COST_BUFFER_RATE), tp1 + epsilon)
        candidate = min(entry, candidate)
    return _tighten_only(side, current_stop, candidate)


def tp2_profit_lock_stop(*, side: str, tp1: float, current_stop: float) -> float:
    """After TP2, move the protective stop to TP1, never farther away."""
    return _tighten_only(str(side or "").upper(), current_stop, tp1)


def _progress_to_tp1(*, side: str, entry: float, tp1: float, price: float) -> float:
    distance = abs(tp1 - entry)
    if distance <= 1e-12 or side not in {"LONG", "SHORT"}:
        return 0.0
    directional = (price - entry) if side == "LONG" else (entry - price)
    return directional / distance


def pre_tp1_protection_signal(
    *,
    side: str,
    entry: float,
    tp1: float,
    high: float,
    low: float,
    close: float,
) -> dict[str, Any]:
    """Detect a completed-candle rejection after price almost reached TP1.

    Nearness alone is not enough. Price must reach at least 85% of the route
    and then give back at least 18% of that TP1 route by candle close.
    """
    side = str(side or "").upper()
    if side not in {"LONG", "SHORT"} or min(entry, tp1, high, low, close) <= 0:
        return {"triggered": False, "reason": "invalid_geometry"}

    favorable_extreme = high if side == "LONG" else low
    max_progress = _progress_to_tp1(side=side, entry=entry, tp1=tp1, price=favorable_extreme)
    close_progress = _progress_to_tp1(side=side, entry=entry, tp1=tp1, price=close)
    retrace = max_progress - close_progress
    triggered = max_progress >= PRE_TP1_ARM_PROGRESS and retrace >= PRE_TP1_RETRACE_FRACTION
    return {
        "triggered": bool(triggered),
        "max_progress": round(max_progress, 4),
        "close_progress": round(close_progress, 4),
        "retrace_fraction": round(retrace, 4),
        "arm_progress": PRE_TP1_ARM_PROGRESS,
        "required_retrace_fraction": PRE_TP1_RETRACE_FRACTION,
        "reason": "near_tp1_rejection_confirmed" if triggered else "no_confirmed_rejection",
    }


def pre_tp1_protective_stop(
    *,
    side: str,
    entry: float,
    current_stop: float,
    close: float,
) -> float:
    """Tighten risk after confirmed near-TP1 rejection without widening."""
    side = str(side or "").upper()
    if side not in {"LONG", "SHORT"} or min(entry, current_stop, close) <= 0:
        return current_stop
    epsilon = entry * 0.00005
    if side == "LONG":
        preferred = entry * (1.0 + PROFIT_LOCK_COST_BUFFER_RATE)
        if close > preferred + epsilon:
            candidate = preferred
        elif close > entry + epsilon:
            candidate = entry
        else:
            reduced_loss = current_stop + (entry - current_stop) * 0.75
            candidate = min(close - epsilon, reduced_loss)
    else:
        preferred = entry * (1.0 - PROFIT_LOCK_COST_BUFFER_RATE)
        if close < preferred - epsilon:
            candidate = preferred
        elif close < entry - epsilon:
            candidate = entry
        else:
            reduced_loss = current_stop - (current_stop - entry) * 0.75
            candidate = max(close + epsilon, reduced_loss)
    return _tighten_only(side, current_stop, candidate)


def evaluate_survival_candle(
    *,
    side: str,
    high: float,
    low: float,
    close: float,
    hard_stop: float,
    soft_stop: float,
    target: float,
    survival_enabled: bool,
) -> tuple[float | None, str | None]:
    """Evaluate one completed candle using the pre-entry stop plan.

    Hard stop is always absolute. With survival enabled, merely wicking through
    the soft invalidation is tolerated; the candle must close beyond it to exit.
    This function intentionally never moves either stop.
    """
    side = str(side or "").upper()
    if side not in {"LONG", "SHORT"}:
        return None, None

    hard_hit = low <= hard_stop if side == "LONG" else high >= hard_stop
    target_hit = high >= target if side == "LONG" else low <= target
    soft_close_invalid = close <= soft_stop if side == "LONG" else close >= soft_stop

    # If hard stop and target both occur inside the same candle, sequence is
    # unknowable from OHLC alone, so remain conservative and record stop.
    if hard_hit and target_hit:
        return hard_stop, "AMBIGUOUS_HARD_STOP"
    if hard_hit:
        return hard_stop, "HARD_STOP"
    if target_hit:
        return target, "TP1"

    if survival_enabled:
        if soft_close_invalid:
            return close, "STRUCTURAL_CLOSE_INVALIDATION"
        return None, None

    normal_hit = low <= soft_stop if side == "LONG" else high >= soft_stop
    if normal_hit:
        return soft_stop, "STOP"
    return None, None


async def close_due_positions(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT id, symbol, side, entry_price, stop_loss, take_profit, quantity,
               notional, opened_at, metadata
        FROM paper_positions
        WHERE status='OPEN'
        ORDER BY opened_at ASC
    """))).mappings().all()

    closed = 0
    actions: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        metadata = _d(row.get("metadata"))
        now = datetime.now(timezone.utc)
        opened_at = row.get("opened_at")
        if not opened_at:
            continue
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)

        max_hold = planned_max_hold_minutes(metadata)
        age_minutes = (now - opened_at).total_seconds() / 60.0

        survival = _d(metadata.get("stop_survival"))
        survival_enabled = bool(metadata.get("stop_survival_enabled")) and bool(survival.get("enabled"))
        confirmation_minutes = int(survival.get("confirmation_minutes") or 5)

        # For a soft-stop close confirmation we need completed candles near the
        # requested confirmation interval. Swing plans use 15m; tactical uses 5m.
        if survival_enabled:
            interval = "15m" if confirmation_minutes >= 15 else "5m"
            interval_minutes = 15 if interval == "15m" else 5
        else:
            interval = "1m" if age_minutes <= 480 else "5m" if age_minutes <= 1440 else "15m"
            interval_minutes = 1 if interval == "1m" else 5 if interval == "5m" else 15

        limit = min(1000, max(50, int(age_minutes / interval_minutes) + 10))
        try:
            klines = await binance_client.klines(row["symbol"], interval=interval, limit=limit)
        except Exception:
            klines = []

        start_ms = int(opened_at.timestamp() * 1000)
        now_ms = int(now.timestamp() * 1000)
        completed = [k for k in klines if len(k) >= 7 and int(k[0]) >= start_ms and int(k[6]) < now_ms]
        future = list(completed)
        exit_price = None
        exit_reason = None
        hard_stop = _f(row.get("stop_loss"))
        soft_stop = _f(metadata.get("soft_invalidation_stop"), hard_stop)
        tp_value = _f(row.get("take_profit"))
        side = str(row.get("side") or "").upper()
        entry = _f(row.get("entry_price"))
        profit_lock = _d(metadata.get("profit_lock"))
        profit_lock_enabled = bool(profit_lock.get("enabled"))
        tp1 = _f(profit_lock.get("tp1"))
        tp2 = _f(profit_lock.get("tp2"))
        lock_stage = str(profit_lock.get("stage") or "INITIAL")
        resume_after_ms = int(max(
            _f(profit_lock.get("last_milestone_candle_ms")),
            _f(profit_lock.get("stop_active_after_candle_ms")),
        ))
        if resume_after_ms > 0:
            # A tightened stop becomes active only after the candle that created it.
            # Never replay the new stop against older candles.
            future = [k for k in future if len(k) >= 5 and int(k[0]) > resume_after_ms]
        lock_changed = False
        adaptive_before = _d(profit_lock.get("adaptive_trailing"))

        for candle in future:
            high, low, close = _f(candle[2]), _f(candle[3]), _f(candle[4])
            exit_price, exit_reason = evaluate_survival_candle(
                side=side,
                high=high,
                low=low,
                close=close,
                hard_stop=hard_stop,
                soft_stop=soft_stop,
                target=tp_value,
                survival_enabled=survival_enabled,
            )
            if exit_price is not None:
                adaptive_active = bool(adaptive_before.get("armed"))
                if lock_stage == "PRE_TP1_PROTECTED" and exit_reason == "HARD_STOP":
                    exit_reason = "PRE_TP1_PROTECT_STOP"
                elif lock_stage == "PRE_TP1_PROTECTED" and exit_reason == "AMBIGUOUS_HARD_STOP":
                    exit_reason = "AMBIGUOUS_PRE_TP1_PROTECT_STOP"
                elif lock_stage != "INITIAL" and exit_reason == "HARD_STOP":
                    exit_reason = "PROFIT_LOCK_STOP"
                elif lock_stage != "INITIAL" and exit_reason == "AMBIGUOUS_HARD_STOP":
                    exit_reason = "AMBIGUOUS_PROFIT_LOCK_STOP"
                elif adaptive_active and exit_reason == "HARD_STOP":
                    exit_reason = "ADAPTIVE_TRAIL_STOP"
                elif adaptive_active and exit_reason == "AMBIGUOUS_HARD_STOP":
                    exit_reason = "AMBIGUOUS_ADAPTIVE_TRAIL_STOP"
                break

            if not profit_lock_enabled:
                continue

            # Before TP1, protect an almost-completed move only after a completed
            # candle confirms a meaningful rejection. Nearness alone never moves
            # the stop. The new stop is active from the NEXT candle.
            if lock_stage == "INITIAL" and tp1 > 0:
                pre_tp1 = pre_tp1_protection_signal(
                    side=side,
                    entry=entry,
                    tp1=tp1,
                    high=high,
                    low=low,
                    close=close,
                )
                if pre_tp1.get("triggered") and not _touched(side, high, low, tp1):
                    new_stop = pre_tp1_protective_stop(
                        side=side,
                        entry=entry,
                        current_stop=hard_stop,
                        close=close,
                    )
                    if new_stop != hard_stop:
                        hard_stop = new_stop
                        lock_changed = True
                        lock_stage = "PRE_TP1_PROTECTED"
                        profit_lock.update({
                            "stage": lock_stage,
                            "active_stop": hard_stop,
                            "rule_active": "NEAR_TP1_REJECTION_PROTECT_RISK",
                            "pre_tp1_signal": pre_tp1,
                            "last_milestone_candle_ms": int(candle[0]) if len(candle) else None,
                            "stop_active_after_candle_ms": int(candle[0]) if len(candle) else None,
                        })
                        continue

            # Milestone tightening becomes active only for later candles. We do
            # not assume whether TP and the tighter stop happened first inside
            # the same OHLC candle.
            if (
                lock_stage not in {"TP2_LOCKED"}
                and _target_beyond(side, tp_value, tp2)
                and _touched(side, high, low, tp2)
            ):
                new_stop = tp2_profit_lock_stop(side=side, tp1=tp1, current_stop=hard_stop)
                if new_stop != hard_stop:
                    hard_stop = new_stop
                    lock_changed = True
                lock_stage = "TP2_LOCKED"
                profit_lock.update({
                    "stage": lock_stage,
                    "active_stop": hard_stop,
                    "rule_active": "TP2_REACHED_STOP_AT_TP1",
                    "last_milestone_candle_ms": int(candle[0]) if len(candle) else None,
                    "stop_active_after_candle_ms": int(candle[0]) if len(candle) else None,
                })
            elif (
                lock_stage in {"INITIAL", "PRE_TP1_PROTECTED"}
                and _target_beyond(side, tp_value, tp1)
                and _touched(side, high, low, tp1)
            ):
                new_stop = breakeven_profit_lock_stop(
                    side=side,
                    entry=entry,
                    tp1=tp1,
                    current_stop=hard_stop,
                )
                if new_stop != hard_stop:
                    hard_stop = new_stop
                    lock_changed = True
                lock_stage = "TP1_LOCKED"
                profit_lock.update({
                    "stage": lock_stage,
                    "active_stop": hard_stop,
                    "rule_active": "TP1_REACHED_BREAKEVEN_PLUS_COST_BUFFER",
                    "last_milestone_candle_ms": int(candle[0]) if len(candle) else None,
                    "stop_active_after_candle_ms": int(candle[0]) if len(candle) else None,
                })

        if profit_lock_enabled and lock_stage != str(_d(metadata.get("profit_lock")).get("stage") or "INITIAL"):
            metadata["profit_lock"] = profit_lock
            metadata["hard_stop"] = hard_stop
            metadata["profit_lock_never_widens_stop"] = True
            await db.execute(text("""
                UPDATE paper_positions
                SET stop_loss=:stop_loss, metadata=CAST(:metadata AS JSONB)
                WHERE id=:id AND status='OPEN'
            """), {
                "id": row["id"],
                "stop_loss": hard_stop,
                "metadata": json.dumps(metadata),
            })

        if exit_price is None and age_minutes >= max_hold:
            if future:
                exit_price = _f(future[-1][4])
            else:
                exit_price = await base._latest_price(row["symbol"])
            exit_reason = "TIME_EXIT"

        if exit_price is None or exit_price <= 0:
            continue

        pnl = base.calculate_trade_pnl(
            side=str(row["side"]),
            entry=_f(row["entry_price"]),
            exit_price=exit_price,
            quantity=_f(row["quantity"]),
            notional=_f(row["notional"]),
            opened_at=opened_at,
            closed_at=now,
        )
        await db.execute(text("""
            UPDATE paper_positions
            SET status='CLOSED', closed_at=:closed_at, exit_price=:exit_price,
                exit_reason=:exit_reason, gross_pnl=:gross_pnl, net_pnl=:net_pnl,
                fees=:fees, slippage=:slippage, funding_estimate=:funding_estimate
            WHERE id=:id
        """), {
            "id": row["id"],
            "closed_at": now,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            **pnl,
        })
        await db.execute(text("""
            UPDATE paper_accounts
            SET cash_balance=cash_balance+:net_pnl,
                realized_pnl=realized_pnl+:net_pnl,
                total_fees=total_fees+:fees+:slippage+:funding_estimate,
                updated_at=NOW()
            WHERE id=1
        """), pnl)
        closed += 1
        actions.append({
            "symbol": row["symbol"],
            "reason": exit_reason,
            "age_minutes": round(age_minutes, 1),
            "max_hold_minutes": max_hold,
            "strategy_mode": metadata.get("strategy_mode"),
            "stop_survival_enabled": survival_enabled,
            "soft_invalidation_stop": soft_stop,
            "hard_stop": hard_stop,
            "profit_lock_stage": lock_stage,
            "confirmation_minutes": confirmation_minutes if survival_enabled else None,
        })

    await db.commit()
    return {"version": VERSION, "closed": closed, "actions": actions}
