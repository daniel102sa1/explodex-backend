from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client
from app.services import paper_portfolio as base

RANGE_SCAN_INTERVAL_SECONDS = 300
MIN_QUOTE_VOLUME_USDT = 5_000_000.0
MAX_RANGE_WIDTH_PCT = 8.0
MIN_RANGE_WIDTH_PCT = 0.55
RANGE_RISK_PER_TRADE = 0.0025  # 0.25% of PAPER balance
RANGE_MAX_MARGIN_SHARE = 0.15
RANGE_MAX_LEVERAGE = 2
RANGE_MAX_HOLD_MINUTES = 60
MIN_NET_PROFIT_USDT = 0.50
MIN_NET_TO_RISK = 0.75
MAX_COST_SHARE_OF_GROSS = 0.35

_last_scan_monotonic = 0.0
_last_scan_result: dict[str, Any] = {"scanned": 0, "candidates": 0, "errors": 0}


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _atr(rows: list[list[Any]], period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    sample = trs[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def analyze_range_klines(rows: list[list[Any]]) -> dict[str, Any]:
    usable = [r for r in rows if len(r) >= 6][-96:]
    if len(usable) < 48:
        return {"actionable": False, "reason": "insufficient_history"}

    window = usable[-48:]
    closes = [_f(r[4]) for r in window]
    highs = [_f(r[2]) for r in window]
    lows = [_f(r[3]) for r in window]
    volumes = [_f(r[5]) for r in window]
    last = closes[-1]
    if last <= 0:
        return {"actionable": False, "reason": "invalid_price"}

    range_low = min(lows[:-1])
    range_high = max(highs[:-1])
    width = range_high - range_low
    width_pct = width / last * 100.0 if last > 0 else 0.0
    atr = _atr(window)
    atr_pct = atr / last * 100.0 if last > 0 else 0.0
    if width <= 0 or width_pct < MIN_RANGE_WIDTH_PCT or width_pct > MAX_RANGE_WIDTH_PCT:
        return {"actionable": False, "reason": "range_width", "range_width_pct": round(width_pct, 4)}

    ema20 = _ema(closes, 20)
    ema_now = ema20[-1]
    ema_prev = ema20[-9] if len(ema20) >= 9 else ema20[0]
    ema_slope_pct = abs(ema_now - ema_prev) / last * 100.0
    displacement_pct = abs(closes[-1] - closes[-36]) / closes[-36] * 100.0 if closes[-36] else 99.0
    flat_limit = max(0.18, atr_pct * 0.65)
    displacement_limit = max(1.2, atr_pct * 1.7)
    if ema_slope_pct > flat_limit or displacement_pct > displacement_limit:
        return {
            "actionable": False,
            "reason": "not_lateral",
            "ema_slope_pct": round(ema_slope_pct, 4),
            "displacement_pct": round(displacement_pct, 4),
        }

    position = (last - range_low) / width
    buffer = max(atr * 0.35, width * 0.035)
    low_touches = sum(1 for low in lows[:-1] if low <= range_low + buffer)
    high_touches = sum(1 for high in highs[:-1] if high >= range_high - buffer)
    if low_touches < 2 or high_touches < 2:
        return {"actionable": False, "reason": "weak_range_touches"}

    side: str | None = None
    if position <= 0.22 and last > range_low:
        side = "LONG"
    elif position >= 0.78 and last < range_high:
        side = "SHORT"
    if side is None:
        return {"actionable": False, "reason": "not_at_range_edge", "range_position": round(position, 4)}

    # Do not fade a fresh breakout candle.
    latest_high, latest_low = _f(window[-1][2]), _f(window[-1][3])
    if latest_low < range_low - atr * 0.25 or latest_high > range_high + atr * 0.25:
        return {"actionable": False, "reason": "possible_breakout"}

    mid = (range_low + range_high) / 2.0
    if side == "LONG":
        stop = range_low - atr * 0.45
        target = mid
        edge_quality = max(0.0, 1.0 - position / 0.22)
    else:
        stop = range_high + atr * 0.45
        target = mid
        edge_quality = max(0.0, 1.0 - (1.0 - position) / 0.22)

    # Volume is secondary in ranges; avoid dead markets and violent spikes.
    avg_volume = sum(volumes[-20:-1]) / max(1, len(volumes[-20:-1]))
    volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 1.0
    if volume_ratio < 0.25 or volume_ratio > 3.5:
        return {"actionable": False, "reason": "abnormal_volume", "volume_ratio": round(volume_ratio, 4)}

    flat_score = max(0.0, 1.0 - ema_slope_pct / max(flat_limit, 1e-9))
    touch_score = min(1.0, (low_touches + high_touches) / 8.0)
    width_atr = width / atr if atr > 0 else 0.0
    structure_score = min(1.0, max(0.0, (width_atr - 2.0) / 4.0))
    score = 100.0 * (edge_quality * 0.42 + flat_score * 0.28 + touch_score * 0.18 + structure_score * 0.12)

    return {
        "actionable": score >= 68.0,
        "strategy_mode": "RANGE_MICRO",
        "side": side,
        "score": round(score, 2),
        "entry_reference": last,
        "stop_loss": stop,
        "take_profit": target,
        "range_low": range_low,
        "range_high": range_high,
        "range_position": round(position, 4),
        "range_width_pct": round(width_pct, 4),
        "atr_pct": round(atr_pct, 4),
        "ema_slope_pct": round(ema_slope_pct, 4),
        "volume_ratio": round(volume_ratio, 4),
        "low_touches": low_touches,
        "high_touches": high_touches,
    }


def size_range_position(balance: float, entry: float, stop: float, leverage: int = 1) -> dict[str, float]:
    distance = abs(entry - stop)
    if balance <= 0 or entry <= 0 or distance <= 0:
        return {"risk_usdt": 0.0, "quantity": 0.0, "notional": 0.0, "margin": 0.0}
    risk_usdt = balance * RANGE_RISK_PER_TRADE
    quantity_by_risk = risk_usdt / distance
    leverage = max(1, min(RANGE_MAX_LEVERAGE, int(leverage)))
    max_notional = balance * RANGE_MAX_MARGIN_SHARE * leverage
    quantity = min(quantity_by_risk, max_notional / entry)
    notional = quantity * entry
    return {
        "risk_usdt": round(risk_usdt, 6),
        "quantity": round(quantity, 10),
        "notional": round(notional, 6),
        "margin": round(notional / leverage, 6),
        "leverage": leverage,
    }


def net_profit_gate(*, side: str, entry: float, target: float, quantity: float, notional: float, risk_usdt: float) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    pnl = base.calculate_trade_pnl(
        side=side,
        entry=entry,
        exit_price=target,
        quantity=quantity,
        notional=notional,
        opened_at=now,
        closed_at=now + timedelta(minutes=30),
    )
    gross = max(0.0, pnl["gross_pnl"])
    costs = pnl["fees"] + pnl["slippage"] + pnl["funding_estimate"]
    min_required = max(MIN_NET_PROFIT_USDT, risk_usdt * MIN_NET_TO_RISK)
    cost_share = costs / gross if gross > 0 else math.inf
    allowed = pnl["net_pnl"] >= min_required and gross > 0 and cost_share <= MAX_COST_SHARE_OF_GROSS
    return {
        "allowed": allowed,
        "projected_net_pnl": pnl["net_pnl"],
        "projected_gross_pnl": pnl["gross_pnl"],
        "projected_costs": round(costs, 6),
        "min_required_net": round(min_required, 6),
        "cost_share_of_gross": round(cost_share, 4) if math.isfinite(cost_share) else None,
    }


async def ensure_range_schema(db: AsyncSession) -> None:
    await base.ensure_paper_schema(db)
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_range_signals (
            id UUID PRIMARY KEY,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            entry_reference NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            take_profit NUMERIC(30,12) NOT NULL,
            range_low NUMERIC(30,12) NOT NULL,
            range_high NUMERIC(30,12) NOT NULL,
            score NUMERIC(10,4) NOT NULL,
            quote_volume NUMERIC(30,4),
            status VARCHAR(16) NOT NULL DEFAULT 'NEW',
            skip_reason VARCHAR(80),
            projected_net_pnl NUMERIC(24,8),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_paper_range_status ON paper_range_signals(status, observed_at DESC)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_paper_range_symbol ON paper_range_signals(symbol, observed_at DESC)"))
    await db.commit()


async def scan_all_eligible_ranges(db: AsyncSession, *, force: bool = False) -> dict[str, Any]:
    global _last_scan_monotonic, _last_scan_result
    await ensure_range_schema(db)
    now_mono = time.monotonic()
    if not force and now_mono - _last_scan_monotonic < RANGE_SCAN_INTERVAL_SECONDS:
        return {**_last_scan_result, "cached": True}

    tickers = await binance_client.ticker_24h()
    universe = []
    for t in tickers:
        symbol = str(t.get("symbol") or "").upper()
        quote_volume = _f(t.get("quoteVolume"))
        if not symbol.endswith("USDT") or "_" in symbol or quote_volume < MIN_QUOTE_VOLUME_USDT:
            continue
        universe.append((symbol, quote_volume))

    semaphore = asyncio.Semaphore(10)
    candidates: list[tuple[str, float, dict[str, Any]]] = []
    errors = 0

    async def inspect(symbol: str, quote_volume: float) -> None:
        nonlocal errors
        try:
            async with semaphore:
                rows = await binance_client.klines(symbol, interval="5m", limit=96)
            result = analyze_range_klines(rows)
            if result.get("actionable"):
                candidates.append((symbol, quote_volume, result))
        except Exception:
            errors += 1

    await asyncio.gather(*(inspect(symbol, volume) for symbol, volume in universe))

    inserted = 0
    for symbol, quote_volume, result in sorted(candidates, key=lambda item: item[2]["score"], reverse=True):
        duplicate = (await db.execute(text("""
            SELECT 1 FROM paper_range_signals
            WHERE symbol=:symbol AND side=:side AND observed_at >= NOW() - INTERVAL '20 minutes'
            LIMIT 1
        """), {"symbol": symbol, "side": result["side"]})).scalar_one_or_none()
        if duplicate:
            continue
        await db.execute(text("""
            INSERT INTO paper_range_signals (
                id, symbol, side, entry_reference, stop_loss, take_profit, range_low, range_high,
                score, quote_volume, metadata
            ) VALUES (
                CAST(:id AS UUID), :symbol, :side, :entry_reference, :stop_loss, :take_profit,
                :range_low, :range_high, :score, :quote_volume, CAST(:metadata AS JSONB)
            )
        """), {
            "id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": result["side"],
            "entry_reference": result["entry_reference"],
            "stop_loss": result["stop_loss"],
            "take_profit": result["take_profit"],
            "range_low": result["range_low"],
            "range_high": result["range_high"],
            "score": result["score"],
            "quote_volume": quote_volume,
            "metadata": json.dumps(result),
        })
        inserted += 1
    await db.commit()

    _last_scan_monotonic = now_mono
    _last_scan_result = {
        "universe": len(universe),
        "scanned": len(universe),
        "candidates": len(candidates),
        "inserted": inserted,
        "errors": errors,
        "data_source": binance_client.active_source,
    }
    return dict(_last_scan_result)


async def open_range_positions(db: AsyncSession) -> dict[str, Any]:
    await ensure_range_schema(db)
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one())
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if slots <= 0:
        return {"opened": 0, "skipped": 0}

    rows = (await db.execute(text("""
        SELECT * FROM paper_range_signals
        WHERE status='NEW' AND observed_at >= NOW() - INTERVAL '12 minutes'
        ORDER BY score DESC, observed_at ASC
        LIMIT :limit
    """), {"limit": slots * 4})).mappings().all()

    opened = 0
    skipped = 0
    for raw in rows:
        if opened >= slots:
            break
        row = dict(raw)
        already_open = (await db.execute(text("SELECT 1 FROM paper_positions WHERE status='OPEN' AND symbol=:symbol LIMIT 1"), {"symbol": row["symbol"]})).scalar_one_or_none()
        if already_open:
            await db.execute(text("UPDATE paper_range_signals SET status='SKIPPED', skip_reason='symbol_already_open' WHERE id=:id"), {"id": row["id"]})
            skipped += 1
            continue

        fill = await base._latest_price(row["symbol"])
        side = str(row["side"]).upper()
        stop, target = base._f(row["stop_loss"]), base._f(row["take_profit"])
        low, high = base._f(row["range_low"]), base._f(row["range_high"])
        width = high - low
        position = (fill - low) / width if width > 0 else 0.5
        geometry_ok = stop < fill < target if side == "LONG" else target < fill < stop
        edge_ok = position <= 0.28 if side == "LONG" else position >= 0.72
        if fill <= 0 or not geometry_ok or not edge_ok:
            await db.execute(text("UPDATE paper_range_signals SET status='SKIPPED', skip_reason='stale_or_left_edge' WHERE id=:id"), {"id": row["id"]})
            skipped += 1
            continue

        leverage = 2 if base._f(row["score"]) >= 82 else 1
        sizing = size_range_position(balance, fill, stop, leverage)
        gate = net_profit_gate(
            side=side,
            entry=fill,
            target=target,
            quantity=sizing["quantity"],
            notional=sizing["notional"],
            risk_usdt=sizing["risk_usdt"],
        )
        if sizing["quantity"] <= 0 or not gate["allowed"]:
            await db.execute(text("""
                UPDATE paper_range_signals SET status='SKIPPED', skip_reason='net_profit_gate', projected_net_pnl=:net
                WHERE id=:id
            """), {"id": row["id"], "net": gate["projected_net_pnl"]})
            skipped += 1
            continue

        opened_at = datetime.now(timezone.utc)
        metadata = json.dumps({
            "strategy_mode": "RANGE_MICRO",
            "range_signal_id": str(row["id"]),
            "range_score": base._f(row["score"]),
            "projected_net_gate": gate,
            "paper_only": True,
        })
        result = await db.execute(text("""
            INSERT INTO paper_positions (
                signal_id, symbol, side, grade, fingerprint_score, leverage, entry_price, stop_loss,
                take_profit, quantity, notional, margin_used, risk_usdt, opened_at, metadata
            ) VALUES (
                NULL, :symbol, :side, 'RANGE', :score, :leverage, :entry_price, :stop_loss,
                :take_profit, :quantity, :notional, :margin_used, :risk_usdt, :opened_at, CAST(:metadata AS JSONB)
            )
        """), {
            "symbol": row["symbol"],
            "side": side,
            "score": base._f(row["score"]),
            "leverage": sizing["leverage"],
            "entry_price": fill,
            "stop_loss": stop,
            "take_profit": target,
            "quantity": sizing["quantity"],
            "notional": sizing["notional"],
            "margin_used": sizing["margin"],
            "risk_usdt": sizing["risk_usdt"],
            "opened_at": opened_at,
            "metadata": metadata,
        })
        if result.rowcount:
            await db.execute(text("""
                UPDATE paper_range_signals SET status='OPENED', projected_net_pnl=:net WHERE id=:id
            """), {"id": row["id"], "net": gate["projected_net_pnl"]})
            opened += 1

    await db.commit()
    return {"opened": opened, "skipped": skipped}


async def close_expired_range_positions(db: AsyncSession) -> dict[str, int]:
    rows = (await db.execute(text("""
        SELECT id, side, entry_price, quantity, notional, opened_at, symbol
        FROM paper_positions
        WHERE status='OPEN'
          AND metadata->>'strategy_mode'='RANGE_MICRO'
          AND opened_at <= NOW() - INTERVAL '60 minutes'
    """))).mappings().all()
    closed = 0
    now = datetime.now(timezone.utc)
    for raw in rows:
        row = dict(raw)
        exit_price = await base._latest_price(row["symbol"])
        if exit_price <= 0:
            continue
        pnl = base.calculate_trade_pnl(
            side=row["side"],
            entry=base._f(row["entry_price"]),
            exit_price=exit_price,
            quantity=base._f(row["quantity"]),
            notional=base._f(row["notional"]),
            opened_at=row["opened_at"],
            closed_at=now,
        )
        await db.execute(text("""
            UPDATE paper_positions SET status='CLOSED', closed_at=:closed_at, exit_price=:exit_price,
                exit_reason='RANGE_TIME_EXIT', gross_pnl=:gross_pnl, net_pnl=:net_pnl, fees=:fees,
                slippage=:slippage, funding_estimate=:funding_estimate
            WHERE id=:id AND status='OPEN'
        """), {"id": row["id"], "closed_at": now, "exit_price": exit_price, **pnl})
        await db.execute(text("""
            UPDATE paper_accounts SET cash_balance=cash_balance+:net_pnl,
                realized_pnl=realized_pnl+:net_pnl,
                total_fees=total_fees+:fees+:slippage+:funding_estimate,
                updated_at=NOW() WHERE id=1
        """), pnl)
        closed += 1
    await db.commit()
    return {"closed": closed}


async def range_summary(db: AsyncSession) -> dict[str, Any]:
    await ensure_range_schema(db)
    stats = dict((await db.execute(text("""
        SELECT COUNT(*) AS signals,
               COUNT(*) FILTER (WHERE status='OPENED') AS opened,
               COUNT(*) FILTER (WHERE status='SKIPPED') AS skipped,
               COALESCE(AVG(projected_net_pnl) FILTER (WHERE status='OPENED'),0) AS avg_projected_net
        FROM paper_range_signals
        WHERE observed_at >= NOW() - INTERVAL '24 hours'
    """))).mappings().one())
    return {
        "strategy_mode": "RANGE_MICRO",
        "paper_only": True,
        "universe_policy": "all USDT symbols with >= 5M USDT 24h quote volume",
        "scan_interval_seconds": RANGE_SCAN_INTERVAL_SECONDS,
        "min_net_profit_usdt": MIN_NET_PROFIT_USDT,
        "range_max_hold_minutes": RANGE_MAX_HOLD_MINUTES,
        **stats,
    }
