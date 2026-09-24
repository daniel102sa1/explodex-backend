from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client

STARTING_BALANCE = 1000.0
RISK_PER_TRADE = 0.03
MAX_OPEN_POSITIONS = 3
TAKER_FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
FUNDING_ESTIMATE_8H = 0.0001
MAX_HOLD_MINUTES = 120

# Visual/evaluation cohort for the arsenal added on 23 Sep 2026 (1/2/3 + AMD).
# Older positions remain stored and available for learning/audit, but the default
# user-facing PAPER view starts here so legacy positions do not contaminate the
# new-arsenal scorecard.
ARSENAL_DISPLAY_START = datetime(2026, 9, 23, 17, 17, 23, tzinfo=timezone.utc)
ARSENAL_DISPLAY_LABEL = "ARSENAL_1_2_3_AMD_2026_09_23"


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
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
    """Size PAPER positions from structural stop risk, then cap by margin.

    Leverage changes required margin only; it never increases the approved stop
    risk budget. Keep the full sizing payload here so every executor uses the
    same implementation without runtime monkey-patching.
    """
    balance = _f(balance)
    entry = _f(entry)
    stop = _f(stop)
    leverage = max(1, int(_f(leverage, 1.0)))
    stop_distance = abs(entry - stop)
    if balance <= 0 or entry <= 0 or stop_distance <= 0:
        return {
            "risk_budget_usdt": 0.0,
            "target_risk_usdt": 0.0,
            "risk_usdt": 0.0,
            "risk_pct_of_balance": 0.0,
            "quantity": 0.0,
            "notional": 0.0,
            "margin": 0.0,
        }

    risk_budget_usdt = balance * RISK_PER_TRADE
    quantity_by_risk = risk_budget_usdt / stop_distance
    max_margin = balance * 0.30
    max_notional = max_margin * leverage
    quantity_by_margin = max_notional / entry
    quantity = max(0.0, min(quantity_by_risk, quantity_by_margin))
    notional = quantity * entry
    margin = notional / leverage
    actual_risk_usdt = quantity * stop_distance

    return {
        "risk_budget_usdt": round(risk_budget_usdt, 6),
        "target_risk_usdt": round(risk_budget_usdt, 6),
        "risk_usdt": round(actual_risk_usdt, 6),
        "risk_pct_of_balance": round(actual_risk_usdt / balance * 100.0, 6),
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
    """Lightweight mark lookup.

    A provider/rate-limit failure must not crash the whole PAPER cycle. Returning
    0 lets callers keep the previous/entry mark rather than inventing a price.
    """
    try:
        payload = await binance_client.price(symbol)
        return _f(payload.get("price")) if isinstance(payload, dict) else 0.0
    except Exception:
        return 0.0


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
        mark_stale = mark <= 0
        if mark_stale:
            mark = entry
        raw = (mark-entry)*qty if row["side"] == "LONG" else (entry-mark)*qty
        unrealized += raw
        metadata = _meta(row.get("metadata"))
        positions.append({
            "id": row["id"], "symbol": row["symbol"], "side": row["side"], "leverage": row["leverage"],
            "entry_price": entry, "mark_price": mark, "mark_price_stale": mark_stale, "stop_loss": _f(row["stop_loss"]),
            "take_profit": _f(row["take_profit"]), "tp1": _f(row["take_profit"]),
            "tp2": metadata.get("tp2"), "tp3": metadata.get("tp3"), "margin_used": _f(row["margin_used"]),
            "unrealized_pnl": round(raw, 6), "opened_at": row["opened_at"].isoformat(),
            "strategy_mode": metadata.get("strategy_mode"),
            "trade_profile": metadata.get("trade_profile") or metadata.get("strategy_mode"),
            "planned_horizon": metadata.get("planned_horizon") or metadata.get("horizon"),
            "max_hold_minutes": metadata.get("planned_max_hold_minutes") or metadata.get("max_hold_minutes"),
            "planned_max_leverage": metadata.get("planned_max_leverage"),
            "leverage_policy": metadata.get("leverage_policy"),
            "stop_policy": metadata.get("stop_policy") or ("IMMUTABLE_STRUCTURAL_STOP" if metadata.get("initial_hard_stop") else None),
            "frozen_plan": metadata.get("frozen_plan"),
            "arsenal_memory_snapshot": metadata.get("arsenal_memory_snapshot"),
            "pattern_score": metadata.get("pattern_score"),
            "phase": metadata.get("phase"),
            "breakout_level": metadata.get("breakout_level"),
            "retest_price": metadata.get("retest_price"),
            "soft_invalidation_stop": metadata.get("soft_invalidation_stop") or metadata.get("soft_invalidation_level"),
            "hard_stop": metadata.get("hard_stop") or metadata.get("structural_stop") or _f(row["stop_loss"]),
            "stop_survival_enabled": bool(metadata.get("stop_survival_enabled")),
            "chase_limit": metadata.get("chase_limit"),
            "actual_stop_risk_usdt": metadata.get("actual_stop_risk_usdt") or _f(row.get("risk_usdt")),
            "risk_usdt": _f(row.get("risk_usdt")),
            "notional": _f(row.get("notional")),
            "quantity": _f(row.get("quantity")),
            "evaluation_generation": metadata.get("evaluation_generation"),
            "chati_sarpon_612_monitor": metadata.get("chati_sarpon_612_monitor"),
            "chati_sarpon_612_history": list(metadata.get("chati_sarpon_612_history") or [])[-8:],
            "net_rr": _meta(metadata.get("execution_math_live")).get("net_rr"),
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


async def paper_arsenal_summary(db: AsyncSession) -> dict[str, Any]:
    """User-facing scorecard for positions opened after the latest arsenal deploy.

    This is display isolation only. Legacy rows remain in the database and all
    learning engines can continue using their historical data.
    """
    full = await paper_summary(db)
    cutoff = ARSENAL_DISPLAY_START
    positions = [
        p for p in list(full.get("open_positions") or [])
        if p.get("opened_at") and datetime.fromisoformat(str(p["opened_at"]).replace("Z", "+00:00")) >= cutoff
    ]
    unrealized = sum(_f(p.get("unrealized_pnl")) for p in positions)

    stats = dict((await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE status='CLOSED') AS closed_trades,
            COUNT(*) FILTER (WHERE status='CLOSED' AND net_pnl > 0) AS winners,
            COUNT(*) FILTER (WHERE status='CLOSED' AND net_pnl <= 0) AS losers,
            COALESCE(SUM(net_pnl) FILTER (WHERE status='CLOSED'),0) AS net_pnl,
            COALESCE(SUM(COALESCE(fees,0)+COALESCE(slippage,0)+COALESCE(funding_estimate,0))
                FILTER (WHERE status='CLOSED'),0) AS costs
        FROM paper_positions
        WHERE opened_at >= :cutoff
    """), {"cutoff": cutoff})).mappings().one())

    closed = int(stats.get("closed_trades") or 0)
    winners = int(stats.get("winners") or 0)
    realized = _f(stats.get("net_pnl"))
    costs = _f(stats.get("costs"))
    display_cash = STARTING_BALANCE + realized
    display_equity = display_cash + unrealized

    result = dict(full)
    result.update({
        "version": "paper_portfolio_arsenal_view_v1",
        "display_scope": "NEW_ARSENAL_ONLY",
        "display_label": ARSENAL_DISPLAY_LABEL,
        "display_started_at": cutoff.isoformat(),
        "legacy_positions_hidden_not_deleted": True,
        "starting_balance": STARTING_BALANCE,
        "cash_balance": round(display_cash, 6),
        "unrealized_pnl": round(unrealized, 6),
        "equity": round(display_equity, 6),
        "realized_pnl": round(realized, 6),
        "total_costs": round(costs, 6),
        "open_positions": positions,
        "closed_trades": closed,
        "winners": winners,
        "losers": int(stats.get("losers") or 0),
        "win_rate_pct": round(winners / closed * 100.0, 2) if closed else None,
    })
    return result


async def paper_history(db: AsyncSession, limit: int = 100, opened_after: datetime | None = None) -> list[dict[str, Any]]:
    await ensure_paper_schema(db)
    cohort_filter = " AND p.opened_at >= :opened_after" if opened_after is not None else ""
    rows = (await db.execute(text(f"""
        SELECT p.id, p.signal_id, p.symbol, p.side, p.leverage, p.entry_price, p.exit_price, p.stop_loss, p.take_profit,
               p.quantity, p.notional, p.margin_used, p.risk_usdt,
               p.opened_at, p.closed_at, p.exit_reason, p.gross_pnl, p.net_pnl, p.fees, p.slippage, p.funding_estimate,
               p.metadata,
               vm.mfe_pct, vm.mae_pct, vm.outcome AS verdict_outcome, vm.minutes_to_outcome
        FROM paper_positions p
        LEFT JOIN verdict_memory vm ON vm.signal_id = p.signal_id
        WHERE p.status='CLOSED'{cohort_filter}
        ORDER BY p.closed_at DESC
        LIMIT :limit
    """), {"limit": limit, **({"opened_after": opened_after} if opened_after is not None else {})})).mappings().all()
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        metadata = _meta(row.pop("metadata", {}))
        row["strategy_mode"] = metadata.get("strategy_mode")
        row["evaluation_generation"] = metadata.get("evaluation_generation")
        row["actual_stop_risk_usdt"] = metadata.get("actual_stop_risk_usdt") or _f(row.get("risk_usdt"))
        row["mfe_pct"] = _f(row.get("mfe_pct")) if row.get("mfe_pct") is not None else None
        row["mae_pct"] = _f(row.get("mae_pct")) if row.get("mae_pct") is not None else None
        row["verdict_outcome"] = row.get("verdict_outcome")
        row["minutes_to_outcome"] = _f(row.get("minutes_to_outcome")) if row.get("minutes_to_outcome") is not None else None
        row["trade_profile"] = metadata.get("trade_profile") or metadata.get("strategy_mode")
        row["planned_horizon"] = metadata.get("planned_horizon") or metadata.get("horizon")
        row["max_hold_minutes"] = metadata.get("planned_max_hold_minutes") or metadata.get("max_hold_minutes")
        row["planned_max_leverage"] = metadata.get("planned_max_leverage")
        row["pattern_score"] = metadata.get("pattern_score")
        row["phase"] = metadata.get("phase")
        row["soft_invalidation_stop"] = metadata.get("soft_invalidation_stop") or metadata.get("soft_invalidation_level")
        row["hard_stop"] = metadata.get("hard_stop") or metadata.get("structural_stop") or row.get("stop_loss")
        row["stop_survival_enabled"] = bool(metadata.get("stop_survival_enabled"))
        row["tp1"] = row.get("take_profit")
        row["tp2"] = metadata.get("tp2")
        row["tp3"] = metadata.get("tp3")
        row["technical_plan"] = metadata.get("technical_plan")
        row["fundamental_context"] = metadata.get("fundamental_context")
        row["net_rr"] = _meta(metadata.get("execution_math_live")).get("net_rr")
        output.append(row)
    return output


async def paper_equity_curve(db: AsyncSession, limit: int = 500, opened_after: datetime | None = None) -> dict[str, Any]:
    """Return a clean realized-equity curve derived from closed PAPER trades.

    This intentionally reconstructs the curve from the canonical paper_positions
    ledger, so a reset starts at exactly STARTING_BALANCE without carrying old
    sampled equity points forward.
    """
    await ensure_paper_schema(db)
    account = dict((await db.execute(text("SELECT * FROM paper_accounts WHERE id=1"))).mappings().one())
    cohort_filter = " AND opened_at >= :opened_after" if opened_after is not None else ""
    rows = [dict(r) for r in (await db.execute(text(f"""
        SELECT id, symbol, side, opened_at, closed_at, net_pnl
        FROM paper_positions
        WHERE status='CLOSED' AND closed_at IS NOT NULL{cohort_filter}
        ORDER BY closed_at ASC
        LIMIT :limit
    """), {
        "limit": max(1, min(int(limit), 5000)),
        **({"opened_after": opened_after} if opened_after is not None else {}),
    })).mappings().all()]

    starting = STARTING_BALANCE if opened_after is not None else _f(account.get("starting_balance"), STARTING_BALANCE)
    equity = starting
    points: list[dict[str, Any]] = [{
        "kind": "START",
        "trade_id": None,
        "symbol": None,
        "side": None,
        "observed_at": opened_after.isoformat() if opened_after is not None else (account["created_at"].isoformat() if account.get("created_at") else None),
        "net_pnl": 0.0,
        "equity": round(equity, 6),
        "cumulative_pnl": 0.0,
    }]
    for row in rows:
        pnl = _f(row.get("net_pnl"))
        equity += pnl
        points.append({
            "kind": "CLOSED_TRADE",
            "trade_id": row.get("id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "observed_at": row["closed_at"].isoformat() if row.get("closed_at") else None,
            "net_pnl": round(pnl, 6),
            "equity": round(equity, 6),
            "cumulative_pnl": round(equity - starting, 6),
        })
    return {
        "version": "paper_equity_curve_v1",
        "paper_only": True,
        "starting_balance": round(starting, 6),
        "realized_equity": round(equity, 6),
        "points": points,
    }


async def paper_signal_history(db: AsyncSession, limit: int = 200, created_after: datetime | None = None) -> list[dict[str, Any]]:
    """Recent scanner signals with PAPER execution linkage for the trade center."""
    cohort_filter = " WHERE s.created_at >= :created_after" if created_after is not None else ""
    rows = (await db.execute(text(f"""
        SELECT
            s.id::text AS id,
            sy.symbol,
            s.created_at,
            s.updated_at,
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
            s.is_active,
            s.reason,
            pp.id AS paper_trade_id,
            pp.status AS paper_trade_status,
            pp.opened_at AS paper_opened_at,
            pp.closed_at AS paper_closed_at,
            pp.exit_reason AS paper_exit_reason,
            pp.net_pnl AS paper_net_pnl
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        LEFT JOIN paper_positions pp ON pp.signal_id=s.id
        {cohort_filter}
        ORDER BY s.created_at DESC
        LIMIT :limit
    """), {
        "limit": max(1, min(int(limit), 1000)),
        **({"created_after": created_after} if created_after is not None else {}),
    })).mappings().all()
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        for key in ("created_at", "updated_at", "paper_opened_at", "paper_closed_at"):
            if row.get(key) is not None:
                row[key] = row[key].isoformat()
        row["was_executed"] = row.get("paper_trade_id") is not None
        output.append(row)
    return output


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
