from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.paper_portfolio import STARTING_BALANCE, ensure_paper_schema

LOSS_AUTOPSY_VERSION = "paper_loss_autopsy_v1"

MIN_EXACT_SAMPLE = 6
MIN_CONTEXT_SAMPLE = 10
MIN_STRATEGY_SAMPLE = 15
HARD_VETO_STOP_RATE = 0.70
REDUCE_STOP_RATE = 0.60
DEFENSIVE_24H_DRAWDOWN_PCT = 2.0
DEFENSIVE_RECENT_TRADES = 20
DEFENSIVE_RECENT_PF = 0.80


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_ratio(num: float, den: float, *, empty: float = 0.0) -> float:
    if den <= 0:
        return empty
    return num / den


def normalize_setup_type(*, strategy_mode: str, setup_type: str | None, grade: str | None = None) -> str:
    strategy = str(strategy_mode or "TREND_PREMOVE").upper()
    explicit = str(setup_type or "").strip().upper()
    if explicit:
        return explicit
    grade_value = str(grade or "").strip().upper()
    if strategy == "RANGE_MICRO":
        return "RANGE"
    if strategy == "MICRO_SCALP":
        return grade_value or "MICRO"
    return grade_value or "GENERAL"


def score_bucket(score: float) -> str:
    floor = int(max(0.0, min(100.0, _f(score))) // 10 * 10)
    ceiling = min(100, floor + 9)
    return f"{floor}-{ceiling}"


def metrics_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trades = len(rows)
    if not trades:
        return {
            "trades": 0,
            "stops": 0,
            "tp1": 0,
            "wins": 0,
            "losses": 0,
            "stop_rate": 0.0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "expectancy_net": 0.0,
            "profit_factor": 0.0,
            "costs": 0.0,
            "cost_share_of_gross_wins": 0.0,
        }

    stops = 0
    tp1 = 0
    wins = 0
    losses = 0
    net_pnl = 0.0
    positive = 0.0
    negative_abs = 0.0
    costs = 0.0
    for row in rows:
        reason = str(row.get("exit_reason") or "").upper()
        pnl = _f(row.get("net_pnl"))
        if reason in {"STOP", "AMBIGUOUS_STOP"}:
            stops += 1
        if reason == "TP1":
            tp1 += 1
        if pnl > 0:
            wins += 1
            positive += pnl
        else:
            losses += 1
            negative_abs += abs(pnl)
        net_pnl += pnl
        costs += _f(row.get("fees")) + _f(row.get("slippage")) + _f(row.get("funding_estimate"))

    return {
        "trades": trades,
        "stops": stops,
        "tp1": tp1,
        "wins": wins,
        "losses": losses,
        "stop_rate": round(stops / trades, 4),
        "win_rate": round(wins / trades, 4),
        "net_pnl": round(net_pnl, 6),
        "expectancy_net": round(net_pnl / trades, 6),
        "profit_factor": round(positive / negative_abs, 4) if negative_abs > 0 else (999.0 if positive > 0 else 0.0),
        "costs": round(costs, 6),
        "cost_share_of_gross_wins": round(costs / positive, 4) if positive > 0 else None,
    }


def classify_loss_gate(
    *,
    exact: dict[str, Any],
    context: dict[str, Any],
    strategy: dict[str, Any],
    recent_symbol_stops: int = 0,
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    veto_reasons: list[str] = []
    warnings: list[str] = []
    risk_multiplier = 1.0

    cohorts = (
        ("exact", exact, MIN_EXACT_SAMPLE),
        ("context", context, MIN_CONTEXT_SAMPLE),
        ("strategy", strategy, MIN_STRATEGY_SAMPLE),
    )
    for name, metrics, min_sample in cohorts:
        trades = int(metrics.get("trades") or 0)
        if trades < min_sample:
            continue
        stop_rate = _f(metrics.get("stop_rate"))
        expectancy = _f(metrics.get("expectancy_net"))
        pf = _f(metrics.get("profit_factor"))
        evidence.append({"cohort": name, **metrics})

        if stop_rate >= HARD_VETO_STOP_RATE and expectancy < 0 and pf < 0.80:
            veto_reasons.append(f"{name}_repeated_stops")
        elif stop_rate >= REDUCE_STOP_RATE and expectancy < 0:
            risk_multiplier = min(risk_multiplier, 0.35)
            warnings.append(f"{name}_negative_expectancy")
        elif expectancy < 0 and pf < 0.95:
            risk_multiplier = min(risk_multiplier, 0.60)
            warnings.append(f"{name}_weak_profit_factor")

    if recent_symbol_stops >= 3:
        veto_reasons.append("symbol_stop_streak")
    elif recent_symbol_stops == 2:
        risk_multiplier = min(risk_multiplier, 0.35)
        warnings.append("symbol_two_recent_stops")

    veto = bool(veto_reasons)
    if veto:
        risk_multiplier = 0.0
        state = "VETO"
    elif risk_multiplier <= 0.35:
        state = "REDUCE_HARD"
    elif risk_multiplier < 1.0:
        state = "REDUCE"
    else:
        state = "ALLOW"

    return {
        "version": LOSS_AUTOPSY_VERSION,
        "paper_only": True,
        "state": state,
        "veto": veto,
        "risk_multiplier": round(risk_multiplier, 3),
        "veto_reasons": veto_reasons,
        "warnings": warnings,
        "evidence": evidence,
        "creates_entry": False,
        "changes_real_money_rules": False,
    }


def classify_portfolio_brake(*, net_24h: float, recent: dict[str, Any]) -> dict[str, Any]:
    drawdown_pct = max(0.0, -_f(net_24h) / STARTING_BALANCE * 100.0)
    recent_trades = int(recent.get("trades") or 0)
    recent_pf = _f(recent.get("profit_factor"))
    recent_expectancy = _f(recent.get("expectancy_net"))
    recent_stop_rate = _f(recent.get("stop_rate"))

    defensive = bool(
        drawdown_pct >= DEFENSIVE_24H_DRAWDOWN_PCT
        or (
            recent_trades >= DEFENSIVE_RECENT_TRADES
            and recent_expectancy < 0
            and recent_pf < DEFENSIVE_RECENT_PF
        )
        or (recent_trades >= DEFENSIVE_RECENT_TRADES and recent_stop_rate >= 0.65)
    )

    if defensive:
        return {
            "mode": "DEFENSIVE",
            "secondary_entries_enabled": False,
            "trend_risk_multiplier": 0.50,
            "drawdown_24h_pct": round(drawdown_pct, 3),
            "reason": "El PAPER está en drawdown o con expectativa reciente negativa; sigue escaneando pero reduce nuevas exposiciones.",
        }
    return {
        "mode": "NORMAL",
        "secondary_entries_enabled": True,
        "trend_risk_multiplier": 1.0,
        "drawdown_24h_pct": round(drawdown_pct, 3),
        "reason": "No hay evidencia suficiente para activar el freno global.",
    }


async def _closed_rows(db: AsyncSession, *, days: int = 30, limit: int = 5000) -> list[dict[str, Any]]:
    await ensure_paper_schema(db)
    rows = (await db.execute(text("""
        SELECT id, symbol, side, grade, fingerprint_score, exit_reason, net_pnl,
               fees, slippage, funding_estimate, closed_at,
               COALESCE(NULLIF(metadata->>'strategy_mode',''), 'TREND_PREMOVE') AS strategy_mode,
               COALESCE(
                   NULLIF(metadata->>'micro_setup_type',''),
                   NULLIF(metadata->>'setup_type',''),
                   NULLIF(grade,''),
                   'GENERAL'
               ) AS raw_setup_type
        FROM paper_positions
        WHERE status='CLOSED'
          AND closed_at >= NOW() - (:days * INTERVAL '1 day')
        ORDER BY closed_at DESC
        LIMIT :limit
    """), {"days": max(1, min(days, 365)), "limit": max(1, min(limit, 20000))})).mappings().all()

    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        row["strategy_mode"] = str(row.get("strategy_mode") or "TREND_PREMOVE").upper()
        row["setup_type"] = normalize_setup_type(
            strategy_mode=row["strategy_mode"],
            setup_type=row.get("raw_setup_type"),
            grade=row.get("grade"),
        )
        row["score_bucket"] = score_bucket(_f(row.get("fingerprint_score")))
        out.append(row)
    return out


async def evaluate_anti_loss_gate(
    db: AsyncSession,
    *,
    symbol: str,
    side: str,
    strategy_mode: str,
    setup_type: str | None,
    score: float,
) -> dict[str, Any]:
    rows = await _closed_rows(db, days=90)
    symbol = symbol.upper()
    side = side.upper()
    strategy_mode = str(strategy_mode or "TREND_PREMOVE").upper()
    setup = normalize_setup_type(strategy_mode=strategy_mode, setup_type=setup_type)
    bucket = score_bucket(score)

    exact_rows = [
        r for r in rows
        if r["symbol"] == symbol
        and r["side"] == side
        and r["strategy_mode"] == strategy_mode
        and r["setup_type"] == setup
        and r["score_bucket"] == bucket
    ]
    context_rows = [
        r for r in rows
        if r["side"] == side
        and r["strategy_mode"] == strategy_mode
        and r["setup_type"] == setup
        and r["score_bucket"] == bucket
    ]
    strategy_rows = [
        r for r in rows
        if r["side"] == side and r["strategy_mode"] == strategy_mode
    ]

    same_symbol_recent = [
        r for r in rows
        if r["symbol"] == symbol and r["side"] == side and r["strategy_mode"] == strategy_mode
    ][:3]
    recent_symbol_stops = 0
    for row in same_symbol_recent:
        if str(row.get("exit_reason") or "").upper() in {"STOP", "AMBIGUOUS_STOP"}:
            recent_symbol_stops += 1
        else:
            break

    gate = classify_loss_gate(
        exact=metrics_from_rows(exact_rows),
        context=metrics_from_rows(context_rows),
        strategy=metrics_from_rows(strategy_rows),
        recent_symbol_stops=recent_symbol_stops,
    )
    return {
        **gate,
        "symbol": symbol,
        "side": side,
        "strategy_mode": strategy_mode,
        "setup_type": setup,
        "score_bucket": bucket,
        "recent_symbol_stop_streak": recent_symbol_stops,
    }


async def portfolio_loss_brake(db: AsyncSession) -> dict[str, Any]:
    rows = await _closed_rows(db, days=2, limit=1000)
    net_24h_row = (await db.execute(text("""
        SELECT COALESCE(SUM(net_pnl),0) AS net_24h
        FROM paper_positions
        WHERE status='CLOSED' AND closed_at >= NOW() - INTERVAL '24 hours'
    """))).mappings().one()
    recent = metrics_from_rows(rows[:DEFENSIVE_RECENT_TRADES])
    return {
        **classify_portfolio_brake(net_24h=_f(net_24h_row.get("net_24h")), recent=recent),
        "recent_metrics": recent,
    }


async def loss_autopsy_report(db: AsyncSession, *, days: int = 30) -> dict[str, Any]:
    rows = await _closed_rows(db, days=days)
    overall = metrics_from_rows(rows)

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    symbol_grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["strategy_mode"], row["side"], row["setup_type"], row["score_bucket"])].append(row)
        symbol_grouped[(row["symbol"], row["side"], row["strategy_mode"])].append(row)

    patterns: list[dict[str, Any]] = []
    for key, items in grouped.items():
        metrics = metrics_from_rows(items)
        patterns.append({
            "strategy_mode": key[0],
            "side": key[1],
            "setup_type": key[2],
            "score_bucket": key[3],
            **metrics,
        })
    patterns.sort(
        key=lambda item: (
            item["trades"] >= MIN_CONTEXT_SAMPLE,
            item["stop_rate"],
            -item["expectancy_net"],
            item["trades"],
        ),
        reverse=True,
    )

    symbols: list[dict[str, Any]] = []
    for key, items in symbol_grouped.items():
        metrics = metrics_from_rows(items)
        symbols.append({
            "symbol": key[0],
            "side": key[1],
            "strategy_mode": key[2],
            **metrics,
        })
    symbols.sort(
        key=lambda item: (
            item["trades"] >= MIN_EXACT_SAMPLE,
            item["stop_rate"],
            -item["expectancy_net"],
            item["trades"],
        ),
        reverse=True,
    )

    brake = await portfolio_loss_brake(db)
    return {
        "version": LOSS_AUTOPSY_VERSION,
        "paper_only": True,
        "window_days": max(1, min(days, 365)),
        "overall": overall,
        "portfolio_brake": brake,
        "worst_patterns": patterns[:20],
        "worst_symbol_directions": symbols[:20],
        "policy": {
            "hard_veto_min_stop_rate_pct": HARD_VETO_STOP_RATE * 100.0,
            "risk_reduce_min_stop_rate_pct": REDUCE_STOP_RATE * 100.0,
            "exact_min_sample": MIN_EXACT_SAMPLE,
            "context_min_sample": MIN_CONTEXT_SAMPLE,
            "strategy_min_sample": MIN_STRATEGY_SAMPLE,
            "three_consecutive_symbol_stops_veto": True,
            "two_consecutive_symbol_stops_reduce": True,
            "defensive_24h_drawdown_pct": DEFENSIVE_24H_DRAWDOWN_PCT,
        },
        "note": "Aprende de resultados PAPER cerrados. No estima probabilidad futura ni modifica trading real.",
    }
