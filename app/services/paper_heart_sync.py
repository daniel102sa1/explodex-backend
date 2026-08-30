from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.binance import binance_client
from app.services.paper_trading import (
    DEFAULT_PAPER_ACCOUNT,
    DEFAULT_SCANNER_SETTINGS,
    _get_setting,
    _paper_equity,
)
from app.services.trade_thesis import mark_thesis_entered

VERSION = "paper_heart_sync_v1"
MAX_HEART_RISK_SCORE = 48.0
SIGNAL_FRESH_MINUTES = 30


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
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


def _valid_geometry(side: str, entry: float, stop: float, tp1: float) -> bool:
    if side == "LONG":
        return stop < entry < tp1
    if side == "SHORT":
        return tp1 < entry < stop
    return False


def _inside_zone(price: float, low: float, high: float) -> bool:
    return price > 0 and low > 0 and high > 0 and min(low, high) <= price <= max(low, high)


def _primary_blocker(blockers: Counter[str], heart_enter_count: int) -> str:
    if heart_enter_count <= 0:
        return "no_heart_enter_signals"
    if not blockers:
        return "none"
    return blockers.most_common(1)[0][0]


async def sync_heart_paper_signals(db: AsyncSession) -> dict[str, Any]:
    """Open the PAPER trades shown by the app from the canonical Heart only.

    This replaces the automatic legacy READY sync. It intentionally does not
    re-apply the old min_setup/max_risk entry gates after the Heart has already
    made an ENTER decision. Portfolio limits and live entry-zone checks remain.
    """
    if not settings.paper_trading_only:
        return {
            "version": VERSION,
            "opened": 0,
            "blocked": True,
            "reason": "paper_trading_only_disabled",
        }

    scanner_cfg = await _get_setting(db, "scanner", DEFAULT_SCANNER_SETTINGS)
    paper_cfg = await _get_setting(db, "paper_account", DEFAULT_PAPER_ACCOUNT)
    starting_equity = _f(paper_cfg.get("starting_equity_usdt"), 1000.0)
    equity, daily_pnl = await _paper_equity(db, starting_equity)
    max_daily_loss = equity * _f(scanner_cfg.get("max_daily_loss_pct"), 3.0) / 100.0

    if daily_pnl <= -max_daily_loss:
        return {
            "version": VERSION,
            "opened": 0,
            "blocked": True,
            "reason": "daily_loss_limit",
            "equity_usdt": round(equity, 4),
            "daily_pnl_usdt": round(daily_pnl, 4),
        }

    open_count = int((await db.execute(text(
        "SELECT COUNT(*) FROM trades WHERE mode='PAPER' AND status IN ('OPEN','PARTIAL')"
    ))).scalar_one() or 0)
    max_open = int(scanner_cfg.get("max_open_trades") or 2)
    available_slots = max(0, max_open - open_count)
    if available_slots <= 0:
        return {
            "version": VERSION,
            "opened": 0,
            "blocked": True,
            "reason": "max_open_trades",
            "open_trades": open_count,
            "max_open_trades": max_open,
        }

    rows = (await db.execute(text(f"""
        SELECT DISTINCT ON (s.symbol_id)
               s.id::text AS signal_id,
               s.symbol_id::text AS symbol_id,
               sy.symbol,
               s.created_at,
               s.direction,
               s.state,
               s.setup_score,
               s.risk_score,
               s.current_price,
               s.entry_low,
               s.entry_high,
               s.stop_loss,
               s.tp1,
               s.tp2,
               s.tp3,
               s.expected_duration_min_minutes,
               s.expected_duration_max_minutes,
               s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.is_active=TRUE
          AND s.created_at >= NOW() - INTERVAL '{SIGNAL_FRESH_MINUTES} minutes'
        ORDER BY s.symbol_id, s.created_at DESC
    """))).mappings().all()

    candidates: list[dict[str, Any]] = []
    blockers: Counter[str] = Counter()
    heart_actions: Counter[str] = Counter()
    heart_enter_count = 0

    for raw in rows:
        row = dict(raw)
        reason = _dict(row.get("reason"))
        heart = _dict(reason.get("explodex_heart"))
        decision = _dict(heart.get("action_decision"))
        plan = _dict(heart.get("plan"))
        thesis = _dict(heart.get("thesis"))
        action = str(decision.get("action") or "NO_HEART_ACTION")
        heart_actions[action] += 1

        should_enter = bool(decision.get("should_enter")) and bool(heart.get("execution_allowed"))
        if should_enter:
            heart_enter_count += 1
        else:
            blockers[f"heart_{action.lower()}"] += 1
            continue
        if str(row.get("state") or "") != "READY":
            blockers["signal_not_ready"] += 1
            continue
        if _f(row.get("risk_score"), 100.0) > MAX_HEART_RISK_SCORE:
            blockers["risk_above_heart_cap"] += 1
            continue
        if not bool(thesis.get("frozen_plan")):
            blockers["no_frozen_thesis"] += 1
            continue

        row["heart"] = heart
        row["decision"] = decision
        row["plan"] = plan
        row["thesis"] = thesis
        candidates.append(row)

    if not candidates:
        return {
            "version": VERSION,
            "opened": 0,
            "blocked": False,
            "reason": _primary_blocker(blockers, heart_enter_count),
            "latest_signals_checked": len(rows),
            "heart_enter_signals": heart_enter_count,
            "heart_actions": dict(heart_actions),
            "blockers": dict(blockers),
            "open_trades": open_count,
            "available_slots": available_slots,
            "equity_usdt": round(equity, 4),
            "daily_pnl_usdt": round(daily_pnl, 4),
        }

    semaphore = asyncio.Semaphore(5)

    async def live_price(candidate: dict[str, Any]) -> tuple[dict[str, Any], float | None, str | None]:
        async with semaphore:
            try:
                payload = await binance_client.price(candidate["symbol"])
                return candidate, _f(payload.get("price")), None
            except Exception as exc:
                return candidate, None, str(exc)[:250]

    priced = await asyncio.gather(*(live_price(item) for item in candidates))
    opened: list[dict[str, Any]] = []

    for candidate, price, error in priced:
        if len(opened) >= available_slots:
            blockers["no_portfolio_slot"] += 1
            break
        if error or not price:
            blockers["price_error"] += 1
            continue

        plan = candidate["plan"]
        thesis = candidate["thesis"]
        side = str(candidate["heart"].get("direction") or candidate.get("direction") or "").upper()
        low = _f(plan.get("entry_low"), _f(thesis.get("entry_low"), _f(candidate.get("entry_low"))))
        high = _f(plan.get("entry_high"), _f(thesis.get("entry_high"), _f(candidate.get("entry_high"))))
        stop = _f(plan.get("stop_loss"), _f(thesis.get("stop_loss"), _f(candidate.get("stop_loss"))))
        tp1 = _f(plan.get("tp1"), _f(thesis.get("tp1"), _f(candidate.get("tp1"))))
        tp2 = _f(plan.get("tp2"), _f(thesis.get("tp2"), _f(candidate.get("tp2"))))
        tp3 = _f(plan.get("tp3"), _f(thesis.get("tp3"), _f(candidate.get("tp3"))))

        if not _inside_zone(price, low, high):
            blockers["outside_entry_zone"] += 1
            continue
        if not _valid_geometry(side, price, stop, tp1):
            blockers["invalid_geometry"] += 1
            continue

        already_open = int((await db.execute(text("""
            SELECT COUNT(*) FROM trades
            WHERE symbol_id=CAST(:symbol_id AS UUID)
              AND mode='PAPER' AND status IN ('OPEN','PARTIAL')
        """), {"symbol_id": candidate["symbol_id"]})).scalar_one() or 0)
        if already_open:
            blockers["symbol_already_open"] += 1
            continue

        duplicate_signal = int((await db.execute(text("""
            SELECT COUNT(*) FROM trades
            WHERE signal_id=CAST(:signal_id AS UUID) AND mode='PAPER'
        """), {"signal_id": candidate["signal_id"]})).scalar_one() or 0)
        if duplicate_signal:
            blockers["signal_already_traded"] += 1
            continue

        risk_fraction = abs(price - stop) / price if price > 0 else 0.0
        if risk_fraction <= 0:
            blockers["zero_risk_distance"] += 1
            continue

        risk_pct = _f(scanner_cfg.get("risk_per_trade_pct"), 0.5)
        risk_usdt = equity * risk_pct / 100.0
        max_leverage = _f(paper_cfg.get("max_leverage"), 3.0)
        raw_notional = risk_usdt / risk_fraction
        notional = min(raw_notional, equity * max_leverage)
        quantity = notional / price
        effective_leverage = notional / equity if equity > 0 else 1.0
        if quantity <= 0 or notional <= 0:
            blockers["zero_position_size"] += 1
            continue

        trade_id = str(uuid.uuid4())
        metadata = {
            "engine_version": VERSION,
            "canonical_source": "EXPLODEX_HEART",
            "explodex_heart": candidate["heart"],
            "action_decision": candidate["decision"],
            "plan_is_frozen": True,
            "post_signal_direction_recalculation": False,
            "post_signal_stop_widening": False,
            "original_stop": stop,
            "initial_risk_usdt": risk_usdt,
            "virtual_equity_at_entry": equity,
            "setup_score_at_entry": _f(candidate.get("setup_score")),
            "risk_score_at_entry": _f(candidate.get("risk_score")),
            "target_policy": paper_cfg.get("target_policy", "TP2_FULL"),
            "tp1_hit": False,
        }

        await db.execute(text("""
            INSERT INTO trades (
                id, signal_id, symbol_id, mode, direction, status,
                leverage, risk_pct, entry_price, quantity, notional_usdt,
                stop_loss, tp1, tp2, tp3, opened_at, fees_usdt, metadata
            ) VALUES (
                :id, CAST(:signal_id AS UUID), CAST(:symbol_id AS UUID), 'PAPER', :direction, 'OPEN',
                :leverage, :risk_pct, :entry_price, :quantity, :notional,
                :stop_loss, :tp1, :tp2, :tp3, NOW(), 0, CAST(:metadata AS JSONB)
            )
        """), {
            "id": trade_id,
            "signal_id": candidate["signal_id"],
            "symbol_id": candidate["symbol_id"],
            "direction": side,
            "leverage": effective_leverage,
            "risk_pct": risk_pct,
            "entry_price": price,
            "quantity": quantity,
            "notional": notional,
            "stop_loss": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "metadata": json.dumps(metadata),
        })
        await db.execute(text("""
            INSERT INTO trade_events (trade_id, event_type, price, setup_score, risk_score, message, data)
            VALUES (:trade_id, 'OPEN', :price, :setup_score, :risk_score,
                    'PAPER opened by canonical ExplodeX Heart', CAST(:data AS JSONB))
        """), {
            "trade_id": trade_id,
            "price": price,
            "setup_score": _f(candidate.get("setup_score")),
            "risk_score": _f(candidate.get("risk_score")),
            "data": json.dumps({
                "version": VERSION,
                "risk_usdt": risk_usdt,
                "notional_usdt": notional,
                "action": candidate["decision"].get("action"),
            }),
        })
        await db.execute(text("""
            INSERT INTO alerts (signal_id, trade_id, channel, severity, title, message, is_sent)
            VALUES (CAST(:signal_id AS UUID), :trade_id, 'APP', 'ENTRY', :title, :message, FALSE)
        """), {
            "signal_id": candidate["signal_id"],
            "trade_id": trade_id,
            "title": f"PAPER {side} {candidate['symbol']}",
            "message": f"Heart ENTER | Entrada {price:.12g} | SL {stop:.12g} | TP1 {tp1:.12g}",
        })
        await db.execute(text("UPDATE signals SET state='ENTER', updated_at=NOW() WHERE id=CAST(:id AS UUID)"), {
            "id": candidate["signal_id"],
        })
        await mark_thesis_entered(db, candidate["symbol"])

        opened.append({
            "trade_id": trade_id,
            "symbol": candidate["symbol"],
            "direction": side,
            "entry": price,
            "stop_loss": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "risk_usdt": round(risk_usdt, 4),
            "risk_pct": risk_pct,
            "notional_usdt": round(notional, 4),
            "effective_leverage": round(effective_leverage, 3),
        })

    await db.commit()
    return {
        "version": VERSION,
        "opened": len(opened),
        "trades": opened,
        "reason": "opened" if opened else _primary_blocker(blockers, heart_enter_count),
        "latest_signals_checked": len(rows),
        "heart_enter_signals": heart_enter_count,
        "heart_actions": dict(heart_actions),
        "blockers": dict(blockers),
        "open_before": open_count,
        "available_slots": available_slots,
        "equity_usdt": round(equity, 4),
        "daily_pnl_usdt": round(daily_pnl, 4),
    }
