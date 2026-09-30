from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client
from app.services.paper_portfolio import (
    MAX_PAPER_LEVERAGE,
    STARTING_BALANCE,
    acquire_paper_open_lock,
    calculate_trade_pnl,
    ensure_paper_schema,
)

MANUAL_STRATEGY = "MANUAL_PRACTICE"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def _market_price(symbol: str) -> float:
    payload = await binance_client.price(symbol)
    price = _f(payload.get("price") if isinstance(payload, dict) else payload)
    if price <= 0:
        raise ValueError("market_price_unavailable")
    return price


async def manual_account_snapshot(db: AsyncSession) -> dict[str, Any]:
    await ensure_paper_schema(db)
    account = dict((await db.execute(
        text("SELECT * FROM paper_accounts WHERE id=1")
    )).mappings().one())
    open_rows = [dict(row) for row in (await db.execute(text("""
        SELECT id, symbol, side, leverage, entry_price, stop_loss, take_profit,
               quantity, notional, margin_used, risk_usdt, opened_at, metadata
        FROM paper_positions
        WHERE status='OPEN'
          AND COALESCE(metadata->>'strategy_mode','')=:strategy
        ORDER BY opened_at DESC
    """), {"strategy": MANUAL_STRATEGY})).mappings().all()]

    positions: list[dict[str, Any]] = []
    unrealized = 0.0
    used_margin = 0.0
    for row in open_rows:
        try:
            mark = await _market_price(str(row["symbol"]))
        except Exception:
            mark = _f(row["entry_price"])
        entry = _f(row["entry_price"])
        qty = _f(row["quantity"])
        side = str(row["side"])
        pnl = (mark - entry) * qty if side == "LONG" else (entry - mark) * qty
        unrealized += pnl
        used_margin += _f(row["margin_used"])
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        positions.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "side": side,
            "leverage": int(row["leverage"]),
            "entry_price": entry,
            "mark_price": mark,
            "stop_loss": _f(row["stop_loss"]),
            "take_profit": _f(row["take_profit"]),
            "quantity": qty,
            "notional": _f(row["notional"]),
            "margin_used": _f(row["margin_used"]),
            "risk_usdt": _f(row["risk_usdt"]),
            "unrealized_pnl": round(pnl, 6),
            "opened_at": row["opened_at"].isoformat(),
            "note": metadata.get("practice_note"),
        })

    cash = _f(account.get("cash_balance"), STARTING_BALANCE)
    return {
        "paper_only": True,
        "strategy_mode": MANUAL_STRATEGY,
        "starting_balance": _f(account.get("starting_balance"), STARTING_BALANCE),
        "cash_balance": round(cash, 6),
        "used_margin": round(used_margin, 6),
        "available_margin": round(max(0.0, cash - used_margin), 6),
        "unrealized_pnl": round(unrealized, 6),
        "equity": round(cash + unrealized, 6),
        "realized_pnl": _f(account.get("realized_pnl")),
        "positions": positions,
    }


async def open_manual_position(
    db: AsyncSession,
    *,
    symbol: str,
    side: str,
    margin_usdt: float,
    leverage: int,
    stop_loss: float,
    take_profit: float,
    practice_note: str | None = None,
) -> dict[str, Any]:
    await ensure_paper_schema(db)
    await acquire_paper_open_lock(db)

    side = str(side or "").upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("invalid_side")
    leverage = max(1, min(MAX_PAPER_LEVERAGE, int(leverage)))
    margin_usdt = _f(margin_usdt)
    stop_loss = _f(stop_loss)
    take_profit = _f(take_profit)
    if margin_usdt <= 0:
        raise ValueError("invalid_margin")

    entry = await _market_price(symbol)
    if side == "LONG" and not (0 < stop_loss < entry < take_profit):
        raise ValueError("invalid_long_geometry")
    if side == "SHORT" and not (0 < take_profit < entry < stop_loss):
        raise ValueError("invalid_short_geometry")

    account = dict((await db.execute(
        text("SELECT * FROM paper_accounts WHERE id=1 FOR UPDATE")
    )).mappings().one())
    used_margin = _f((await db.execute(text("""
        SELECT COALESCE(SUM(margin_used),0)
        FROM paper_positions
        WHERE status='OPEN'
    """))).scalar_one())
    cash = _f(account.get("cash_balance"), STARTING_BALANCE)
    available = max(0.0, cash - used_margin)
    if margin_usdt > available + 1e-9:
        raise ValueError("insufficient_paper_margin")

    notional = margin_usdt * leverage
    quantity = notional / entry
    risk_usdt = abs(entry - stop_loss) * quantity
    metadata = {
        "strategy_mode": MANUAL_STRATEGY,
        "manual_practice": True,
        "practice_note": (practice_note or "")[:300],
        "paper_only": True,
        "opened_from": "EXPLODEX_TERMINAL",
    }

    result = await db.execute(text("""
        INSERT INTO paper_positions (
            signal_id, symbol, side, grade, fingerprint_score, leverage,
            entry_price, stop_loss, take_profit, quantity, notional,
            margin_used, risk_usdt, opened_at, metadata
        ) VALUES (
            NULL, :symbol, :side, 'MANUAL', 0, :leverage,
            :entry_price, :stop_loss, :take_profit, :quantity, :notional,
            :margin_used, :risk_usdt, :opened_at, CAST(:metadata AS JSONB)
        )
        RETURNING id
    """), {
        "symbol": symbol,
        "side": side,
        "leverage": leverage,
        "entry_price": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "quantity": quantity,
        "notional": notional,
        "margin_used": margin_usdt,
        "risk_usdt": risk_usdt,
        "opened_at": datetime.now(timezone.utc),
        "metadata": json.dumps(metadata),
    })
    position_id = int(result.scalar_one())
    await db.commit()

    return {
        "paper_only": True,
        "position_id": position_id,
        "symbol": symbol,
        "side": side,
        "entry_price": round(entry, 12),
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "leverage": leverage,
        "margin_usdt": round(margin_usdt, 6),
        "notional": round(notional, 6),
        "quantity": round(quantity, 10),
        "risk_usdt": round(risk_usdt, 6),
        "available_margin_before": round(available, 6),
    }


async def close_manual_position(db: AsyncSession, position_id: int) -> dict[str, Any]:
    await ensure_paper_schema(db)
    row = (await db.execute(text("""
        SELECT id, symbol, side, entry_price, quantity, notional, opened_at, metadata
        FROM paper_positions
        WHERE id=:id AND status='OPEN'
        FOR UPDATE
    """), {"id": int(position_id)})).mappings().first()
    if not row:
        raise ValueError("position_not_open")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    if str(metadata.get("strategy_mode") or "") != MANUAL_STRATEGY:
        raise ValueError("not_manual_practice_position")

    exit_price = await _market_price(str(row["symbol"]))
    now = datetime.now(timezone.utc)
    pnl = calculate_trade_pnl(
        side=str(row["side"]),
        entry=_f(row["entry_price"]),
        exit_price=exit_price,
        quantity=_f(row["quantity"]),
        notional=_f(row["notional"]),
        opened_at=row["opened_at"],
        closed_at=now,
    )

    await db.execute(text("""
        UPDATE paper_positions
        SET status='CLOSED', closed_at=:closed_at, exit_price=:exit_price,
            exit_reason='MANUAL_CLOSE', gross_pnl=:gross_pnl, net_pnl=:net_pnl,
            fees=:fees, slippage=:slippage, funding_estimate=:funding_estimate
        WHERE id=:id
    """), {
        "id": int(position_id),
        "closed_at": now,
        "exit_price": exit_price,
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
    await db.commit()

    return {
        "paper_only": True,
        "position_id": int(position_id),
        "exit_price": round(exit_price, 12),
        **pnl,
    }
