from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client
from app.services.paper_portfolio import (
    FUNDING_ESTIMATE_8H,
    SLIPPAGE_RATE,
    TAKER_FEE_RATE,
    calculate_trade_pnl,
)

STARTING_BALANCE = 1000.0
MAX_LEVERAGE = 20
MAX_MARGIN_SHARE = 0.95


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_session_id(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 80:
        raise ValueError("invalid practice session")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if any(ch not in allowed for ch in value):
        raise ValueError("invalid practice session")
    return value


def _clean_symbol(value: str) -> str:
    value = str(value or "").upper().strip().replace("/", "")
    if not value.endswith("USDT"):
        value += "USDT"
    base = value[:-4]
    if not base or not base.isalnum():
        raise ValueError("invalid symbol")
    return value


async def ensure_practice_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS practice_accounts (
            session_id VARCHAR(80) PRIMARY KEY,
            starting_balance NUMERIC(18,6) NOT NULL DEFAULT 1000,
            cash_balance NUMERIC(18,6) NOT NULL DEFAULT 1000,
            realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
            total_costs NUMERIC(18,6) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS practice_positions (
            id BIGSERIAL PRIMARY KEY,
            session_id VARCHAR(80) NOT NULL REFERENCES practice_accounts(session_id) ON DELETE CASCADE,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
            timeframe VARCHAR(16),
            pattern VARCHAR(80),
            entry_price NUMERIC(30,12) NOT NULL,
            exit_price NUMERIC(30,12),
            stop_loss NUMERIC(30,12) NOT NULL,
            take_profit NUMERIC(30,12) NOT NULL,
            tp2 NUMERIC(30,12),
            tp3 NUMERIC(30,12),
            leverage INTEGER NOT NULL,
            margin_used NUMERIC(24,8) NOT NULL,
            quantity NUMERIC(30,12) NOT NULL,
            notional NUMERIC(24,8) NOT NULL,
            risk_usdt NUMERIC(24,8) NOT NULL,
            gross_pnl NUMERIC(24,8),
            net_pnl NUMERIC(24,8),
            fees NUMERIC(24,8),
            slippage NUMERIC(24,8),
            funding_estimate NUMERIC(24,8),
            close_reason VARCHAR(64),
            note TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            closed_at TIMESTAMPTZ
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_practice_positions_session_status "
        "ON practice_positions(session_id, status, opened_at DESC)"
    ))
    await db.commit()


async def ensure_practice_account(db: AsyncSession, session_id: str) -> None:
    session_id = _clean_session_id(session_id)
    await ensure_practice_schema(db)
    await db.execute(text("""
        INSERT INTO practice_accounts (session_id, starting_balance, cash_balance)
        VALUES (:session_id, :balance, :balance)
        ON CONFLICT (session_id) DO NOTHING
    """), {"session_id": session_id, "balance": STARTING_BALANCE})
    await db.commit()


async def _latest_price(symbol: str) -> float:
    payload = await binance_client.price(symbol)
    return _f(payload.get("price"))


async def _mark_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    symbols = sorted({str(row.get("symbol") or "") for row in rows if row.get("symbol")})
    marks: dict[str, float] = {}
    for symbol in symbols:
        try:
            marks[symbol] = await _latest_price(symbol)
        except Exception:
            marks[symbol] = 0.0

    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        entry = _f(row.get("entry_price"))
        mark = _f(marks.get(str(row.get("symbol") or "")), entry) or entry
        qty = _f(row.get("quantity"))
        side = str(row.get("side") or "").upper()
        gross = (mark - entry) * qty if side == "LONG" else (entry - mark) * qty
        margin = _f(row.get("margin_used"))
        out.append({
            **row,
            "id": int(row["id"]),
            "entry_price": entry,
            "mark_price": mark,
            "stop_loss": _f(row.get("stop_loss")),
            "take_profit": _f(row.get("take_profit")),
            "tp2": _f(row.get("tp2")) if row.get("tp2") is not None else None,
            "tp3": _f(row.get("tp3")) if row.get("tp3") is not None else None,
            "margin_used": margin,
            "quantity": qty,
            "notional": _f(row.get("notional")),
            "risk_usdt": _f(row.get("risk_usdt")),
            "unrealized_pnl": round(gross, 6),
            "roi_on_margin_pct": round(gross / margin * 100.0, 4) if margin > 0 else 0.0,
            "opened_at": row["opened_at"].isoformat() if row.get("opened_at") else None,
        })
    return out


async def practice_summary(db: AsyncSession, session_id: str) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    account = dict((await db.execute(
        text("SELECT * FROM practice_accounts WHERE session_id=:session_id"),
        {"session_id": session_id},
    )).mappings().one())

    rows = [dict(r) for r in (await db.execute(text("""
        SELECT * FROM practice_positions
        WHERE session_id=:session_id AND status='OPEN'
        ORDER BY opened_at DESC
    """), {"session_id": session_id})).mappings().all()]
    open_positions = await _mark_positions(rows)
    reserved_margin = sum(_f(row.get("margin_used")) for row in open_positions)
    unrealized = sum(_f(row.get("unrealized_pnl")) for row in open_positions)
    cash = _f(account.get("cash_balance"))
    equity = cash + unrealized
    available_margin = max(0.0, cash - reserved_margin)

    stats = dict((await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE status='CLOSED') AS closed_trades,
            COUNT(*) FILTER (WHERE status='CLOSED' AND net_pnl > 0) AS winners,
            COUNT(*) FILTER (WHERE status='CLOSED' AND net_pnl <= 0) AS losers
        FROM practice_positions
        WHERE session_id=:session_id
    """), {"session_id": session_id})).mappings().one())
    closed = int(stats.get("closed_trades") or 0)
    winners = int(stats.get("winners") or 0)

    return {
        "paper_only": True,
        "practice_mode": True,
        "session_id": session_id,
        "starting_balance": _f(account.get("starting_balance"), STARTING_BALANCE),
        "cash_balance": round(cash, 6),
        "reserved_margin": round(reserved_margin, 6),
        "available_margin": round(available_margin, 6),
        "unrealized_pnl": round(unrealized, 6),
        "equity": round(equity, 6),
        "realized_pnl": _f(account.get("realized_pnl")),
        "total_costs": _f(account.get("total_costs")),
        "open_positions": open_positions,
        "closed_trades": closed,
        "winners": winners,
        "losers": int(stats.get("losers") or 0),
        "win_rate_pct": round(winners / closed * 100.0, 2) if closed else None,
        "assumptions": {
            "max_leverage": MAX_LEVERAGE,
            "taker_fee_pct_per_side": TAKER_FEE_RATE * 100.0,
            "slippage_pct_per_side": SLIPPAGE_RATE * 100.0,
            "funding_estimate_pct_per_8h": FUNDING_ESTIMATE_8H * 100.0,
        },
    }


async def open_practice_trade(
    db: AsyncSession,
    *,
    session_id: str,
    symbol: str,
    side: str,
    margin: float,
    leverage: int,
    stop_loss: float,
    take_profit: float,
    tp2: float | None = None,
    tp3: float | None = None,
    timeframe: str | None = None,
    pattern: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    symbol = _clean_symbol(symbol)
    side = str(side or "").upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")

    leverage = max(1, min(MAX_LEVERAGE, int(leverage)))
    margin = _f(margin)
    stop_loss = _f(stop_loss)
    take_profit = _f(take_profit)
    if margin <= 0 or stop_loss <= 0 or take_profit <= 0:
        raise ValueError("margin, stop and take profit must be positive")

    await ensure_practice_account(db, session_id)
    summary = await practice_summary(db, session_id)
    available = _f(summary.get("available_margin"))
    if margin > available or margin > _f(summary.get("cash_balance")) * MAX_MARGIN_SHARE:
        raise ValueError("insufficient practice margin")

    entry = await _latest_price(symbol)
    if entry <= 0:
        raise ValueError("market price unavailable")

    valid_geometry = (
        side == "LONG" and stop_loss < entry < take_profit
    ) or (
        side == "SHORT" and take_profit < entry < stop_loss
    )
    if not valid_geometry:
        raise ValueError("invalid LONG/SHORT stop-target geometry")

    notional = margin * leverage
    quantity = notional / entry
    risk_usdt = abs(entry - stop_loss) * quantity
    risk_pct = risk_usdt / max(_f(summary.get("equity"), STARTING_BALANCE), 1e-9) * 100.0
    warnings: list[str] = []
    if risk_pct > 0.5:
        warnings.append("risk_above_beginner_practice_range_0_25_to_0_5_pct")
    if leverage > 10:
        warnings.append("high_leverage_for_practice")

    metadata = {
        "source": "MANUAL_PRACTICE_TERMINAL",
        "practice_only": True,
        "risk_pct_of_equity": round(risk_pct, 6),
    }
    result = await db.execute(text("""
        INSERT INTO practice_positions (
            session_id, symbol, side, timeframe, pattern, entry_price, stop_loss,
            take_profit, tp2, tp3, leverage, margin_used, quantity, notional,
            risk_usdt, note, metadata
        ) VALUES (
            :session_id, :symbol, :side, :timeframe, :pattern, :entry_price, :stop_loss,
            :take_profit, :tp2, :tp3, :leverage, :margin_used, :quantity, :notional,
            :risk_usdt, :note, CAST(:metadata AS JSONB)
        ) RETURNING id
    """), {
        "session_id": session_id,
        "symbol": symbol,
        "side": side,
        "timeframe": (timeframe or "")[:16] or None,
        "pattern": (pattern or "")[:80] or None,
        "entry_price": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "tp2": _f(tp2) if tp2 is not None else None,
        "tp3": _f(tp3) if tp3 is not None else None,
        "leverage": leverage,
        "margin_used": margin,
        "quantity": quantity,
        "notional": notional,
        "risk_usdt": risk_usdt,
        "note": (note or "")[:1000] or None,
        "metadata": json.dumps(metadata),
    })
    trade_id = int(result.scalar_one())
    await db.commit()
    return {
        "paper_only": True,
        "practice_mode": True,
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "entry_price": entry,
        "margin_used": round(margin, 6),
        "leverage": leverage,
        "quantity": round(quantity, 10),
        "notional": round(notional, 6),
        "risk_usdt": round(risk_usdt, 6),
        "risk_pct_of_equity": round(risk_pct, 4),
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "warnings": warnings,
    }


async def close_practice_trade(
    db: AsyncSession,
    *,
    session_id: str,
    trade_id: int,
    reason: str = "USER_CLOSE",
) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    row = (await db.execute(text("""
        SELECT * FROM practice_positions
        WHERE id=:trade_id AND session_id=:session_id AND status='OPEN'
        FOR UPDATE
    """), {"trade_id": trade_id, "session_id": session_id})).mappings().first()
    if not row:
        raise ValueError("practice trade not found or already closed")

    position = dict(row)
    exit_price = await _latest_price(str(position["symbol"]))
    closed_at = datetime.now(timezone.utc)
    pnl = calculate_trade_pnl(
        side=str(position["side"]),
        entry=_f(position["entry_price"]),
        exit_price=exit_price,
        quantity=_f(position["quantity"]),
        notional=_f(position["notional"]),
        opened_at=position["opened_at"],
        closed_at=closed_at,
    )
    total_costs = pnl["fees"] + pnl["slippage"] + pnl["funding_estimate"]
    await db.execute(text("""
        UPDATE practice_positions
        SET status='CLOSED', exit_price=:exit_price, closed_at=:closed_at,
            gross_pnl=:gross_pnl, net_pnl=:net_pnl, fees=:fees,
            slippage=:slippage, funding_estimate=:funding_estimate,
            close_reason=:close_reason
        WHERE id=:trade_id AND session_id=:session_id
    """), {
        "exit_price": exit_price,
        "closed_at": closed_at,
        "gross_pnl": pnl["gross_pnl"],
        "net_pnl": pnl["net_pnl"],
        "fees": pnl["fees"],
        "slippage": pnl["slippage"],
        "funding_estimate": pnl["funding_estimate"],
        "close_reason": str(reason or "USER_CLOSE")[:64],
        "trade_id": trade_id,
        "session_id": session_id,
    })
    await db.execute(text("""
        UPDATE practice_accounts
        SET cash_balance=cash_balance + :net_pnl,
            realized_pnl=realized_pnl + :net_pnl,
            total_costs=total_costs + :costs,
            updated_at=NOW()
        WHERE session_id=:session_id
    """), {"net_pnl": pnl["net_pnl"], "costs": total_costs, "session_id": session_id})
    await db.commit()
    return {
        "paper_only": True,
        "practice_mode": True,
        "trade_id": trade_id,
        "exit_price": exit_price,
        "close_reason": str(reason or "USER_CLOSE")[:64],
        **pnl,
    }


async def practice_history(db: AsyncSession, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT * FROM practice_positions
        WHERE session_id=:session_id AND status='CLOSED'
        ORDER BY closed_at DESC
        LIMIT :limit
    """), {"session_id": session_id, "limit": max(1, min(int(limit), 500))})).mappings().all()]
    for row in rows:
        for key in ("opened_at", "closed_at"):
            if row.get(key) is not None:
                row[key] = row[key].isoformat()
        for key in (
            "entry_price", "exit_price", "stop_loss", "take_profit", "tp2", "tp3",
            "margin_used", "quantity", "notional", "risk_usdt", "gross_pnl",
            "net_pnl", "fees", "slippage", "funding_estimate",
        ):
            if row.get(key) is not None:
                row[key] = _f(row[key])
    return rows


async def reset_practice_account(db: AsyncSession, session_id: str) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    deleted = await db.execute(text(
        "DELETE FROM practice_positions WHERE session_id=:session_id"
    ), {"session_id": session_id})
    await db.execute(text("""
        UPDATE practice_accounts
        SET starting_balance=:balance, cash_balance=:balance,
            realized_pnl=0, total_costs=0, updated_at=NOW()
        WHERE session_id=:session_id
    """), {"balance": STARTING_BALANCE, "session_id": session_id})
    await db.commit()
    return {
        "paper_only": True,
        "practice_mode": True,
        "reset": True,
        "positions_deleted": int(deleted.rowcount or 0),
        "starting_balance": STARTING_BALANCE,
    }
