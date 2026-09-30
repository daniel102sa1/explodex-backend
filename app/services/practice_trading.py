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
MAINTENANCE_MARGIN_RATE = 0.005


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


def estimated_liquidation_price(entry: float, side: str, leverage: int) -> float:
    """Simple isolated-margin training estimate, not an exchange liquidation engine."""
    entry = _f(entry)
    leverage = max(1, min(MAX_LEVERAGE, int(leverage)))
    if entry <= 0:
        return 0.0
    if str(side).upper() == "LONG":
        return max(0.0, entry * (1.0 - 1.0 / leverage + MAINTENANCE_MARGIN_RATE))
    return entry * (1.0 + 1.0 / leverage - MAINTENANCE_MARGIN_RATE)


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
    # Additive migrations keep existing browser practice accounts intact.
    migrations = [
        "ALTER TABLE practice_positions ADD COLUMN IF NOT EXISTS initial_quantity NUMERIC(30,12)",
        "ALTER TABLE practice_positions ADD COLUMN IF NOT EXISTS initial_risk_usdt NUMERIC(24,8)",
        "ALTER TABLE practice_positions ADD COLUMN IF NOT EXISTS partial_realized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0",
        "ALTER TABLE practice_positions ADD COLUMN IF NOT EXISTS liquidation_price NUMERIC(30,12)",
        "ALTER TABLE practice_positions ADD COLUMN IF NOT EXISTS tp1_hit BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE practice_positions ADD COLUMN IF NOT EXISTS tp2_hit BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE practice_positions ADD COLUMN IF NOT EXISTS tp3_hit BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE practice_positions ADD COLUMN IF NOT EXISTS moved_to_be BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE practice_positions ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ",
    ]
    for ddl in migrations:
        await db.execute(text(ddl))
    await db.execute(text("""
        UPDATE practice_positions
        SET initial_quantity=COALESCE(initial_quantity, quantity),
            initial_risk_usdt=COALESCE(initial_risk_usdt, risk_usdt),
            last_synced_at=COALESCE(last_synced_at, opened_at)
        WHERE initial_quantity IS NULL OR initial_risk_usdt IS NULL OR last_synced_at IS NULL
    """))

    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS practice_orders (
            id BIGSERIAL PRIMARY KEY,
            session_id VARCHAR(80) NOT NULL REFERENCES practice_accounts(session_id) ON DELETE CASCADE,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            order_type VARCHAR(16) NOT NULL DEFAULT 'LIMIT',
            status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
            limit_price NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            take_profit NUMERIC(30,12) NOT NULL,
            tp2 NUMERIC(30,12),
            tp3 NUMERIC(30,12),
            leverage INTEGER NOT NULL,
            margin_used NUMERIC(24,8) NOT NULL,
            timeframe VARCHAR(16),
            pattern VARCHAR(80),
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            filled_at TIMESTAMPTZ,
            canceled_at TIMESTAMPTZ,
            position_id BIGINT REFERENCES practice_positions(id) ON DELETE SET NULL
        )
    """))
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS practice_trade_events (
            id BIGSERIAL PRIMARY KEY,
            session_id VARCHAR(80) NOT NULL REFERENCES practice_accounts(session_id) ON DELETE CASCADE,
            position_id BIGINT REFERENCES practice_positions(id) ON DELETE CASCADE,
            event_type VARCHAR(32) NOT NULL,
            price NUMERIC(30,12),
            quantity NUMERIC(30,12),
            net_pnl NUMERIC(24,8),
            message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_practice_positions_session_status "
        "ON practice_positions(session_id, status, opened_at DESC)"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_practice_orders_session_status "
        "ON practice_orders(session_id, status, created_at DESC)"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_practice_events_position_time "
        "ON practice_trade_events(position_id, created_at DESC)"
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


async def _record_event(
    db: AsyncSession,
    *,
    session_id: str,
    position_id: int | None,
    event_type: str,
    price: float | None = None,
    quantity: float | None = None,
    net_pnl: float | None = None,
    message: str | None = None,
) -> None:
    await db.execute(text("""
        INSERT INTO practice_trade_events (
            session_id, position_id, event_type, price, quantity, net_pnl, message
        ) VALUES (
            :session_id, :position_id, :event_type, :price, :quantity, :net_pnl, :message
        )
    """), {
        "session_id": session_id,
        "position_id": position_id,
        "event_type": str(event_type)[:32],
        "price": price,
        "quantity": quantity,
        "net_pnl": net_pnl,
        "message": (message or "")[:500] or None,
    })


def _validate_geometry(
    *,
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    tp2: float | None = None,
    tp3: float | None = None,
    allow_break_even: bool = False,
) -> None:
    side = str(side).upper()
    entry = _f(entry)
    stop_loss = _f(stop_loss)
    take_profit = _f(take_profit)
    tp2f = _f(tp2) if tp2 is not None else None
    tp3f = _f(tp3) if tp3 is not None else None
    if side == "LONG":
        targets = [x for x in (take_profit, tp2f, tp3f) if x is not None]
        stop_ok = 0 < stop_loss <= entry if allow_break_even else 0 < stop_loss < entry
        if not (stop_ok and entry < take_profit):
            raise ValueError("invalid LONG stop-target geometry")
        if any(target <= entry for target in targets):
            raise ValueError("LONG targets must be above entry")
        if targets != sorted(targets):
            raise ValueError("LONG TP1/TP2/TP3 must be ascending")
    elif side == "SHORT":
        targets = [x for x in (take_profit, tp2f, tp3f) if x is not None]
        stop_ok = stop_loss >= entry if allow_break_even else stop_loss > entry
        if not (0 < take_profit < entry and stop_ok):
            raise ValueError("invalid SHORT stop-target geometry")
        if any(target >= entry for target in targets):
            raise ValueError("SHORT targets must be below entry")
        if targets != sorted(targets, reverse=True):
            raise ValueError("SHORT TP1/TP2/TP3 must be descending")
    else:
        raise ValueError("side must be LONG or SHORT")


async def _account_available_margin(db: AsyncSession, session_id: str) -> tuple[float, float]:
    account = dict((await db.execute(
        text("SELECT * FROM practice_accounts WHERE session_id=:session_id"),
        {"session_id": session_id},
    )).mappings().one())
    open_margin = _f((await db.execute(text("""
        SELECT COALESCE(SUM(margin_used),0)
        FROM practice_positions
        WHERE session_id=:session_id AND status='OPEN'
    """), {"session_id": session_id})).scalar_one())
    pending_margin = _f((await db.execute(text("""
        SELECT COALESCE(SUM(margin_used),0)
        FROM practice_orders
        WHERE session_id=:session_id AND status='PENDING'
    """), {"session_id": session_id})).scalar_one())
    cash = _f(account.get("cash_balance"), STARTING_BALANCE)
    return cash, max(0.0, cash - open_margin - pending_margin)


async def _insert_position(
    db: AsyncSession,
    *,
    session_id: str,
    symbol: str,
    side: str,
    entry: float,
    margin: float,
    leverage: int,
    stop_loss: float,
    take_profit: float,
    tp2: float | None,
    tp3: float | None,
    timeframe: str | None,
    pattern: str | None,
    note: str | None,
    source: str,
) -> dict[str, Any]:
    _validate_geometry(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        tp2=tp2,
        tp3=tp3,
    )
    notional = margin * leverage
    quantity = notional / entry
    risk_usdt = abs(entry - stop_loss) * quantity
    liq = estimated_liquidation_price(entry, side, leverage)
    metadata = {
        "source": source,
        "practice_only": True,
        "maintenance_margin_rate": MAINTENANCE_MARGIN_RATE,
        "liquidation_is_estimate": True,
    }
    result = await db.execute(text("""
        INSERT INTO practice_positions (
            session_id, symbol, side, timeframe, pattern, entry_price, stop_loss,
            take_profit, tp2, tp3, leverage, margin_used, quantity, initial_quantity,
            notional, risk_usdt, initial_risk_usdt, liquidation_price, note, metadata, last_synced_at
        ) VALUES (
            :session_id, :symbol, :side, :timeframe, :pattern, :entry_price, :stop_loss,
            :take_profit, :tp2, :tp3, :leverage, :margin_used, :quantity, :quantity,
            :notional, :risk_usdt, :risk_usdt, :liquidation_price, :note, CAST(:metadata AS JSONB), NOW()
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
        "liquidation_price": liq,
        "note": (note or "")[:1000] or None,
        "metadata": json.dumps(metadata),
    })
    position_id = int(result.scalar_one())
    await _record_event(
        db,
        session_id=session_id,
        position_id=position_id,
        event_type="OPEN",
        price=entry,
        quantity=quantity,
        message=f"{side} {leverage}x PAPER only",
    )
    return {
        "position_id": position_id,
        "entry_price": entry,
        "quantity": quantity,
        "notional": notional,
        "risk_usdt": risk_usdt,
        "liquidation_price": liq,
    }


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
        liq = _f(row.get("liquidation_price"))
        liq_distance = abs(mark - liq) / mark * 100.0 if mark > 0 and liq > 0 else None
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
            "initial_quantity": _f(row.get("initial_quantity"), qty),
            "notional": _f(row.get("notional")),
            "risk_usdt": _f(row.get("risk_usdt")),
            "initial_risk_usdt": _f(row.get("initial_risk_usdt"), _f(row.get("risk_usdt"))),
            "liquidation_price": liq,
            "liquidation_distance_pct": round(liq_distance, 4) if liq_distance is not None else None,
            "unrealized_pnl": round(gross, 6),
            "partial_realized_pnl": _f(row.get("partial_realized_pnl")),
            "roi_on_margin_pct": round(gross / margin * 100.0, 4) if margin > 0 else 0.0,
            "opened_at": row["opened_at"].isoformat() if row.get("opened_at") else None,
            "last_synced_at": row["last_synced_at"].isoformat() if row.get("last_synced_at") else None,
        })
    return out


async def _performance_metrics(db: AsyncSession, session_id: str) -> dict[str, Any]:
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT net_pnl, initial_risk_usdt, closed_at
        FROM practice_positions
        WHERE session_id=:session_id AND status='CLOSED' AND net_pnl IS NOT NULL
        ORDER BY closed_at ASC
    """), {"session_id": session_id})).mappings().all()]
    if not rows:
        return {
            "expectancy_usdt": 0.0,
            "profit_factor": None,
            "average_r": None,
            "max_drawdown_pct": 0.0,
        }
    pnls = [_f(r.get("net_pnl")) for r in rows]
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    r_values = [
        _f(r.get("net_pnl")) / _f(r.get("initial_risk_usdt"))
        for r in rows if _f(r.get("initial_risk_usdt")) > 0
    ]
    equity = STARTING_BALANCE
    peak = equity
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    return {
        "expectancy_usdt": round(sum(pnls) / len(pnls), 6),
        "profit_factor": round(wins / losses, 4) if losses > 0 else None,
        "average_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
        "max_drawdown_pct": round(max_dd, 4),
    }


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
    pending_orders = [dict(r) for r in (await db.execute(text("""
        SELECT * FROM practice_orders
        WHERE session_id=:session_id AND status='PENDING'
        ORDER BY created_at DESC
    """), {"session_id": session_id})).mappings().all()]
    for order in pending_orders:
        order["id"] = int(order["id"])
        order["limit_price"] = _f(order.get("limit_price"))
        order["stop_loss"] = _f(order.get("stop_loss"))
        order["take_profit"] = _f(order.get("take_profit"))
        order["tp2"] = _f(order.get("tp2")) if order.get("tp2") is not None else None
        order["tp3"] = _f(order.get("tp3")) if order.get("tp3") is not None else None
        order["margin_used"] = _f(order.get("margin_used"))
        if order.get("created_at"):
            order["created_at"] = order["created_at"].isoformat()

    reserved_margin = sum(_f(row.get("margin_used")) for row in open_positions)
    pending_margin = sum(_f(row.get("margin_used")) for row in pending_orders)
    unrealized = sum(_f(row.get("unrealized_pnl")) for row in open_positions)
    cash = _f(account.get("cash_balance"))
    equity = cash + unrealized
    available_margin = max(0.0, cash - reserved_margin - pending_margin)

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
    performance = await _performance_metrics(db, session_id)

    return {
        "paper_only": True,
        "practice_mode": True,
        "session_id": session_id,
        "starting_balance": _f(account.get("starting_balance"), STARTING_BALANCE),
        "cash_balance": round(cash, 6),
        "reserved_margin": round(reserved_margin, 6),
        "pending_margin": round(pending_margin, 6),
        "available_margin": round(available_margin, 6),
        "unrealized_pnl": round(unrealized, 6),
        "equity": round(equity, 6),
        "realized_pnl": _f(account.get("realized_pnl")),
        "total_costs": _f(account.get("total_costs")),
        "open_positions": open_positions,
        "pending_orders": pending_orders,
        "closed_trades": closed,
        "winners": winners,
        "losers": int(stats.get("losers") or 0),
        "win_rate_pct": round(winners / closed * 100.0, 2) if closed else None,
        "performance": performance,
        "assumptions": {
            "max_leverage": MAX_LEVERAGE,
            "taker_fee_pct_per_side": TAKER_FEE_RATE * 100.0,
            "slippage_pct_per_side": SLIPPAGE_RATE * 100.0,
            "funding_estimate_pct_per_8h": FUNDING_ESTIMATE_8H * 100.0,
            "maintenance_margin_rate_pct": MAINTENANCE_MARGIN_RATE * 100.0,
            "liquidation_is_estimate": True,
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
    leverage = max(1, min(MAX_LEVERAGE, int(leverage)))
    margin = _f(margin)
    stop_loss = _f(stop_loss)
    take_profit = _f(take_profit)
    if margin <= 0 or stop_loss <= 0 or take_profit <= 0:
        raise ValueError("margin, stop and take profit must be positive")

    await ensure_practice_account(db, session_id)
    cash, available = await _account_available_margin(db, session_id)
    if margin > available or margin > cash * MAX_MARGIN_SHARE:
        raise ValueError("insufficient practice margin")

    entry = await _latest_price(symbol)
    if entry <= 0:
        raise ValueError("market price unavailable")
    _validate_geometry(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        tp2=tp2,
        tp3=tp3,
    )

    created = await _insert_position(
        db,
        session_id=session_id,
        symbol=symbol,
        side=side,
        entry=entry,
        margin=margin,
        leverage=leverage,
        stop_loss=stop_loss,
        take_profit=take_profit,
        tp2=tp2,
        tp3=tp3,
        timeframe=timeframe,
        pattern=pattern,
        note=note,
        source="MANUAL_PRACTICE_MARKET",
    )
    equity = max(cash, 1e-9)
    risk_pct = created["risk_usdt"] / equity * 100.0
    warnings: list[str] = []
    if risk_pct > 0.5:
        warnings.append("risk_above_beginner_practice_range_0_25_to_0_5_pct")
    if leverage > 10:
        warnings.append("high_leverage_for_practice")
    liq = created["liquidation_price"]
    if (side == "LONG" and stop_loss <= liq) or (side == "SHORT" and stop_loss >= liq):
        warnings.append("stop_is_beyond_estimated_liquidation")
    await db.commit()
    return {
        "paper_only": True,
        "practice_mode": True,
        "trade_id": created["position_id"],
        "symbol": symbol,
        "side": side,
        "entry_price": entry,
        "margin_used": round(margin, 6),
        "leverage": leverage,
        "quantity": round(created["quantity"], 10),
        "notional": round(created["notional"], 6),
        "risk_usdt": round(created["risk_usdt"], 6),
        "risk_pct_of_equity": round(risk_pct, 4),
        "liquidation_price": round(liq, 12),
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "warnings": warnings,
    }


async def create_practice_limit_order(
    db: AsyncSession,
    *,
    session_id: str,
    symbol: str,
    side: str,
    limit_price: float,
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
    limit_price = _f(limit_price)
    margin = _f(margin)
    leverage = max(1, min(MAX_LEVERAGE, int(leverage)))
    if limit_price <= 0 or margin <= 0:
        raise ValueError("limit price and margin must be positive")
    _validate_geometry(
        side=side,
        entry=limit_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        tp2=tp2,
        tp3=tp3,
    )
    await ensure_practice_account(db, session_id)
    cash, available = await _account_available_margin(db, session_id)
    if margin > available or margin > cash * MAX_MARGIN_SHARE:
        raise ValueError("insufficient practice margin")
    current = await _latest_price(symbol)
    if current <= 0:
        raise ValueError("market price unavailable")

    result = await db.execute(text("""
        INSERT INTO practice_orders (
            session_id, symbol, side, order_type, status, limit_price, stop_loss,
            take_profit, tp2, tp3, leverage, margin_used, timeframe, pattern, note
        ) VALUES (
            :session_id, :symbol, :side, 'LIMIT', 'PENDING', :limit_price, :stop_loss,
            :take_profit, :tp2, :tp3, :leverage, :margin_used, :timeframe, :pattern, :note
        ) RETURNING id
    """), {
        "session_id": session_id,
        "symbol": symbol,
        "side": side,
        "limit_price": limit_price,
        "stop_loss": _f(stop_loss),
        "take_profit": _f(take_profit),
        "tp2": _f(tp2) if tp2 is not None else None,
        "tp3": _f(tp3) if tp3 is not None else None,
        "leverage": leverage,
        "margin_used": margin,
        "timeframe": (timeframe or "")[:16] or None,
        "pattern": (pattern or "")[:80] or None,
        "note": (note or "")[:1000] or None,
    })
    order_id = int(result.scalar_one())
    await db.commit()
    marketable = (side == "LONG" and limit_price >= current) or (side == "SHORT" and limit_price <= current)
    return {
        "paper_only": True,
        "practice_mode": True,
        "order_id": order_id,
        "status": "PENDING",
        "symbol": symbol,
        "side": side,
        "limit_price": limit_price,
        "current_price": current,
        "marketable_now": marketable,
        "note": "LIMIT PAPER reserves fictitious margin until fill or cancel.",
    }


async def cancel_practice_order(db: AsyncSession, session_id: str, order_id: int) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    result = await db.execute(text("""
        UPDATE practice_orders
        SET status='CANCELED', canceled_at=NOW()
        WHERE id=:order_id AND session_id=:session_id AND status='PENDING'
        RETURNING id
    """), {"order_id": int(order_id), "session_id": session_id})
    row = result.first()
    if not row:
        raise ValueError("pending practice order not found")
    await db.commit()
    return {"paper_only": True, "practice_mode": True, "order_id": int(order_id), "status": "CANCELED"}


async def modify_practice_trade(
    db: AsyncSession,
    *,
    session_id: str,
    trade_id: int,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    tp2: float | None = None,
    tp3: float | None = None,
) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    row = (await db.execute(text("""
        SELECT * FROM practice_positions
        WHERE id=:trade_id AND session_id=:session_id AND status='OPEN'
        FOR UPDATE
    """), {"trade_id": int(trade_id), "session_id": session_id})).mappings().first()
    if not row:
        raise ValueError("practice trade not found or already closed")
    position = dict(row)
    new_stop = _f(stop_loss) if stop_loss is not None else _f(position.get("stop_loss"))
    new_tp1 = _f(take_profit) if take_profit is not None else _f(position.get("take_profit"))
    new_tp2 = _f(tp2) if tp2 is not None else (_f(position.get("tp2")) if position.get("tp2") is not None else None)
    new_tp3 = _f(tp3) if tp3 is not None else (_f(position.get("tp3")) if position.get("tp3") is not None else None)
    _validate_geometry(
        side=str(position["side"]),
        entry=_f(position["entry_price"]),
        stop_loss=new_stop,
        take_profit=new_tp1,
        tp2=new_tp2,
        tp3=new_tp3,
        allow_break_even=True,
    )
    qty = _f(position.get("quantity"))
    risk = abs(_f(position["entry_price"]) - new_stop) * qty
    await db.execute(text("""
        UPDATE practice_positions
        SET stop_loss=:stop_loss, take_profit=:take_profit, tp2=:tp2, tp3=:tp3,
            risk_usdt=:risk_usdt, last_synced_at=NOW()
        WHERE id=:trade_id AND session_id=:session_id
    """), {
        "stop_loss": new_stop,
        "take_profit": new_tp1,
        "tp2": new_tp2,
        "tp3": new_tp3,
        "risk_usdt": risk,
        "trade_id": int(trade_id),
        "session_id": session_id,
    })
    await _record_event(
        db,
        session_id=session_id,
        position_id=int(trade_id),
        event_type="MODIFY",
        price=new_stop,
        quantity=qty,
        message="SL/TP updated in practice",
    )
    await db.commit()
    return {
        "paper_only": True,
        "practice_mode": True,
        "trade_id": int(trade_id),
        "stop_loss": new_stop,
        "take_profit": new_tp1,
        "tp2": new_tp2,
        "tp3": new_tp3,
        "risk_usdt": round(risk, 6),
    }


async def move_practice_stop_to_break_even(db: AsyncSession, session_id: str, trade_id: int) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    row = (await db.execute(text("""
        SELECT id, entry_price, quantity FROM practice_positions
        WHERE id=:trade_id AND session_id=:session_id AND status='OPEN'
        FOR UPDATE
    """), {"trade_id": int(trade_id), "session_id": session_id})).mappings().first()
    if not row:
        raise ValueError("practice trade not found or already closed")
    entry = _f(row["entry_price"])
    await db.execute(text("""
        UPDATE practice_positions
        SET stop_loss=:entry, risk_usdt=0, moved_to_be=TRUE, last_synced_at=NOW()
        WHERE id=:trade_id AND session_id=:session_id
    """), {"entry": entry, "trade_id": int(trade_id), "session_id": session_id})
    await _record_event(
        db,
        session_id=session_id,
        position_id=int(trade_id),
        event_type="BREAK_EVEN",
        price=entry,
        quantity=_f(row["quantity"]),
        message="Stop moved to entry",
    )
    await db.commit()
    return {"paper_only": True, "practice_mode": True, "trade_id": int(trade_id), "stop_loss": entry, "moved_to_be": True}


async def _realize_quantity(
    db: AsyncSession,
    *,
    position: dict[str, Any],
    quantity_to_close: float,
    exit_price: float,
    reason: str,
    final: bool,
) -> dict[str, float]:
    session_id = str(position["session_id"])
    trade_id = int(position["id"])
    current_qty = _f(position.get("quantity"))
    close_qty = max(0.0, min(current_qty, _f(quantity_to_close)))
    if close_qty <= 0:
        return {"net_pnl": 0.0, "closed_quantity": 0.0}
    entry = _f(position["entry_price"])
    portion_notional = entry * close_qty
    now = datetime.now(timezone.utc)
    pnl = calculate_trade_pnl(
        side=str(position["side"]),
        entry=entry,
        exit_price=exit_price,
        quantity=close_qty,
        notional=portion_notional,
        opened_at=position["opened_at"],
        closed_at=now,
    )
    remaining = max(0.0, current_qty - close_qty)
    leverage = max(1, int(position["leverage"]))
    remaining_notional = entry * remaining
    remaining_margin = remaining_notional / leverage
    remaining_risk = abs(entry - _f(position["stop_loss"])) * remaining
    final = bool(final or remaining <= max(1e-12, _f(position.get("initial_quantity")) * 1e-8))
    await db.execute(text("""
        UPDATE practice_positions
        SET quantity=:remaining_quantity,
            notional=:remaining_notional,
            margin_used=:remaining_margin,
            risk_usdt=:remaining_risk,
            gross_pnl=COALESCE(gross_pnl,0)+:gross_pnl,
            net_pnl=COALESCE(net_pnl,0)+:net_pnl,
            fees=COALESCE(fees,0)+:fees,
            slippage=COALESCE(slippage,0)+:slippage,
            funding_estimate=COALESCE(funding_estimate,0)+:funding_estimate,
            partial_realized_pnl=COALESCE(partial_realized_pnl,0)+:net_pnl,
            status=CASE WHEN :final THEN 'CLOSED' ELSE status END,
            exit_price=CASE WHEN :final THEN :exit_price ELSE exit_price END,
            close_reason=CASE WHEN :final THEN :close_reason ELSE close_reason END,
            closed_at=CASE WHEN :final THEN :closed_at ELSE closed_at END
        WHERE id=:trade_id AND session_id=:session_id
    """), {
        "remaining_quantity": remaining,
        "remaining_notional": remaining_notional,
        "remaining_margin": remaining_margin,
        "remaining_risk": remaining_risk,
        "gross_pnl": pnl["gross_pnl"],
        "net_pnl": pnl["net_pnl"],
        "fees": pnl["fees"],
        "slippage": pnl["slippage"],
        "funding_estimate": pnl["funding_estimate"],
        "final": final,
        "exit_price": exit_price,
        "close_reason": str(reason)[:64],
        "closed_at": now,
        "trade_id": trade_id,
        "session_id": session_id,
    })
    costs = pnl["fees"] + pnl["slippage"] + pnl["funding_estimate"]
    await db.execute(text("""
        UPDATE practice_accounts
        SET cash_balance=cash_balance+:net_pnl,
            realized_pnl=realized_pnl+:net_pnl,
            total_costs=total_costs+:costs,
            updated_at=NOW()
        WHERE session_id=:session_id
    """), {"net_pnl": pnl["net_pnl"], "costs": costs, "session_id": session_id})
    await _record_event(
        db,
        session_id=session_id,
        position_id=trade_id,
        event_type=str(reason)[:32],
        price=exit_price,
        quantity=close_qty,
        net_pnl=pnl["net_pnl"],
        message="Final close" if final else "Partial realization",
    )
    return {**pnl, "closed_quantity": close_qty, "remaining_quantity": remaining, "final": final}


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
    result = await _realize_quantity(
        db,
        position=position,
        quantity_to_close=_f(position["quantity"]),
        exit_price=exit_price,
        reason=reason,
        final=True,
    )
    await db.commit()
    return {
        "paper_only": True,
        "practice_mode": True,
        "trade_id": trade_id,
        "exit_price": exit_price,
        "close_reason": str(reason or "USER_CLOSE")[:64],
        **result,
    }


async def partial_close_practice_trade(
    db: AsyncSession,
    *,
    session_id: str,
    trade_id: int,
    fraction: float,
) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    fraction = _f(fraction)
    if not (0 < fraction <= 1):
        raise ValueError("partial fraction must be > 0 and <= 1")
    await ensure_practice_account(db, session_id)
    row = (await db.execute(text("""
        SELECT * FROM practice_positions
        WHERE id=:trade_id AND session_id=:session_id AND status='OPEN'
        FOR UPDATE
    """), {"trade_id": int(trade_id), "session_id": session_id})).mappings().first()
    if not row:
        raise ValueError("practice trade not found or already closed")
    position = dict(row)
    exit_price = await _latest_price(str(position["symbol"]))
    qty = _f(position["quantity"]) * fraction
    result = await _realize_quantity(
        db,
        position=position,
        quantity_to_close=qty,
        exit_price=exit_price,
        reason="PARTIAL_CLOSE",
        final=fraction >= 0.999999,
    )
    await db.commit()
    return {"paper_only": True, "practice_mode": True, "trade_id": int(trade_id), "exit_price": exit_price, **result}


async def _sync_pending_orders(db: AsyncSession, session_id: str) -> int:
    orders = [dict(r) for r in (await db.execute(text("""
        SELECT * FROM practice_orders
        WHERE session_id=:session_id AND status='PENDING'
        ORDER BY created_at ASC
    """), {"session_id": session_id})).mappings().all()]
    filled = 0
    for order in orders:
        try:
            mark = await _latest_price(str(order["symbol"]))
        except Exception:
            continue
        side = str(order["side"]).upper()
        limit_price = _f(order["limit_price"])
        triggered = (side == "LONG" and mark <= limit_price) or (side == "SHORT" and mark >= limit_price)
        # Marketable limit orders are filled on the next sync as well.
        if not triggered:
            continue
        created = await _insert_position(
            db,
            session_id=session_id,
            symbol=str(order["symbol"]),
            side=side,
            entry=limit_price,
            margin=_f(order["margin_used"]),
            leverage=int(order["leverage"]),
            stop_loss=_f(order["stop_loss"]),
            take_profit=_f(order["take_profit"]),
            tp2=_f(order["tp2"]) if order.get("tp2") is not None else None,
            tp3=_f(order["tp3"]) if order.get("tp3") is not None else None,
            timeframe=order.get("timeframe"),
            pattern=order.get("pattern"),
            note=order.get("note"),
            source="MANUAL_PRACTICE_LIMIT",
        )
        await db.execute(text("""
            UPDATE practice_orders
            SET status='FILLED', filled_at=NOW(), position_id=:position_id
            WHERE id=:order_id AND session_id=:session_id
        """), {"position_id": created["position_id"], "order_id": int(order["id"]), "session_id": session_id})
        filled += 1
    return filled


async def _sync_open_positions(db: AsyncSession, session_id: str) -> dict[str, int]:
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT * FROM practice_positions
        WHERE session_id=:session_id AND status='OPEN'
        ORDER BY opened_at ASC
    """), {"session_id": session_id})).mappings().all()]
    closed = 0
    partials = 0
    for row in rows:
        try:
            klines = await binance_client.klines(str(row["symbol"]), interval="1m", limit=500)
        except Exception:
            continue
        sync_from = row.get("last_synced_at") or row["opened_at"]
        start_ms = int(sync_from.timestamp() * 1000)
        future = [k for k in klines if len(k) >= 5 and int(k[0]) >= start_ms]
        if not future:
            continue
        side = str(row["side"]).upper()
        stop = _f(row["stop_loss"])
        liq = _f(row.get("liquidation_price"))
        tp1 = _f(row["take_profit"])
        tp2 = _f(row["tp2"]) if row.get("tp2") is not None else None
        tp3 = _f(row["tp3"]) if row.get("tp3") is not None else None
        initial_qty = _f(row.get("initial_quantity"), _f(row["quantity"]))
        tp1_hit = bool(row.get("tp1_hit"))
        tp2_hit = bool(row.get("tp2_hit"))
        tp3_hit = bool(row.get("tp3_hit"))

        for k in future:
            # Refresh the position after each realization so remaining quantity is correct.
            current = (await db.execute(text("""
                SELECT * FROM practice_positions
                WHERE id=:id AND session_id=:session_id
            """), {"id": int(row["id"]), "session_id": session_id})).mappings().first()
            if not current or str(current["status"]) != "OPEN":
                break
            position = dict(current)
            high, low = _f(k[2]), _f(k[3])

            liquidation_hit = (
                liq > 0 and (
                    (side == "LONG" and stop <= liq and low <= liq) or
                    (side == "SHORT" and stop >= liq and high >= liq)
                )
            )
            stop_hit = (side == "LONG" and low <= stop) or (side == "SHORT" and high >= stop)
            if liquidation_hit:
                await _realize_quantity(
                    db,
                    position=position,
                    quantity_to_close=_f(position["quantity"]),
                    exit_price=liq,
                    reason="LIQUIDATION",
                    final=True,
                )
                closed += 1
                break
            if stop_hit:
                await _realize_quantity(
                    db,
                    position=position,
                    quantity_to_close=_f(position["quantity"]),
                    exit_price=stop,
                    reason="BREAK_EVEN" if bool(position.get("moved_to_be")) else "STOP",
                    final=True,
                )
                closed += 1
                break

            hit1 = (side == "LONG" and high >= tp1) or (side == "SHORT" and low <= tp1)
            hit2 = tp2 is not None and ((side == "LONG" and high >= tp2) or (side == "SHORT" and low <= tp2))
            hit3 = tp3 is not None and ((side == "LONG" and high >= tp3) or (side == "SHORT" and low <= tp3))

            if hit1 and not tp1_hit:
                if tp2 is None and tp3 is None:
                    qty = _f(position["quantity"])
                    final = True
                else:
                    qty = min(_f(position["quantity"]), initial_qty * (0.4 if tp3 is not None else 0.5))
                    final = False
                await _realize_quantity(
                    db,
                    position=position,
                    quantity_to_close=qty,
                    exit_price=tp1,
                    reason="TP1",
                    final=final,
                )
                await db.execute(text("UPDATE practice_positions SET tp1_hit=TRUE WHERE id=:id"), {"id": int(row["id"])})
                tp1_hit = True
                partials += 1
                if final:
                    closed += 1
                    break

            if hit2 and not tp2_hit:
                current = (await db.execute(text("SELECT * FROM practice_positions WHERE id=:id"), {"id": int(row["id"])})).mappings().first()
                if not current or str(current["status"]) != "OPEN":
                    break
                position = dict(current)
                qty = _f(position["quantity"]) if tp3 is None else min(_f(position["quantity"]), initial_qty * 0.3)
                final = tp3 is None
                await _realize_quantity(
                    db,
                    position=position,
                    quantity_to_close=qty,
                    exit_price=float(tp2),
                    reason="TP2",
                    final=final,
                )
                await db.execute(text("UPDATE practice_positions SET tp2_hit=TRUE WHERE id=:id"), {"id": int(row["id"])})
                tp2_hit = True
                partials += 1
                if final:
                    closed += 1
                    break

            if hit3 and not tp3_hit:
                current = (await db.execute(text("SELECT * FROM practice_positions WHERE id=:id"), {"id": int(row["id"])})).mappings().first()
                if not current or str(current["status"]) != "OPEN":
                    break
                position = dict(current)
                await _realize_quantity(
                    db,
                    position=position,
                    quantity_to_close=_f(position["quantity"]),
                    exit_price=float(tp3),
                    reason="TP3",
                    final=True,
                )
                await db.execute(text("UPDATE practice_positions SET tp3_hit=TRUE WHERE id=:id"), {"id": int(row["id"])})
                tp3_hit = True
                partials += 1
                closed += 1
                break

        await db.execute(text("""
            UPDATE practice_positions
            SET last_synced_at=NOW()
            WHERE id=:id AND session_id=:session_id AND status='OPEN'
        """), {"id": int(row["id"]), "session_id": session_id})
    return {"closed": closed, "partials": partials}


async def sync_practice_account(db: AsyncSession, session_id: str) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    filled = await _sync_pending_orders(db, session_id)
    managed = await _sync_open_positions(db, session_id)
    await db.commit()
    return {
        "paper_only": True,
        "practice_mode": True,
        "filled_orders": filled,
        **managed,
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
            "margin_used", "quantity", "initial_quantity", "notional", "risk_usdt",
            "initial_risk_usdt", "gross_pnl", "net_pnl", "partial_realized_pnl",
            "fees", "slippage", "funding_estimate", "liquidation_price",
        ):
            if row.get(key) is not None:
                row[key] = _f(row[key])
        initial_risk = _f(row.get("initial_risk_usdt"))
        row["r_multiple"] = round(_f(row.get("net_pnl")) / initial_risk, 4) if initial_risk > 0 else None
    return rows


async def practice_events(db: AsyncSession, session_id: str, trade_id: int, limit: int = 100) -> list[dict[str, Any]]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT id, position_id, event_type, price, quantity, net_pnl, message, created_at
        FROM practice_trade_events
        WHERE session_id=:session_id AND position_id=:trade_id
        ORDER BY created_at ASC
        LIMIT :limit
    """), {"session_id": session_id, "trade_id": int(trade_id), "limit": max(1, min(int(limit), 500))})).mappings().all()]
    for row in rows:
        if row.get("created_at"):
            row["created_at"] = row["created_at"].isoformat()
        for key in ("price", "quantity", "net_pnl"):
            if row.get(key) is not None:
                row[key] = _f(row[key])
    return rows


async def reset_practice_account(db: AsyncSession, session_id: str) -> dict[str, Any]:
    session_id = _clean_session_id(session_id)
    await ensure_practice_account(db, session_id)
    await db.execute(text("DELETE FROM practice_trade_events WHERE session_id=:session_id"), {"session_id": session_id})
    await db.execute(text("DELETE FROM practice_orders WHERE session_id=:session_id"), {"session_id": session_id})
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
