from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client

STARTING_BALANCE = 1000.0
RISK_PER_TRADE = 0.01
MAX_OPEN_POSITIONS = 3
TAKER_FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
FUNDING_ESTIMATE_8H = 0.0001
MAX_HOLD_MINUTES = 120


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def choose_leverage(grade: str | None, fingerprint_score: float, catalyst_state: str | None) -> int:
    grade = str(grade or "").upper()
    if catalyst_state in {"CONFLICT", "SHOCK_RISK"}:
        return 1
    if grade == "A+" and fingerprint_score >= 82:
        return 4
    if grade in {"A+", "A"} and fingerprint_score >= 76:
        return 3
    return 2


def size_position(balance: float, entry: float, stop: float, leverage: int) -> dict[str, float]:
    stop_distance = abs(entry - stop)
    if balance <= 0 or entry <= 0 or stop_distance <= 0:
        return {"risk_usdt": 0.0, "quantity": 0.0, "notional": 0.0, "margin": 0.0}
    risk_usdt = balance * RISK_PER_TRADE
    quantity_by_risk = risk_usdt / stop_distance
    max_margin = balance * 0.30
    max_notional = max_margin * max(1, leverage)
    quantity = min(quantity_by_risk, max_notional / entry)
    notional = quantity * entry
    margin = notional / max(1, leverage)
    return {
        "risk_usdt": round(risk_usdt, 6),
        "quantity": round(quantity, 10),
        "notional": round(notional, 6),
        "margin": round(margin, 6),
    }


def calculate_trade_pnl(*, side: str, entry: float, exit_price: float, quantity: float, notional: float, opened_at: datetime, closed_at: datetime) -> dict[str, float]:
    gross = (exit_price - entry) * quantity if side == "LONG" else (entry - exit_price) * quantity
    entry_fee = notional * TAKER_FEE_RATE
    exit_notional = quantity * exit_price
    exit_fee = exit_notional * TAKER_FEE_RATE
    slippage = (notional + exit_notional) * SLIPPAGE_RATE
    hours = max(0.0, (closed_at - opened_at).total_seconds() / 3600.0)
    funding = notional * FUNDING_ESTIMATE_8H * (hours / 8.0)
    net = gross - entry_fee - exit_fee - slippage - funding
    return {
        "gross_pnl": round(gross, 6),
        "fees": round(entry_fee + exit_fee, 6),
        "slippage": round(slippage, 6),
        "funding_estimate": round(funding, 6),
        "net_pnl": round(net, 6),
    }


async def ensure_paper_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_accounts (
            id INTEGER PRIMARY KEY DEFAULT 1,
            starting_balance NUMERIC(18,6) NOT NULL DEFAULT 1000,
            cash_balance NUMERIC(18,6) NOT NULL DEFAULT 1000,
            realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
            total_fees NUMERIC(18,6) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (id = 1)
        )
    """))
    await db.execute(text("""
        INSERT INTO paper_accounts (id, starting_balance, cash_balance)
        VALUES (1, :balance, :balance)
        ON CONFLICT (id) DO NOTHING
    """), {"balance": STARTING_BALANCE})
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_positions (
            id BIGSERIAL PRIMARY KEY,
            signal_id UUID UNIQUE REFERENCES validation_observations(signal_id) ON DELETE SET NULL,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            status VARCHAR(12) NOT NULL DEFAULT 'OPEN',
            grade VARCHAR(8),
            fingerprint_score NUMERIC(10,4),
            leverage INTEGER NOT NULL,
            entry_price NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            take_profit NUMERIC(30,12) NOT NULL,
            quantity NUMERIC(30,12) NOT NULL,
            notional NUMERIC(24,8) NOT NULL,
            margin_used NUMERIC(24,8) NOT NULL,
            risk_usdt NUMERIC(24,8) NOT NULL,
            opened_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ,
            exit_price NUMERIC(30,12),
            exit_reason VARCHAR(24),
            gross_pnl NUMERIC(24,8),
            net_pnl NUMERIC(24,8),
            fees NUMERIC(24,8) DEFAULT 0,
            slippage NUMERIC(24,8) DEFAULT 0,
            funding_estimate NUMERIC(24,8) DEFAULT 0,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status, opened_at DESC)"))
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_equity_curve (
            id BIGSERIAL PRIMARY KEY,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            cash_balance NUMERIC(18,6) NOT NULL,
            unrealized_pnl NUMERIC(18,6) NOT NULL,
            equity NUMERIC(18,6) NOT NULL,
            open_positions INTEGER NOT NULL
        )
    """))
    await db.commit()


async def _latest_price(symbol: str) -> float:
    rows = await binance_client.klines(symbol, interval="1m", limit=3)
    if not rows:
        return 0.0
    return _f(rows[-1][4])


async def _close_due_positions(db: AsyncSession) -> dict[str, int]:
    rows = (await db.execute(text("""
        SELECT id, symbol, side, entry_price, stop_loss, take_profit, quantity, notional, opened_at
        FROM paper_positions
        WHERE status='OPEN'
        ORDER BY opened_at ASC
    """))).mappings().all()
    closed = 0
    for raw in rows:
        row = dict(raw)
        now = datetime.now(timezone.utc)
        klines = await binance_client.klines(row["symbol"], interval="1m", limit=500)
        start_ms = int(row["opened_at"].timestamp() * 1000)
        future = [k for k in klines if len(k) >= 5 and int(k[0]) >= start_ms]
        exit_price = None
        exit_reason = None
        for k in future:
            high, low = _f(k[2]), _f(k[3])
            side = row["side"]
            stop_hit = low <= _f(row["stop_loss"]) if side == "LONG" else high >= _f(row["stop_loss"])
            tp_hit = high >= _f(row["take_profit"]) if side == "LONG" else low <= _f(row["take_profit"])
            if stop_hit and tp_hit:
                exit_price, exit_reason = _f(row["stop_loss"]), "AMBIGUOUS_STOP"
                break
            if stop_hit:
                exit_price, exit_reason = _f(row["stop_loss"]), "STOP"
                break
            if tp_hit:
                exit_price, exit_reason = _f(row["take_profit"]), "TP1"
                break
        age_minutes = (now - row["opened_at"]).total_seconds() / 60.0
        if exit_price is None and age_minutes >= MAX_HOLD_MINUTES:
            exit_price = _f(future[-1][4]) if future else await _latest_price(row["symbol"])
            exit_reason = "TIME_EXIT"
        if exit_price is None or exit_price <= 0:
            continue
        pnl = calculate_trade_pnl(
            side=row["side"], entry=_f(row["entry_price"]), exit_price=exit_price,
            quantity=_f(row["quantity"]), notional=_f(row["notional"]), opened_at=row["opened_at"], closed_at=now,
        )
        await db.execute(text("""
            UPDATE paper_positions SET status='CLOSED', closed_at=:closed_at, exit_price=:exit_price,
                exit_reason=:exit_reason, gross_pnl=:gross_pnl, net_pnl=:net_pnl, fees=:fees,
                slippage=:slippage, funding_estimate=:funding_estimate
            WHERE id=:id
        """), {"id": row["id"], "closed_at": now, "exit_price": exit_price, "exit_reason": exit_reason, **pnl})
        await db.execute(text("""
            UPDATE paper_accounts SET cash_balance=cash_balance+:net_pnl,
                realized_pnl=realized_pnl+:net_pnl,
                total_fees=total_fees+:fees+:slippage+:funding_estimate,
                updated_at=NOW() WHERE id=1
        """), pnl)
        closed += 1
    await db.commit()
    return {"closed": closed}


async def _open_new_positions(db: AsyncSession) -> dict[str, int]:
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = _f(account["cash_balance"] if account else STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one())
    slots = max(0, MAX_OPEN_POSITIONS - open_count)
    if slots <= 0:
        return {"opened": 0}
    candidates = (await db.execute(text("""
        SELECT vo.signal_id::text, vo.symbol, vo.observed_at, vo.direction, vo.entry_price,
               vo.stop_loss, vo.tp1, vo.trade_class, vo.grade, vo.fingerprint_score,
               vo.catalyst_state, vo.master_state
        FROM validation_observations vo
        LEFT JOIN paper_positions pp ON pp.signal_id=vo.signal_id
        WHERE pp.signal_id IS NULL
          AND vo.trade_class='TRADE_NOW'
          AND COALESCE(vo.master_state,'YES')='YES'
          AND vo.observed_at >= NOW() - INTERVAL '20 minutes'
        ORDER BY vo.observed_at ASC
        LIMIT :slots
    """), {"slots": slots})).mappings().all()
    opened = 0
    for raw in candidates:
        row = dict(raw)
        entry = _f(row["entry_price"])
        stop = _f(row["stop_loss"])
        tp = _f(row["tp1"])
        if entry <= 0 or stop <= 0 or tp <= 0:
            continue
        leverage = choose_leverage(row.get("grade"), _f(row.get("fingerprint_score")), row.get("catalyst_state"))
        sizing = size_position(balance, entry, stop, leverage)
        if sizing["quantity"] <= 0 or sizing["margin"] <= 0:
            continue
        await db.execute(text("""
            INSERT INTO paper_positions (
                signal_id, symbol, side, grade, fingerprint_score, leverage, entry_price, stop_loss,
                take_profit, quantity, notional, margin_used, risk_usdt, opened_at, metadata
            ) VALUES (
                CAST(:signal_id AS UUID), :symbol, :side, :grade, :fingerprint_score, :leverage,
                :entry_price, :stop_loss, :take_profit, :quantity, :notional, :margin_used,
                :risk_usdt, :opened_at, '{}'::jsonb
            ) ON CONFLICT (signal_id) DO NOTHING
        """), {
            "signal_id": row["signal_id"], "symbol": row["symbol"], "side": row["direction"],
            "grade": row.get("grade"), "fingerprint_score": _f(row.get("fingerprint_score")),
            "leverage": leverage, "entry_price": entry, "stop_loss": stop, "take_profit": tp,
            "quantity": sizing["quantity"], "notional": sizing["notional"], "margin_used": sizing["margin"],
            "risk_usdt": sizing["risk_usdt"], "opened_at": row["observed_at"],
        })
        opened += 1
    await db.commit()
    return {"opened": opened}


async def paper_summary(db: AsyncSession) -> dict[str, Any]:
    await ensure_paper_schema(db)
    account = dict((await db.execute(text("SELECT * FROM paper_accounts WHERE id=1"))).mappings().one())
    open_rows = [dict(r) for r in (await db.execute(text("SELECT * FROM paper_positions WHERE status='OPEN' ORDER BY opened_at DESC"))).mappings().all()]
    unrealized = 0.0
    positions = []
    for row in open_rows:
        mark = await _latest_price(row["symbol"])
        qty, entry = _f(row["quantity"]), _f(row["entry_price"])
        raw = (mark-entry)*qty if row["side"] == "LONG" else (entry-mark)*qty
        unrealized += raw
        positions.append({
            "id": row["id"], "symbol": row["symbol"], "side": row["side"], "leverage": row["leverage"],
            "entry_price": entry, "mark_price": mark, "stop_loss": _f(row["stop_loss"]),
            "take_profit": _f(row["take_profit"]), "margin_used": _f(row["margin_used"]),
            "unrealized_pnl": round(raw, 6), "opened_at": row["opened_at"].isoformat(),
        })
    cash = _f(account["cash_balance"])
    equity = cash + unrealized
    stats = dict((await db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE status='CLOSED') AS closed_trades,
               COUNT(*) FILTER (WHERE status='CLOSED' AND net_pnl > 0) AS winners,
               COUNT(*) FILTER (WHERE status='CLOSED' AND net_pnl <= 0) AS losers,
               COALESCE(SUM(net_pnl) FILTER (WHERE status='CLOSED'),0) AS net_pnl
        FROM paper_positions
    """))).mappings().one())
    closed = int(stats["closed_trades"] or 0)
    return {
        "version": "paper_portfolio_v1",
        "paper_only": True,
        "starting_balance": _f(account["starting_balance"]),
        "cash_balance": round(cash, 6),
        "unrealized_pnl": round(unrealized, 6),
        "equity": round(equity, 6),
        "realized_pnl": _f(account["realized_pnl"]),
        "total_costs": _f(account["total_fees"]),
        "open_positions": positions,
        "closed_trades": closed,
        "winners": int(stats["winners"] or 0),
        "losers": int(stats["losers"] or 0),
        "win_rate_pct": round((int(stats["winners"] or 0) / closed * 100.0), 2) if closed else None,
        "assumptions": {
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "taker_fee_pct_per_side": TAKER_FEE_RATE * 100,
            "slippage_pct_per_side": SLIPPAGE_RATE * 100,
            "funding_estimate_pct_per_8h": FUNDING_ESTIMATE_8H * 100,
            "max_hold_minutes": MAX_HOLD_MINUTES,
        },
    }


async def paper_history(db: AsyncSession, limit: int = 100) -> list[dict[str, Any]]:
    await ensure_paper_schema(db)
    rows = (await db.execute(text("""
        SELECT id, symbol, side, leverage, entry_price, exit_price, stop_loss, take_profit,
               opened_at, closed_at, exit_reason, gross_pnl, net_pnl, fees, slippage, funding_estimate
        FROM paper_positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT :limit
    """), {"limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def run_paper_cycle(db: AsyncSession) -> dict[str, Any]:
    await ensure_paper_schema(db)
    closed = await _close_due_positions(db)
    opened = await _open_new_positions(db)
    summary = await paper_summary(db)
    await db.execute(text("""
        INSERT INTO paper_equity_curve (cash_balance, unrealized_pnl, equity, open_positions)
        VALUES (:cash, :unrealized, :equity, :open_positions)
    """), {"cash": summary["cash_balance"], "unrealized": summary["unrealized_pnl"], "equity": summary["equity"], "open_positions": len(summary["open_positions"])})
    await db.commit()
    return {**closed, **opened, "equity": summary["equity"]}
