from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client
from app.services.execution_math import choose_target_for_min_net_rr
from app.services.structure_retest_strategy import detect_structure_retest

VERSION = "structure_retest_persistence_v1"
TTL_MINUTES = 360
MAX_SYMBOLS_PER_RUN = 8


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


async def ensure_structure_retest_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS structure_retest_setups (
            id UUID PRIMARY KEY,
            symbol VARCHAR(32) NOT NULL,
            direction VARCHAR(8) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'ACTIVE',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            entered_at TIMESTAMPTZ,
            origin_signal_id UUID,
            breakout_level NUMERIC(30,12) NOT NULL,
            retest_price NUMERIC(30,12),
            entry_low NUMERIC(30,12) NOT NULL,
            entry_high NUMERIC(30,12) NOT NULL,
            chase_limit NUMERIC(30,12) NOT NULL,
            structural_level NUMERIC(30,12),
            structural_stop NUMERIC(30,12) NOT NULL,
            target_price NUMERIC(30,12) NOT NULL,
            target_name VARCHAR(40),
            pattern_score NUMERIC(10,4) NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_structure_retest_active "
        "ON structure_retest_setups(symbol, status, created_at DESC)"
    ))
    await db.commit()


async def _active_setup(db: AsyncSession, symbol: str) -> dict[str, Any] | None:
    row = (await db.execute(text("""
        SELECT * FROM structure_retest_setups
        WHERE symbol=:symbol AND status IN ('ACTIVE','NO_CHASE')
        ORDER BY created_at DESC LIMIT 1
    """), {"symbol": symbol})).mappings().first()
    return dict(row) if row else None


def _live_setup_state(setup: dict[str, Any], current_price: float) -> tuple[str, str, bool]:
    direction = str(setup.get("direction") or "").upper()
    stop = _f(setup.get("structural_stop"))
    low, high = sorted((_f(setup.get("entry_low")), _f(setup.get("entry_high"))))
    chase = _f(setup.get("chase_limit"))
    current = _f(current_price)

    if direction == "LONG" and current <= stop:
        return "INVALIDATED", "structural_stop_broken", False
    if direction == "SHORT" and current >= stop:
        return "INVALIDATED", "structural_stop_broken", False
    if direction == "LONG" and current > chase:
        return "NO_CHASE", "price_above_frozen_chase_limit", False
    if direction == "SHORT" and current < chase:
        return "NO_CHASE", "price_below_frozen_chase_limit", False
    if low <= current <= high:
        return "ACTIVE", "inside_frozen_entry_zone", True
    return "ACTIVE", "waiting_frozen_entry_zone", False


async def _get_or_freeze_setup(
    db: AsyncSession,
    *,
    symbol: str,
    signal_id: str,
    current_price: float,
    analysis: dict[str, Any],
    target_math: dict[str, Any],
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    existing = await _active_setup(db, symbol)
    direction = str(analysis.get("direction") or "").upper()

    if existing:
        expires_at = existing.get("expires_at")
        existing_direction = str(existing.get("direction") or "").upper()
        if expires_at and expires_at <= now:
            await db.execute(text("UPDATE structure_retest_setups SET status='EXPIRED', updated_at=NOW() WHERE id=:id"), {"id": existing["id"]})
            await db.commit()
            existing = None
        elif existing_direction != direction:
            await db.execute(text("UPDATE structure_retest_setups SET status='SUPERSEDED', updated_at=NOW() WHERE id=:id"), {"id": existing["id"]})
            await db.commit()
            existing = None
        else:
            state, reason, eligible = _live_setup_state(existing, current_price)
            if state == "INVALIDATED":
                await db.execute(text("UPDATE structure_retest_setups SET status='INVALIDATED', updated_at=NOW() WHERE id=:id"), {"id": existing["id"]})
                await db.commit()
                return {**existing, "status": state, "eligible_now": False, "state_reason": reason}
            if state == "NO_CHASE" and str(existing.get("status") or "") != "NO_CHASE":
                await db.execute(text("UPDATE structure_retest_setups SET status='NO_CHASE', updated_at=NOW() WHERE id=:id"), {"id": existing["id"]})
                await db.commit()
            return {**existing, "status": state, "eligible_now": eligible, "state_reason": reason}

    if not analysis.get("paper_candidate") or not target_math.get("accepted"):
        return None

    chosen = _d(target_math.get("chosen_target"))
    setup_id = str(uuid.uuid4())
    expires_at = now + timedelta(minutes=TTL_MINUTES)
    metadata = {
        "version": VERSION,
        "analysis_at_creation": analysis,
        "execution_math_at_creation": target_math,
        "entry_zone_frozen": True,
        "stop_fixed_before_entry": True,
        "stop_basis": "retest_swing_plus_atr_buffer",
        "money_loss_does_not_place_stop": True,
        "position_size_adapts_to_stop": True,
    }
    row = (await db.execute(text("""
        INSERT INTO structure_retest_setups (
            id, symbol, direction, status, expires_at, origin_signal_id,
            breakout_level, retest_price, entry_low, entry_high, chase_limit,
            structural_level, structural_stop, target_price, target_name,
            pattern_score, metadata
        ) VALUES (
            CAST(:id AS UUID), :symbol, :direction, 'ACTIVE', :expires_at, CAST(:origin_signal_id AS UUID),
            :breakout_level, :retest_price, :entry_low, :entry_high, :chase_limit,
            :structural_level, :structural_stop, :target_price, :target_name,
            :pattern_score, CAST(:metadata AS JSONB)
        ) RETURNING *
    """), {
        "id": setup_id,
        "symbol": symbol,
        "direction": direction,
        "expires_at": expires_at,
        "origin_signal_id": signal_id,
        "breakout_level": _f(analysis.get("breakout_level")),
        "retest_price": _f(analysis.get("retest_price")),
        "entry_low": _f(analysis.get("entry_low")),
        "entry_high": _f(analysis.get("entry_high")),
        "chase_limit": _f(analysis.get("chase_limit")),
        "structural_level": _f(analysis.get("structural_level")),
        "structural_stop": _f(analysis.get("structural_stop")),
        "target_price": _f(chosen.get("price")),
        "target_name": chosen.get("name"),
        "pattern_score": _f(analysis.get("pattern_score")),
        "metadata": json.dumps(metadata),
    })).mappings().one()
    await db.commit()
    state, reason, eligible = _live_setup_state(dict(row), current_price)
    return {**dict(row), "status": state, "eligible_now": eligible, "state_reason": reason}


async def mark_structure_retest_entered(db: AsyncSession, setup_id: str) -> None:
    await ensure_structure_retest_schema(db)
    await db.execute(text("""
        UPDATE structure_retest_setups
        SET status='ENTERED', entered_at=COALESCE(entered_at, NOW()), updated_at=NOW()
        WHERE id=CAST(:id AS UUID)
    """), {"id": setup_id})
    await db.commit()


async def persist_structure_retest_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    await ensure_structure_retest_schema(db)
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.setup_score,
               s.risk_score, s.current_price, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.setup_score DESC NULLS LAST, s.risk_score ASC NULLS LAST
        LIMIT :limit
    """), {"run_id": run_id, "limit": MAX_SYMBOLS_PER_RUN})).mappings().all()

    updated = 0
    eligible = 0
    frozen = 0
    no_chase = 0
    rejected: dict[str, int] = {}

    def reject(name: str) -> None:
        rejected[name] = rejected.get(name, 0) + 1

    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        contract = _d(heart.get("execution_contract"))
        if not heart or not contract:
            reject("missing_heart_contract")
            continue

        primary_direction = str(contract.get("primary_direction") or heart.get("direction") or row.get("direction") or "").upper()
        symbol = str(row.get("symbol") or "")
        current = _f(row.get("current_price"))

        try:
            klines15, klines1h = await asyncio.gather(
                binance_client.klines(symbol, "15m", 90),
                binance_client.klines(symbol, "1h", 90),
            )
            analysis = detect_structure_retest(
                direction=primary_direction,
                current_price=current,
                klines_15m=klines15,
                klines_1h=klines1h,
            )
        except Exception as exc:
            reject("market_data_error")
            analysis = {
                "version": "structure_retest_strategy_v1",
                "phase": "ERROR",
                "direction": primary_direction,
                "pattern_score": 0.0,
                "paper_candidate": False,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }

        event = _d(contract.get("event_risk")) or _d(heart.get("event_risk"))
        breadth = _d(contract.get("market_breadth")) or _d(heart.get("market_breadth"))
        breadth_alignment = _d(breadth.get("alignment_to_primary"))
        risk_score = _f(row.get("risk_score"), 100.0)

        blockers: list[str] = []
        if primary_direction not in {"LONG", "SHORT"}:
            blockers.append("primary_direction_missing")
        if not bool(analysis.get("paper_candidate")):
            blockers.append("structure_retest_not_confirmed")
        if _f(analysis.get("pattern_score")) < 72.0:
            blockers.append("pattern_score_below_72")
        if risk_score > 60.0:
            blockers.append("risk_score_above_60")
        if not bool(contract.get("hard_safety_clear", True)):
            blockers.append("heart_hard_safety_block")
        if bool(event.get("block_new_entries")):
            blockers.append("critical_event_block")
        if str(breadth_alignment.get("state") or "") == "STRONG_CONFLICT":
            blockers.append("market_breadth_strong_conflict")

        targets = _d(analysis.get("targets"))
        target_math = {"accepted": False, "reason": "not_evaluated"}
        stop = _f(analysis.get("structural_stop"))
        if current > 0 and stop > 0:
            target_math = choose_target_for_min_net_rr(
                side=primary_direction,
                entry=current,
                stop=stop,
                targets=[
                    ("HTF_LIQUIDITY", _f(targets.get("htf_liquidity"))),
                    ("R2_5", _f(targets.get("r2_5"))),
                    ("R3_5", _f(targets.get("r3_5"))),
                    ("R5_0", _f(targets.get("r5_0"))),
                ],
                expected_hold_hours=8.0,
                min_net_rr=2.4,
            )
        if not target_math.get("accepted"):
            blockers.append("net_rr_below_2_4")

        setup = await _get_or_freeze_setup(
            db,
            symbol=symbol,
            signal_id=str(row.get("signal_id")),
            current_price=current,
            analysis=analysis,
            target_math=target_math,
        )

        if setup:
            frozen += 1
            if str(setup.get("status") or "") == "NO_CHASE":
                no_chase += 1
                blockers.append("frozen_setup_no_chase")
            if not bool(setup.get("eligible_now")):
                blockers.append(str(setup.get("state_reason") or "outside_frozen_entry_zone"))

        existing_lane = str(contract.get("permitted_paper_lane") or "")
        lane = {
            "lane": "STRUCTURE_RETEST_PAPER",
            "paper_only": True,
            "experimental": True,
            "eligible": bool(setup) and bool(setup.get("eligible_now")) and not blockers,
            "direction": primary_direction,
            "pattern_score": analysis.get("pattern_score"),
            "phase": analysis.get("phase"),
            "breakout_level": setup.get("breakout_level") if setup else analysis.get("breakout_level"),
            "retest_price": setup.get("retest_price") if setup else analysis.get("retest_price"),
            "entry_low": setup.get("entry_low") if setup else analysis.get("entry_low"),
            "entry_high": setup.get("entry_high") if setup else analysis.get("entry_high"),
            "chase_limit": setup.get("chase_limit") if setup else analysis.get("chase_limit"),
            "stop_loss": setup.get("structural_stop") if setup else analysis.get("structural_stop"),
            "soft_invalidation_level": setup.get("structural_level") if setup else analysis.get("structural_level"),
            "target_name": setup.get("target_name") if setup else _d(target_math.get("chosen_target")).get("name"),
            "target_price": setup.get("target_price") if setup else _d(target_math.get("chosen_target")).get("price"),
            "setup_id": str(setup.get("id")) if setup else None,
            "entry_zone_frozen": bool(setup),
            "stop_fixed_before_entry": bool(setup),
            "stop_can_widen_after_entry": False,
            "position_size_must_adapt_to_stop": True,
            "money_loss_does_not_place_stop": True,
            "execution_math": target_math,
            "risk_budget_pct": 0.35,
            "max_leverage": 2,
            "max_hold_minutes": 480,
            "market_breadth_alignment": breadth_alignment,
            "event_risk": event,
            "analysis": analysis,
            "blockers": list(dict.fromkeys(blockers)),
            "reason": "Breakout + retest + structure continuation. Stop comes from structural invalidation plus ATR buffer; size adapts to that stop.",
            "source": "HEART_STRUCTURE_RETEST_EXPERIMENT",
        }

        lanes = _d(contract.get("lanes"))
        lanes["structure_retest_paper"] = lane
        contract["lanes"] = lanes
        priority = list(contract.get("priority") or ["TACTICAL", "AGGRESSIVE_PAPER", "SWING_PAPER", "PRE_EVENT_PAPER"])
        if "STRUCTURE_RETEST_PAPER" not in priority:
            priority.append("STRUCTURE_RETEST_PAPER")
        contract["priority"] = priority

        if lane["eligible"] and not existing_lane:
            contract["permitted_paper_lane"] = "STRUCTURE_RETEST_PAPER"
            eligible += 1
        elif lane["eligible"] and existing_lane:
            reject("higher_priority_lane_exists")
        else:
            for blocker in lane["blockers"]:
                reject(str(blocker))

        heart["structure_retest"] = {
            "version": VERSION,
            "analysis": analysis,
            "frozen_setup": setup,
            "paper_lane": lane,
            "creates_primary_direction": False,
            "changes_primary_direction": False,
        }
        heart["execution_contract"] = contract
        reason["explodex_heart"] = heart
        if prediction:
            prediction["explodex_heart"] = heart
            reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals SET reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:id AS UUID)
        """), {"id": row["signal_id"], "reason": json.dumps(reason)})
        updated += 1

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "updated": updated,
        "eligible": eligible,
        "frozen_setups": frozen,
        "no_chase": no_chase,
        "rejected": rejected,
        "paper_only": True,
    }
