from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client
from app.services.paper_portfolio import (
    FUNDING_ESTIMATE_8H,
    MAX_PAPER_LEVERAGE,
    SLIPPAGE_RATE,
    TAKER_FEE_RATE,
    calculate_trade_pnl,
)

STARTING_BALANCE = 1000.0
MANUAL_STRATEGY = "MANUAL_PRACTICE"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def simulated_liquidation_price(entry: float, side: str, leverage: int) -> float:
    """Simple isolated-margin teaching model, not an exchange liquidation formula."""
    lev = max(1, int(leverage))
    if lev <= 1:
        return 0.0
    move = 1.0 / lev
    return max(0.0, entry * (1.0 - move)) if side == "LONG" else entry * (1.0 + move)


async def _market_price(symbol: str) -> float:
    payload = await binance_client.price(symbol)
    price = _f(payload.get("price") if isinstance(payload, dict) else payload)
    if price <= 0:
        raise ValueError("market_price_unavailable")
    return price


async def ensure_manual_practice_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS manual_practice_accounts (
            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id=1),
            starting_balance NUMERIC(18,6) NOT NULL DEFAULT 1000,
            cash_balance NUMERIC(18,6) NOT NULL DEFAULT 1000,
            realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
            total_costs NUMERIC(18,6) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("""
        INSERT INTO manual_practice_accounts (id, starting_balance, cash_balance)
        VALUES (1, :balance, :balance)
        ON CONFLICT (id) DO NOTHING
    """), {"balance": STARTING_BALANCE})
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS manual_practice_orders (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            order_type VARCHAR(12) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
            limit_price NUMERIC(30,12),
            fill_price NUMERIC(30,12),
            margin_usdt NUMERIC(24,8) NOT NULL,
            leverage INTEGER NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            tp1 NUMERIC(30,12) NOT NULL,
            tp2 NUMERIC(30,12) NOT NULL,
            tp3 NUMERIC(30,12) NOT NULL,
            practice_note TEXT,
            auto_be_after_tp1 BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            filled_at TIMESTAMPTZ,
            canceled_at TIMESTAMPTZ,
            cancel_reason VARCHAR(64),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS manual_practice_positions (
            id BIGSERIAL PRIMARY KEY,
            order_id BIGINT REFERENCES manual_practice_orders(id) ON DELETE SET NULL,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
            leverage INTEGER NOT NULL,
            entry_price NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            tp1 NUMERIC(30,12) NOT NULL,
            tp2 NUMERIC(30,12) NOT NULL,
            tp3 NUMERIC(30,12) NOT NULL,
            liquidation_price NUMERIC(30,12),
            quantity_initial NUMERIC(30,12) NOT NULL,
            quantity_remaining NUMERIC(30,12) NOT NULL,
            notional_initial NUMERIC(24,8) NOT NULL,
            margin_initial NUMERIC(24,8) NOT NULL,
            margin_remaining NUMERIC(24,8) NOT NULL,
            realized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
            total_costs NUMERIC(24,8) NOT NULL DEFAULT 0,
            opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            closed_at TIMESTAMPTZ,
            exit_price NUMERIC(30,12),
            exit_reason VARCHAR(64),
            practice_note TEXT,
            tp1_hit BOOLEAN NOT NULL DEFAULT FALSE,
            tp2_hit BOOLEAN NOT NULL DEFAULT FALSE,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS manual_practice_events (
            id BIGSERIAL PRIMARY KEY,
            position_id BIGINT NOT NULL REFERENCES manual_practice_positions(id) ON DELETE CASCADE,
            event_type VARCHAR(32) NOT NULL,
            price NUMERIC(30,12),
            quantity NUMERIC(30,12),
            net_pnl NUMERIC(24,8),
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_manual_orders_status ON manual_practice_orders(status, created_at DESC)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_manual_positions_status ON manual_practice_positions(status, opened_at DESC)"))
    await db.commit()


async def _available_margin(db: AsyncSession) -> tuple[float, float, float]:
    account = dict((await db.execute(text("SELECT * FROM manual_practice_accounts WHERE id=1"))).mappings().one())
    used = _f((await db.execute(text("""
        SELECT COALESCE(SUM(margin_remaining),0)
        FROM manual_practice_positions WHERE status='OPEN'
    """))).scalar_one())
    reserved = _f((await db.execute(text("""
        SELECT COALESCE(SUM(margin_usdt),0)
        FROM manual_practice_orders WHERE status='PENDING'
    """))).scalar_one())
    cash = _f(account.get("cash_balance"), STARTING_BALANCE)
    return cash, used, max(0.0, cash - used - reserved)


def _validate_geometry(side: str, entry: float, stop: float, tp1: float, tp2: float, tp3: float, *, allow_be: bool = False) -> None:
    long_stop_ok = 0 < stop <= entry if allow_be else 0 < stop < entry
    short_stop_ok = stop >= entry if allow_be else stop > entry
    if side == "LONG" and not (long_stop_ok and entry < tp1 <= tp2 <= tp3):
        raise ValueError("invalid_long_geometry")
    if side == "SHORT" and not (0 < tp3 <= tp2 <= tp1 < entry and short_stop_ok):
        raise ValueError("invalid_short_geometry")


async def _fill_order(db: AsyncSession, order: dict[str, Any], fill_price: float) -> int:
    side = str(order["side"])
    _validate_geometry(side, fill_price, _f(order["stop_loss"]), _f(order["tp1"]), _f(order["tp2"]), _f(order["tp3"]))
    margin = _f(order["margin_usdt"])
    leverage = max(1, min(MAX_PAPER_LEVERAGE, int(order["leverage"])))
    notional = margin * leverage
    quantity = notional / fill_price
    liquidation = simulated_liquidation_price(fill_price, side, leverage)
    meta = {
        "strategy_mode": MANUAL_STRATEGY,
        "paper_only": True,
        "order_type": order["order_type"],
        "auto_be_after_tp1": bool(order.get("auto_be_after_tp1")),
        "cost_model": {
            "taker_fee_rate": TAKER_FEE_RATE,
            "slippage_rate": SLIPPAGE_RATE,
            "funding_estimate_8h": FUNDING_ESTIMATE_8H,
        },
    }
    result = await db.execute(text("""
        INSERT INTO manual_practice_positions (
            order_id, symbol, side, leverage, entry_price, stop_loss, tp1, tp2, tp3,
            liquidation_price, quantity_initial, quantity_remaining, notional_initial,
            margin_initial, margin_remaining, opened_at, practice_note, metadata
        ) VALUES (
            :order_id, :symbol, :side, :leverage, :entry, :stop, :tp1, :tp2, :tp3,
            :liquidation, :quantity, :quantity, :notional, :margin, :margin, :opened_at,
            :note, CAST(:metadata AS JSONB)
        ) RETURNING id
    """), {
        "order_id": order["id"],
        "symbol": order["symbol"],
        "side": side,
        "leverage": leverage,
        "entry": fill_price,
        "stop": _f(order["stop_loss"]),
        "tp1": _f(order["tp1"]),
        "tp2": _f(order["tp2"]),
        "tp3": _f(order["tp3"]),
        "liquidation": liquidation or None,
        "quantity": quantity,
        "notional": notional,
        "margin": margin,
        "opened_at": datetime.now(timezone.utc),
        "note": order.get("practice_note"),
        "metadata": json.dumps(meta),
    })
    position_id = int(result.scalar_one())
    await db.execute(text("""
        UPDATE manual_practice_orders
        SET status='FILLED', fill_price=:fill, filled_at=NOW()
        WHERE id=:id
    """), {"fill": fill_price, "id": order["id"]})
    await db.execute(text("""
        INSERT INTO manual_practice_events(position_id,event_type,price,quantity,note)
        VALUES(:id,'OPEN',:price,:qty,:note)
    """), {"id": position_id, "price": fill_price, "qty": quantity, "note": order.get("practice_note")})
    return position_id


async def create_manual_order(
    db: AsyncSession,
    *,
    symbol: str,
    side: str,
    order_type: str,
    margin_usdt: float,
    leverage: int,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp3: float,
    limit_price: float | None = None,
    practice_note: str | None = None,
    auto_be_after_tp1: bool = False,
) -> dict[str, Any]:
    await ensure_manual_practice_schema(db)
    side = str(side or "").upper()
    order_type = str(order_type or "MARKET").upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("invalid_side")
    if order_type not in {"MARKET", "LIMIT"}:
        raise ValueError("invalid_order_type")
    margin_usdt = _f(margin_usdt)
    leverage = max(1, min(MAX_PAPER_LEVERAGE, int(leverage)))
    if margin_usdt <= 0:
        raise ValueError("invalid_margin")

    market = await _market_price(symbol)
    reference = market if order_type == "MARKET" else _f(limit_price)
    if reference <= 0:
        raise ValueError("invalid_limit_price")
    if order_type == "LIMIT":
        if side == "LONG" and reference >= market:
            raise ValueError("long_limit_must_be_below_market")
        if side == "SHORT" and reference <= market:
            raise ValueError("short_limit_must_be_above_market")
    _validate_geometry(side, reference, _f(stop_loss), _f(tp1), _f(tp2), _f(tp3))

    _, _, available = await _available_margin(db)
    if margin_usdt > available + 1e-9:
        raise ValueError("insufficient_paper_margin")

    result = await db.execute(text("""
        INSERT INTO manual_practice_orders (
            symbol, side, order_type, status, limit_price, margin_usdt, leverage,
            stop_loss, tp1, tp2, tp3, practice_note, auto_be_after_tp1, metadata
        ) VALUES (
            :symbol, :side, :order_type, 'PENDING', :limit_price, :margin, :leverage,
            :stop, :tp1, :tp2, :tp3, :note, :auto_be, '{}'::jsonb
        ) RETURNING *
    """), {
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "limit_price": reference if order_type == "LIMIT" else None,
        "margin": margin_usdt,
        "leverage": leverage,
        "stop": _f(stop_loss),
        "tp1": _f(tp1),
        "tp2": _f(tp2),
        "tp3": _f(tp3),
        "note": (practice_note or "")[:500],
        "auto_be": bool(auto_be_after_tp1),
    })
    order = dict(result.mappings().one())
    position_id = None
    if order_type == "MARKET":
        position_id = await _fill_order(db, order, market)
    await db.commit()
    return {
        "paper_only": True,
        "order_id": order["id"],
        "position_id": position_id,
        "status": "FILLED" if position_id else "PENDING",
        "order_type": order_type,
        "symbol": symbol,
        "side": side,
        "market_price": market,
        "reference_price": reference,
        "margin_usdt": margin_usdt,
        "leverage": leverage,
    }


async def _realize_quantity(
    db: AsyncSession,
    row: dict[str, Any],
    *,
    exit_price: float,
    quantity: float,
    reason: str,
) -> dict[str, float]:
    current_qty = _f(row["quantity_remaining"])
    if current_qty <= 0:
        return {"net_pnl": 0.0, "closed_qty": 0.0}
    qty = min(current_qty, max(0.0, quantity))
    if qty <= 0:
        return {"net_pnl": 0.0, "closed_qty": 0.0}

    fraction = qty / current_qty
    remaining_qty = max(0.0, current_qty - qty)
    current_margin = _f(row["margin_remaining"])
    closed_margin = current_margin * fraction
    remaining_margin = max(0.0, current_margin - closed_margin)
    entry = _f(row["entry_price"])
    partial_notional = qty * entry
    now = datetime.now(timezone.utc)
    pnl = calculate_trade_pnl(
        side=str(row["side"]),
        entry=entry,
        exit_price=exit_price,
        quantity=qty,
        notional=partial_notional,
        opened_at=row["opened_at"],
        closed_at=now,
    )
    fully_closed = remaining_qty <= max(1e-12, _f(row["quantity_initial"]) * 1e-8)
    await db.execute(text("""
        UPDATE manual_practice_positions
        SET quantity_remaining=:remaining_qty,
            margin_remaining=:remaining_margin,
            realized_pnl=realized_pnl+:net_pnl,
            total_costs=total_costs+:costs,
            status=CASE WHEN :fully_closed THEN 'CLOSED' ELSE status END,
            closed_at=CASE WHEN :fully_closed THEN :closed_at ELSE closed_at END,
            exit_price=CASE WHEN :fully_closed THEN :exit_price ELSE exit_price END,
            exit_reason=CASE WHEN :fully_closed THEN :reason ELSE exit_reason END
        WHERE id=:id
    """), {
        "id": row["id"],
        "remaining_qty": remaining_qty,
        "remaining_margin": remaining_margin,
        "net_pnl": pnl["net_pnl"],
        "costs": pnl["fees"] + pnl["slippage"] + pnl["funding_estimate"],
        "fully_closed": fully_closed,
        "closed_at": now,
        "exit_price": exit_price,
        "reason": reason,
    })
    await db.execute(text("""
        UPDATE manual_practice_accounts
        SET cash_balance=cash_balance+:net_pnl,
            realized_pnl=realized_pnl+:net_pnl,
            total_costs=total_costs+:costs,
            updated_at=NOW()
        WHERE id=1
    """), {
        "net_pnl": pnl["net_pnl"],
        "costs": pnl["fees"] + pnl["slippage"] + pnl["funding_estimate"],
    })
    await db.execute(text("""
        INSERT INTO manual_practice_events(position_id,event_type,price,quantity,net_pnl,note)
        VALUES(:id,:event,:price,:qty,:pnl,:note)
    """), {
        "id": row["id"],
        "event": reason,
        "price": exit_price,
        "qty": qty,
        "pnl": pnl["net_pnl"],
        "note": f"Closed {qty:.10f} of {current_qty:.10f}",
    })
    return {"net_pnl": pnl["net_pnl"], "closed_qty": qty, "remaining_qty": remaining_qty}


async def _sync_pending_orders(db: AsyncSession, prices: dict[str, float]) -> int:
    orders = [dict(r) for r in (await db.execute(text("""
        SELECT * FROM manual_practice_orders
        WHERE status='PENDING'
        ORDER BY created_at ASC
    """))).mappings().all()]
    filled = 0
    for order in orders:
        market = prices.get(str(order["symbol"])) or await _market_price(str(order["symbol"]))
        prices[str(order["symbol"])] = market
        limit_price = _f(order["limit_price"])
        hit = market <= limit_price if order["side"] == "LONG" else market >= limit_price
        if not hit:
            continue
        _, _, available = await _available_margin(db)
        if _f(order["margin_usdt"]) > available + _f(order["margin_usdt"]) + 1e-9:
            continue
        await _fill_order(db, order, limit_price)
        filled += 1
    return filled


async def _sync_open_positions(db: AsyncSession, prices: dict[str, float]) -> int:
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT * FROM manual_practice_positions
        WHERE status='OPEN'
        ORDER BY opened_at ASC
    """))).mappings().all()]
    events = 0
    for row in rows:
        symbol = str(row["symbol"])
        mark = prices.get(symbol) or await _market_price(symbol)
        prices[symbol] = mark
        side = str(row["side"])
        qty_initial = _f(row["quantity_initial"])
        qty_remaining = _f(row["quantity_remaining"])
        if qty_remaining <= 0:
            continue
        stop = _f(row["stop_loss"])
        liquidation = _f(row["liquidation_price"])
        tp1, tp2, tp3 = _f(row["tp1"]), _f(row["tp2"]), _f(row["tp3"])
        meta = _meta(row.get("metadata"))

        liquidated = liquidation > 0 and (mark <= liquidation if side == "LONG" else mark >= liquidation)
        stopped = mark <= stop if side == "LONG" else mark >= stop
        if liquidated:
            await _realize_quantity(db, row, exit_price=liquidation, quantity=qty_remaining, reason="SIM_LIQUIDATION")
            events += 1
            continue
        if stopped:
            await _realize_quantity(db, row, exit_price=stop, quantity=qty_remaining, reason="STOP")
            events += 1
            continue

        tp1_hit = bool(row.get("tp1_hit"))
        tp2_hit = bool(row.get("tp2_hit"))
        if not tp1_hit and (mark >= tp1 if side == "LONG" else mark <= tp1):
            close_qty = min(qty_remaining, qty_initial * 0.33)
            await _realize_quantity(db, row, exit_price=tp1, quantity=close_qty, reason="TP1_PARTIAL")
            await db.execute(text("UPDATE manual_practice_positions SET tp1_hit=TRUE WHERE id=:id"), {"id": row["id"]})
            if bool(meta.get("auto_be_after_tp1")):
                await db.execute(text("UPDATE manual_practice_positions SET stop_loss=entry_price WHERE id=:id"), {"id": row["id"]})
            events += 1
            previous_qty = qty_remaining
            previous_margin = _f(row["margin_remaining"])
            row["quantity_remaining"] = max(0.0, previous_qty - close_qty)
            row["margin_remaining"] = previous_margin * (row["quantity_remaining"] / previous_qty) if previous_qty > 0 else 0.0
            row["tp1_hit"] = True
            qty_remaining = _f(row["quantity_remaining"])

        if qty_remaining > 0 and not tp2_hit and (mark >= tp2 if side == "LONG" else mark <= tp2):
            close_qty = min(qty_remaining, qty_initial * 0.33)
            await _realize_quantity(db, row, exit_price=tp2, quantity=close_qty, reason="TP2_PARTIAL")
            await db.execute(text("UPDATE manual_practice_positions SET tp2_hit=TRUE WHERE id=:id"), {"id": row["id"]})
            events += 1
            previous_qty = qty_remaining
            previous_margin = _f(row["margin_remaining"])
            row["quantity_remaining"] = max(0.0, previous_qty - close_qty)
            row["margin_remaining"] = previous_margin * (row["quantity_remaining"] / previous_qty) if previous_qty > 0 else 0.0
            row["tp2_hit"] = True
            qty_remaining = _f(row["quantity_remaining"])

        if qty_remaining > 0 and (mark >= tp3 if side == "LONG" else mark <= tp3):
            await _realize_quantity(db, row, exit_price=tp3, quantity=qty_remaining, reason="TP3_FINAL")
            events += 1
    return events


async def manual_account_snapshot(db: AsyncSession) -> dict[str, Any]:
    await ensure_manual_practice_schema(db)
    prices: dict[str, float] = {}
    await _sync_pending_orders(db, prices)
    await _sync_open_positions(db, prices)
    await db.commit()

    account = dict((await db.execute(text("SELECT * FROM manual_practice_accounts WHERE id=1"))).mappings().one())
    open_rows = [dict(r) for r in (await db.execute(text("""
        SELECT * FROM manual_practice_positions WHERE status='OPEN' ORDER BY opened_at DESC
    """))).mappings().all()]
    positions: list[dict[str, Any]] = []
    unrealized = 0.0
    used_margin = 0.0
    for row in open_rows:
        symbol = str(row["symbol"])
        try:
            mark = prices.get(symbol) or await _market_price(symbol)
        except Exception:
            mark = _f(row["entry_price"])
        entry = _f(row["entry_price"])
        qty = _f(row["quantity_remaining"])
        pnl = (mark - entry) * qty if row["side"] == "LONG" else (entry - mark) * qty
        unrealized += pnl
        used_margin += _f(row["margin_remaining"])
        positions.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "side": row["side"],
            "leverage": int(row["leverage"]),
            "entry_price": entry,
            "mark_price": mark,
            "stop_loss": _f(row["stop_loss"]),
            "tp1": _f(row["tp1"]),
            "tp2": _f(row["tp2"]),
            "tp3": _f(row["tp3"]),
            "liquidation_price": _f(row["liquidation_price"]) or None,
            "quantity_initial": _f(row["quantity_initial"]),
            "quantity_remaining": qty,
            "margin_initial": _f(row["margin_initial"]),
            "margin_remaining": _f(row["margin_remaining"]),
            "unrealized_pnl": round(pnl, 6),
            "realized_pnl": _f(row["realized_pnl"]),
            "tp1_hit": bool(row["tp1_hit"]),
            "tp2_hit": bool(row["tp2_hit"]),
            "opened_at": row["opened_at"].isoformat(),
            "note": row.get("practice_note"),
        })

    pending = [dict(r) for r in (await db.execute(text("""
        SELECT id,symbol,side,order_type,status,limit_price,margin_usdt,leverage,
               stop_loss,tp1,tp2,tp3,practice_note,created_at
        FROM manual_practice_orders WHERE status='PENDING' ORDER BY created_at DESC
    """))).mappings().all()]
    reserved = sum(_f(r["margin_usdt"]) for r in pending)
    cash = _f(account["cash_balance"], STARTING_BALANCE)
    history = await manual_history(db, limit=30)
    return {
        "paper_only": True,
        "strategy_mode": MANUAL_STRATEGY,
        "starting_balance": _f(account["starting_balance"], STARTING_BALANCE),
        "cash_balance": round(cash, 6),
        "used_margin": round(used_margin, 6),
        "reserved_margin": round(reserved, 6),
        "available_margin": round(max(0.0, cash - used_margin - reserved), 6),
        "unrealized_pnl": round(unrealized, 6),
        "equity": round(cash + unrealized, 6),
        "realized_pnl": _f(account["realized_pnl"]),
        "total_costs": _f(account["total_costs"]),
        "positions": positions,
        "pending_orders": [
            {**r, "created_at": r["created_at"].isoformat() if r.get("created_at") else None}
            for r in pending
        ],
        "history": history,
        "liquidation_model": "SIMPLE_ISOLATED_EDUCATIONAL",
        "sync_mode": "ON_TERMINAL_REFRESH",
    }


async def manual_history(db: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    await ensure_manual_practice_schema(db)
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT id,symbol,side,leverage,entry_price,stop_loss,tp1,tp2,tp3,
               liquidation_price,quantity_initial,realized_pnl,total_costs,
               opened_at,closed_at,exit_price,exit_reason,practice_note
        FROM manual_practice_positions
        WHERE status='CLOSED'
        ORDER BY closed_at DESC NULLS LAST
        LIMIT :limit
    """), {"limit": max(1, min(int(limit), 200))})).mappings().all()]
    for row in rows:
        for key in ("opened_at", "closed_at"):
            if row.get(key):
                row[key] = row[key].isoformat()
        for key in ("entry_price","stop_loss","tp1","tp2","tp3","liquidation_price","quantity_initial","realized_pnl","total_costs","exit_price"):
            if row.get(key) is not None:
                row[key] = _f(row[key])
    return rows


async def close_manual_position(db: AsyncSession, position_id: int, fraction: float = 1.0) -> dict[str, Any]:
    await ensure_manual_practice_schema(db)
    row = (await db.execute(text("""
        SELECT * FROM manual_practice_positions
        WHERE id=:id AND status='OPEN' FOR UPDATE
    """), {"id": int(position_id)})).mappings().first()
    if not row:
        raise ValueError("position_not_open")
    row = dict(row)
    mark = await _market_price(str(row["symbol"]))
    fraction = min(1.0, max(0.01, _f(fraction, 1.0)))
    qty = _f(row["quantity_remaining"]) * fraction
    result = await _realize_quantity(db, row, exit_price=mark, quantity=qty, reason="MANUAL_CLOSE")
    await db.commit()
    return {"paper_only": True, "position_id": int(position_id), "exit_price": mark, **result}


async def move_manual_stop_to_be(db: AsyncSession, position_id: int) -> dict[str, Any]:
    await ensure_manual_practice_schema(db)
    result = await db.execute(text("""
        UPDATE manual_practice_positions
        SET stop_loss=entry_price
        WHERE id=:id AND status='OPEN'
        RETURNING entry_price
    """), {"id": int(position_id)})
    entry = result.scalar_one_or_none()
    if entry is None:
        raise ValueError("position_not_open")
    await db.execute(text("""
        INSERT INTO manual_practice_events(position_id,event_type,price,note)
        VALUES(:id,'MOVE_BE',:price,'Stop moved to entry')
    """), {"id": int(position_id), "price": _f(entry)})
    await db.commit()
    return {"paper_only": True, "position_id": int(position_id), "stop_loss": _f(entry)}


async def update_manual_risk(
    db: AsyncSession,
    position_id: int,
    *,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp3: float,
) -> dict[str, Any]:
    await ensure_manual_practice_schema(db)
    row = (await db.execute(text("""
        SELECT * FROM manual_practice_positions WHERE id=:id AND status='OPEN'
    """), {"id": int(position_id)})).mappings().first()
    if not row:
        raise ValueError("position_not_open")
    row = dict(row)
    _validate_geometry(str(row["side"]), _f(row["entry_price"]), _f(stop_loss), _f(tp1), _f(tp2), _f(tp3), allow_be=True)
    await db.execute(text("""
        UPDATE manual_practice_positions
        SET stop_loss=:stop,tp1=:tp1,tp2=:tp2,tp3=:tp3
        WHERE id=:id
    """), {"id": int(position_id), "stop": _f(stop_loss), "tp1": _f(tp1), "tp2": _f(tp2), "tp3": _f(tp3)})
    await db.execute(text("""
        INSERT INTO manual_practice_events(position_id,event_type,price,note)
        VALUES(:id,'EDIT_PLAN',:price,:note)
    """), {"id": int(position_id), "price": _f(row["entry_price"]), "note": f"SL {_f(stop_loss)} | TP {_f(tp1)}/{_f(tp2)}/{_f(tp3)}"})
    await db.commit()
    return {"paper_only": True, "position_id": int(position_id), "stop_loss": _f(stop_loss), "tp1": _f(tp1), "tp2": _f(tp2), "tp3": _f(tp3)}


async def cancel_manual_order(db: AsyncSession, order_id: int) -> dict[str, Any]:
    await ensure_manual_practice_schema(db)
    result = await db.execute(text("""
        UPDATE manual_practice_orders
        SET status='CANCELED',canceled_at=NOW(),cancel_reason='USER_CANCEL'
        WHERE id=:id AND status='PENDING'
        RETURNING id
    """), {"id": int(order_id)})
    value = result.scalar_one_or_none()
    if value is None:
        raise ValueError("order_not_pending")
    await db.commit()
    return {"paper_only": True, "order_id": int(order_id), "status": "CANCELED"}


async def reset_manual_practice_account(db: AsyncSession) -> dict[str, Any]:
    await ensure_manual_practice_schema(db)
    await db.execute(text("DELETE FROM manual_practice_events"))
    await db.execute(text("DELETE FROM manual_practice_positions"))
    await db.execute(text("DELETE FROM manual_practice_orders"))
    await db.execute(text("""
        UPDATE manual_practice_accounts
        SET starting_balance=:balance,cash_balance=:balance,realized_pnl=0,total_costs=0,updated_at=NOW()
        WHERE id=1
    """), {"balance": STARTING_BALANCE})
    await db.commit()
    return {"paper_only": True, "starting_balance": STARTING_BALANCE, "reset": True}
