from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import paper_portfolio as base
from app.services.binance import binance_client

MICRO_SCAN_INTERVAL_SECONDS = 180
MICRO_UNIVERSE_LIMIT = 40
MIN_QUOTE_VOLUME_USDT = 8_000_000.0
MICRO_RISK_PER_TRADE = 0.001  # 0.10% of PAPER balance
MICRO_MAX_MARGIN_SHARE = 0.12
MICRO_MAX_LEVERAGE = 2
MICRO_MAX_HOLD_MINUTES = 35
MIN_NET_PROFIT_USDT = 0.25
MIN_NET_TO_RISK = 0.30
MAX_COST_SHARE_OF_GROSS = 0.50
STANDARD_SCORE = 62.0
EXPLORATION_SCORE = 50.0

_last_scan_monotonic = 0.0
_last_scan_result: dict[str, Any] = {
    "universe": 0,
    "scanned": 0,
    "standard_candidates": 0,
    "exploration_candidates": 0,
    "inserted": 0,
    "errors": 0,
    "rejection_reasons": {},
}


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


def analyze_micro_scalp(rows: list[list[Any]]) -> dict[str, Any]:
    usable = [row for row in rows if len(row) >= 6][-72:]
    if len(usable) < 40:
        return {"eligible": False, "reason": "insufficient_history"}

    closes = [_f(row[4]) for row in usable]
    highs = [_f(row[2]) for row in usable]
    lows = [_f(row[3]) for row in usable]
    opens = [_f(row[1]) for row in usable]
    volumes = [_f(row[5]) for row in usable]
    last = closes[-1]
    if last <= 0:
        return {"eligible": False, "reason": "invalid_price"}

    atr = _atr(usable)
    atr_pct = atr / last * 100.0 if last > 0 else 0.0
    if atr <= 0 or atr_pct < 0.06:
        return {"eligible": False, "reason": "too_dead", "atr_pct": round(atr_pct, 4)}
    if atr_pct > 3.0:
        return {"eligible": False, "reason": "too_volatile", "atr_pct": round(atr_pct, 4)}

    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    e9 = ema9[-1]
    e21 = ema21[-1]
    e9_prev = ema9[-4]
    slope = e9 - e9_prev
    momentum_3_pct = (last / closes[-4] - 1.0) * 100.0 if closes[-4] else 0.0

    recent_high = max(highs[-18:])
    recent_low = min(lows[-18:])
    recent_width = max(1e-12, recent_high - recent_low)
    range_position = (last - recent_low) / recent_width

    avg_volume = sum(volumes[-20:-1]) / max(1, len(volumes[-20:-1]))
    volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 1.0
    if volume_ratio < 0.30:
        return {"eligible": False, "reason": "low_recent_volume", "volume_ratio": round(volume_ratio, 4)}
    if volume_ratio > 4.5:
        return {"eligible": False, "reason": "volume_spike", "volume_ratio": round(volume_ratio, 4)}

    last_body = abs(closes[-1] - opens[-1])
    distance_ema_atr = abs(last - e9) / atr if atr > 0 else 99.0
    if distance_ema_atr > 1.8 or last_body > atr * 2.0:
        return {"eligible": False, "reason": "chase_risk", "distance_ema_atr": round(distance_ema_atr, 4)}

    trend_long = e9 > e21 and slope > 0
    trend_short = e9 < e21 and slope < 0
    side: str | None = None
    setup_type = ""

    if trend_long and momentum_3_pct > -0.18:
        side = "LONG"
        setup_type = "MICRO_TREND"
    elif trend_short and momentum_3_pct < 0.18:
        side = "SHORT"
        setup_type = "MICRO_TREND"
    elif range_position <= 0.22 and momentum_3_pct >= -0.30:
        side = "LONG"
        setup_type = "MICRO_MEAN_REVERSION"
    elif range_position >= 0.78 and momentum_3_pct <= 0.30:
        side = "SHORT"
        setup_type = "MICRO_MEAN_REVERSION"
    elif abs(momentum_3_pct) >= 0.05:
        side = "LONG" if momentum_3_pct > 0 else "SHORT"
        setup_type = "MICRO_MOMENTUM"

    if side is None:
        return {"eligible": False, "reason": "no_direction"}

    alignment = 1.0 if ((side == "LONG" and trend_long) or (side == "SHORT" and trend_short)) else 0.55
    momentum_abs = abs(momentum_3_pct)
    momentum_quality = max(0.0, min(1.0, 1.0 - abs(momentum_abs - 0.28) / 0.75))
    proximity_quality = max(0.0, 1.0 - distance_ema_atr / 1.8)
    volume_quality = max(0.0, min(1.0, 1.0 - abs(volume_ratio - 1.1) / 2.4))
    volatility_quality = max(0.0, min(1.0, 1.0 - abs(atr_pct - 0.55) / 2.5))
    candle_confirms = (side == "LONG" and closes[-1] >= opens[-1]) or (side == "SHORT" and closes[-1] <= opens[-1])
    candle_quality = 1.0 if candle_confirms else 0.35

    score = 100.0 * (
        alignment * 0.28
        + momentum_quality * 0.18
        + proximity_quality * 0.20
        + volume_quality * 0.12
        + volatility_quality * 0.10
        + candle_quality * 0.12
    )

    target_distance = max(last * 0.0040, atr * 0.90)
    stop_distance = max(last * 0.0032, atr * 0.65)
    target_distance = min(target_distance, last * 0.012)
    stop_distance = min(stop_distance, last * 0.009)

    if setup_type == "MICRO_MEAN_REVERSION":
        midpoint = (recent_high + recent_low) / 2.0
        structural_distance = midpoint - last if side == "LONG" else last - midpoint
        if structural_distance > target_distance * 0.75:
            target_distance = min(max(target_distance, structural_distance), last * 0.012)

    if side == "LONG":
        stop = last - stop_distance
        target = last + target_distance
    else:
        stop = last + stop_distance
        target = last - target_distance

    tier = "STANDARD" if score >= STANDARD_SCORE else "EXPLORATION" if score >= EXPLORATION_SCORE else "REJECT"
    return {
        "eligible": tier != "REJECT",
        "actionable": tier == "STANDARD",
        "strategy_mode": "MICRO_SCALP",
        "setup_type": setup_type,
        "tier": tier,
        "side": side,
        "score": round(score, 2),
        "entry_reference": last,
        "stop_loss": stop,
        "take_profit": target,
        "stop_distance": stop_distance,
        "target_distance": target_distance,
        "atr_pct": round(atr_pct, 4),
        "momentum_3_pct": round(momentum_3_pct, 4),
        "volume_ratio": round(volume_ratio, 4),
        "distance_ema_atr": round(distance_ema_atr, 4),
        "range_position": round(range_position, 4),
        "recent_low": recent_low,
        "recent_high": recent_high,
    }


def size_micro_position(balance: float, entry: float, stop: float, leverage: int = 2) -> dict[str, float]:
    distance = abs(entry - stop)
    if balance <= 0 or entry <= 0 or distance <= 0:
        return {"risk_usdt": 0.0, "quantity": 0.0, "notional": 0.0, "margin": 0.0, "leverage": 1}
    leverage = max(1, min(MICRO_MAX_LEVERAGE, int(leverage)))
    risk_usdt = balance * MICRO_RISK_PER_TRADE
    quantity_by_risk = risk_usdt / distance
    max_notional = balance * MICRO_MAX_MARGIN_SHARE * leverage
    quantity = min(quantity_by_risk, max_notional / entry)
    notional = quantity * entry
    return {
        "risk_usdt": round(risk_usdt, 6),
        "quantity": round(quantity, 10),
        "notional": round(notional, 6),
        "margin": round(notional / leverage, 6),
        "leverage": leverage,
    }


def micro_net_profit_gate(*, side: str, entry: float, target: float, quantity: float, notional: float, risk_usdt: float) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    pnl = base.calculate_trade_pnl(
        side=side,
        entry=entry,
        exit_price=target,
        quantity=quantity,
        notional=notional,
        opened_at=now,
        closed_at=now + timedelta(minutes=20),
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


async def ensure_micro_schema(db: AsyncSession) -> None:
    await base.ensure_paper_schema(db)
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_micro_signals (
            id UUID PRIMARY KEY,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            entry_reference NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            take_profit NUMERIC(30,12) NOT NULL,
            score NUMERIC(10,4) NOT NULL,
            tier VARCHAR(16) NOT NULL,
            setup_type VARCHAR(40),
            quote_volume NUMERIC(30,4),
            status VARCHAR(16) NOT NULL DEFAULT 'NEW',
            skip_reason VARCHAR(80),
            projected_net_pnl NUMERIC(24,8),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_paper_micro_status ON paper_micro_signals(status, observed_at DESC)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_paper_micro_symbol ON paper_micro_signals(symbol, observed_at DESC)"))
    await db.commit()


async def scan_micro_scalps(db: AsyncSession, *, force: bool = False) -> dict[str, Any]:
    global _last_scan_monotonic, _last_scan_result
    await ensure_micro_schema(db)
    now_mono = time.monotonic()
    if not force and now_mono - _last_scan_monotonic < MICRO_SCAN_INTERVAL_SECONDS:
        return {**_last_scan_result, "cached": True}

    tickers = await binance_client.ticker_24h()
    universe: list[tuple[str, float]] = []
    for ticker in tickers:
        symbol = str(ticker.get("symbol") or "").upper()
        quote_volume = _f(ticker.get("quoteVolume"))
        if not symbol.endswith("USDT") or "_" in symbol or quote_volume < MIN_QUOTE_VOLUME_USDT:
            continue
        universe.append((symbol, quote_volume))
    universe.sort(key=lambda item: item[1], reverse=True)
    universe = universe[:MICRO_UNIVERSE_LIMIT]

    semaphore = asyncio.Semaphore(8)
    standard: list[tuple[str, float, dict[str, Any]]] = []
    exploration: list[tuple[str, float, dict[str, Any]]] = []
    reasons: Counter[str] = Counter()
    errors = 0

    async def inspect(symbol: str, quote_volume: float) -> None:
        nonlocal errors
        try:
            async with semaphore:
                rows = await binance_client.klines(symbol, interval="5m", limit=72)
            result = analyze_micro_scalp(rows)
            if result.get("actionable"):
                standard.append((symbol, quote_volume, result))
            elif result.get("eligible"):
                exploration.append((symbol, quote_volume, result))
            else:
                reasons[str(result.get("reason") or "rejected")] += 1
        except Exception:
            errors += 1
            reasons["provider_error"] += 1

    await asyncio.gather(*(inspect(symbol, volume) for symbol, volume in universe))
    standard.sort(key=lambda item: item[2]["score"], reverse=True)
    exploration.sort(key=lambda item: item[2]["score"], reverse=True)

    # STANDARD candidates are preferred. If the market gives none, PAPER mode is
    # allowed to collect data on the best exploratory setups with much smaller risk.
    selected = standard[:8]
    if len(selected) < 3:
        selected.extend(exploration[: 3 - len(selected)])

    inserted = 0
    for symbol, quote_volume, result in selected:
        duplicate = (await db.execute(text("""
            SELECT 1 FROM paper_micro_signals
            WHERE symbol=:symbol AND side=:side AND observed_at >= NOW() - INTERVAL '15 minutes'
            LIMIT 1
        """), {"symbol": symbol, "side": result["side"]})).scalar_one_or_none()
        if duplicate:
            reasons["recent_duplicate"] += 1
            continue
        await db.execute(text("""
            INSERT INTO paper_micro_signals (
                id, symbol, side, entry_reference, stop_loss, take_profit, score, tier,
                setup_type, quote_volume, metadata
            ) VALUES (
                CAST(:id AS UUID), :symbol, :side, :entry_reference, :stop_loss, :take_profit,
                :score, :tier, :setup_type, :quote_volume, CAST(:metadata AS JSONB)
            )
        """), {
            "id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": result["side"],
            "entry_reference": result["entry_reference"],
            "stop_loss": result["stop_loss"],
            "take_profit": result["take_profit"],
            "score": result["score"],
            "tier": result["tier"],
            "setup_type": result["setup_type"],
            "quote_volume": quote_volume,
            "metadata": json.dumps(result),
        })
        inserted += 1
    await db.commit()

    _last_scan_monotonic = now_mono
    _last_scan_result = {
        "universe": len(universe),
        "scanned": len(universe),
        "standard_candidates": len(standard),
        "exploration_candidates": len(exploration),
        "selected": len(selected),
        "inserted": inserted,
        "errors": errors,
        "rejection_reasons": dict(reasons.most_common(8)),
        "data_source": binance_client.active_source,
    }
    return dict(_last_scan_result)


async def open_micro_positions(db: AsyncSession) -> dict[str, Any]:
    await ensure_micro_schema(db)
    account = (await db.execute(text("SELECT cash_balance FROM paper_accounts WHERE id=1"))).mappings().first()
    balance = base._f(account["cash_balance"] if account else base.STARTING_BALANCE)
    open_count = int((await db.execute(text("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"))).scalar_one())
    slots = max(0, base.MAX_OPEN_POSITIONS - open_count)
    if slots <= 0:
        return {"opened": 0, "skipped": 0, "no_slots": True}

    rows = (await db.execute(text("""
        SELECT * FROM paper_micro_signals
        WHERE status='NEW' AND observed_at >= NOW() - INTERVAL '10 minutes'
        ORDER BY CASE WHEN tier='STANDARD' THEN 0 ELSE 1 END, score DESC, observed_at ASC
        LIMIT :limit
    """), {"limit": slots * 5})).mappings().all()

    opened = 0
    skipped = 0
    for raw in rows:
        if opened >= slots:
            break
        row = dict(raw)
        already_open = (await db.execute(text("SELECT 1 FROM paper_positions WHERE status='OPEN' AND symbol=:symbol LIMIT 1"), {"symbol": row["symbol"]})).scalar_one_or_none()
        if already_open:
            await db.execute(text("UPDATE paper_micro_signals SET status='SKIPPED', skip_reason='symbol_already_open' WHERE id=:id"), {"id": row["id"]})
            skipped += 1
            continue

        fill = await base._latest_price(row["symbol"])
        entry_ref = base._f(row["entry_reference"])
        if fill <= 0 or entry_ref <= 0:
            skipped += 1
            continue
        stale_move_pct = abs(fill - entry_ref) / entry_ref * 100.0
        if stale_move_pct > 0.80:
            await db.execute(text("UPDATE paper_micro_signals SET status='SKIPPED', skip_reason='stale_move' WHERE id=:id"), {"id": row["id"]})
            skipped += 1
            continue

        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        side = str(row["side"]).upper()
        stop_distance = base._f(meta.get("stop_distance"), abs(entry_ref - base._f(row["stop_loss"])))
        target_distance = base._f(meta.get("target_distance"), abs(base._f(row["take_profit"]) - entry_ref))
        if stop_distance <= 0 or target_distance <= 0:
            skipped += 1
            continue

        stop = fill - stop_distance if side == "LONG" else fill + stop_distance
        target = fill + target_distance if side == "LONG" else fill - target_distance
        leverage = 2
        sizing = size_micro_position(balance, fill, stop, leverage)
        gate = micro_net_profit_gate(
            side=side,
            entry=fill,
            target=target,
            quantity=sizing["quantity"],
            notional=sizing["notional"],
            risk_usdt=sizing["risk_usdt"],
        )
        if sizing["quantity"] <= 0 or not gate["allowed"]:
            await db.execute(text("""
                UPDATE paper_micro_signals SET status='SKIPPED', skip_reason='net_profit_gate', projected_net_pnl=:net
                WHERE id=:id
            """), {"id": row["id"], "net": gate["projected_net_pnl"]})
            skipped += 1
            continue

        opened_at = datetime.now(timezone.utc)
        position_meta = json.dumps({
            "strategy_mode": "MICRO_SCALP",
            "micro_signal_id": str(row["id"]),
            "micro_tier": row.get("tier"),
            "micro_setup_type": row.get("setup_type"),
            "micro_score": base._f(row.get("score")),
            "projected_net_gate": gate,
            "paper_exploration": str(row.get("tier")) == "EXPLORATION",
            "paper_only": True,
        })
        result = await db.execute(text("""
            INSERT INTO paper_positions (
                signal_id, symbol, side, grade, fingerprint_score, leverage, entry_price, stop_loss,
                take_profit, quantity, notional, margin_used, risk_usdt, opened_at, metadata
            ) VALUES (
                NULL, :symbol, :side, :grade, :score, :leverage, :entry_price, :stop_loss,
                :take_profit, :quantity, :notional, :margin_used, :risk_usdt, :opened_at, CAST(:metadata AS JSONB)
            )
        """), {
            "symbol": row["symbol"],
            "side": side,
            "grade": "MICRO" if row.get("tier") == "STANDARD" else "EXPLORE",
            "score": base._f(row.get("score")),
            "leverage": sizing["leverage"],
            "entry_price": fill,
            "stop_loss": stop,
            "take_profit": target,
            "quantity": sizing["quantity"],
            "notional": sizing["notional"],
            "margin_used": sizing["margin"],
            "risk_usdt": sizing["risk_usdt"],
            "opened_at": opened_at,
            "metadata": position_meta,
        })
        if result.rowcount:
            await db.execute(text("UPDATE paper_micro_signals SET status='OPENED', projected_net_pnl=:net WHERE id=:id"), {"id": row["id"], "net": gate["projected_net_pnl"]})
            opened += 1

    await db.commit()
    return {"opened": opened, "skipped": skipped, "no_slots": False}


async def close_expired_micro_positions(db: AsyncSession) -> dict[str, int]:
    rows = (await db.execute(text("""
        SELECT id, side, entry_price, quantity, notional, opened_at, symbol
        FROM paper_positions
        WHERE status='OPEN'
          AND metadata->>'strategy_mode'='MICRO_SCALP'
          AND opened_at <= NOW() - INTERVAL '35 minutes'
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
                exit_reason='MICRO_TIME_EXIT', gross_pnl=:gross_pnl, net_pnl=:net_pnl, fees=:fees,
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


async def micro_summary(db: AsyncSession) -> dict[str, Any]:
    await ensure_micro_schema(db)
    stats = dict((await db.execute(text("""
        SELECT COUNT(*) AS signals,
               COUNT(*) FILTER (WHERE status='OPENED') AS opened,
               COUNT(*) FILTER (WHERE status='SKIPPED') AS skipped,
               COUNT(*) FILTER (WHERE tier='STANDARD') AS standard,
               COUNT(*) FILTER (WHERE tier='EXPLORATION') AS exploration,
               COALESCE(AVG(projected_net_pnl) FILTER (WHERE status='OPENED'),0) AS avg_projected_net
        FROM paper_micro_signals
        WHERE observed_at >= NOW() - INTERVAL '24 hours'
    """))).mappings().one())
    return {
        "strategy_mode": "MICRO_SCALP",
        "paper_only": True,
        "scan_interval_seconds": MICRO_SCAN_INTERVAL_SECONDS,
        "universe_limit": MICRO_UNIVERSE_LIMIT,
        "min_quote_volume_usdt": MIN_QUOTE_VOLUME_USDT,
        "risk_per_trade_pct": MICRO_RISK_PER_TRADE * 100.0,
        "max_leverage": MICRO_MAX_LEVERAGE,
        "max_hold_minutes": MICRO_MAX_HOLD_MINUTES,
        "min_net_profit_usdt": MIN_NET_PROFIT_USDT,
        "last_scan": dict(_last_scan_result),
        **stats,
    }
