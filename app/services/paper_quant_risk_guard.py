from __future__ import annotations

from math import ceil, log, sqrt
from statistics import mean, stdev
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.paper_portfolio import STARTING_BALANCE, ensure_paper_schema

VERSION = "paper_quant_risk_guard_v1"

MIN_RECENT_SAMPLE = 20
HARD_DRAWDOWN_24H_PCT = 4.0
HARD_LOSS_STREAK = 6
HARD_RECENT_PROFIT_FACTOR = 0.55

REDUCE_HARD_DRAWDOWN_24H_PCT = 2.0
REDUCE_HARD_LOSS_STREAK = 4
REDUCE_HARD_PROFIT_FACTOR = 0.80

REDUCE_DRAWDOWN_24H_PCT = 1.0
REDUCE_LOSS_STREAK = 3
REDUCE_PROFIT_FACTOR = 0.95


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _trade_r(row: dict[str, Any]) -> float | None:
    risk = abs(_f(row.get("risk_usdt")))
    if risk <= 1e-12:
        return None
    return _f(row.get("net_pnl")) / risk


def _loss_streak(rows_desc: list[dict[str, Any]]) -> int:
    streak = 0
    for row in rows_desc:
        if _f(row.get("net_pnl")) < 0:
            streak += 1
        else:
            break
    return streak


def _max_drawdown_pct(rows_asc: list[dict[str, Any]], starting_balance: float = STARTING_BALANCE) -> float:
    equity = float(starting_balance)
    peak = equity
    worst = 0.0
    for row in rows_asc:
        equity += _f(row.get("net_pnl"))
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100.0)
    return worst


def _profit_factor(rows: list[dict[str, Any]]) -> float:
    wins = sum(max(0.0, _f(row.get("net_pnl"))) for row in rows)
    losses = sum(abs(min(0.0, _f(row.get("net_pnl")))) for row in rows)
    if losses <= 1e-12:
        return 999.0 if wins > 0 else 0.0
    return wins / losses


def _trade_ratios(r_values: list[float], trials: int = 1) -> dict[str, Any]:
    n = len(r_values)
    if n == 0:
        return {
            "sample": 0,
            "mean_r": None,
            "trade_sharpe": None,
            "trade_sortino": None,
            "cvar_95_r": None,
            "deflated_sharpe_proxy": None,
            "deflated_sharpe_is_exact": False,
        }

    avg = mean(r_values)
    sigma = stdev(r_values) if n >= 2 else 0.0
    sharpe = avg / sigma if sigma > 1e-12 else (999.0 if avg > 0 else 0.0)

    downside = [min(0.0, x) for x in r_values]
    downside_rms = sqrt(sum(x * x for x in downside) / len(downside)) if downside else 0.0
    sortino = avg / downside_rms if downside_rms > 1e-12 else (999.0 if avg > 0 else 0.0)

    tail_n = max(1, ceil(n * 0.05))
    worst_tail = sorted(r_values)[:tail_n]
    cvar = mean(worst_tail)

    # Conservative multiple-testing penalty. This is intentionally labelled a
    # proxy rather than the exact Bailey/Lopez de Prado Deflated Sharpe Ratio,
    # because ExplodeX does not yet persist the full strategy-search trial set.
    effective_trials = max(1, int(trials))
    penalty = sqrt(2.0 * log(effective_trials) / max(1, n)) if effective_trials > 1 else 0.0
    deflated_proxy = sharpe - penalty if sharpe not in {999.0, -999.0} else sharpe

    return {
        "sample": n,
        "mean_r": round(avg, 4),
        "trade_sharpe": round(sharpe, 4),
        "trade_sortino": round(sortino, 4),
        "cvar_95_r": round(cvar, 4),
        "deflated_sharpe_proxy": round(deflated_proxy, 4),
        "deflated_sharpe_is_exact": False,
        "multiple_testing_trials_proxy": effective_trials,
    }


def build_quant_metrics(
    rows_desc: list[dict[str, Any]],
    *,
    recent_window: int = 20,
    trials: int = 1,
) -> dict[str, Any]:
    recent = list(rows_desc[:max(1, recent_window)])
    r_values = [r for row in recent if (r := _trade_r(row)) is not None]
    ratios = _trade_ratios(r_values, trials=trials)
    net = sum(_f(row.get("net_pnl")) for row in recent)
    winners = sum(1 for row in recent if _f(row.get("net_pnl")) > 0)
    closed = len(recent)
    rows_asc = list(reversed(rows_desc))

    return {
        **ratios,
        "recent_trades": closed,
        "recent_net_pnl": round(net, 6),
        "recent_expectancy_usdt": round(net / closed, 6) if closed else 0.0,
        "recent_profit_factor": round(_profit_factor(recent), 4),
        "recent_win_rate_pct": round(winners / closed * 100.0, 2) if closed else None,
        "consecutive_losses": _loss_streak(rows_desc),
        "max_drawdown_all_pct": round(_max_drawdown_pct(rows_asc), 4),
    }


def evaluate_quant_guard(
    *,
    metrics: dict[str, Any],
    net_24h: float,
    starting_balance: float = STARTING_BALANCE,
) -> dict[str, Any]:
    drawdown_24h_pct = max(0.0, -_f(net_24h) / max(1e-9, float(starting_balance)) * 100.0)
    trades = int(metrics.get("recent_trades") or 0)
    pf = _f(metrics.get("recent_profit_factor"))
    expectancy = _f(metrics.get("recent_expectancy_usdt"))
    streak = int(metrics.get("consecutive_losses") or 0)
    cvar_r = metrics.get("cvar_95_r")
    cvar_r_f = _f(cvar_r) if cvar_r is not None else None

    hard_reasons: list[str] = []
    reduce_hard_reasons: list[str] = []
    reduce_reasons: list[str] = []

    if drawdown_24h_pct >= HARD_DRAWDOWN_24H_PCT:
        hard_reasons.append("daily_drawdown_ge_4pct")
    if streak >= HARD_LOSS_STREAK:
        hard_reasons.append("six_consecutive_losses")
    if trades >= MIN_RECENT_SAMPLE and expectancy < 0 and pf < HARD_RECENT_PROFIT_FACTOR:
        hard_reasons.append("recent_profit_factor_below_0_55")

    if drawdown_24h_pct >= REDUCE_HARD_DRAWDOWN_24H_PCT:
        reduce_hard_reasons.append("daily_drawdown_ge_2pct")
    if streak >= REDUCE_HARD_LOSS_STREAK:
        reduce_hard_reasons.append("four_consecutive_losses")
    if trades >= MIN_RECENT_SAMPLE and expectancy < 0 and pf < REDUCE_HARD_PROFIT_FACTOR:
        reduce_hard_reasons.append("recent_profit_factor_below_0_80")
    if trades >= MIN_RECENT_SAMPLE and cvar_r_f is not None and cvar_r_f <= -1.35:
        reduce_hard_reasons.append("cvar95_worse_than_minus_1_35r")

    if drawdown_24h_pct >= REDUCE_DRAWDOWN_24H_PCT:
        reduce_reasons.append("daily_drawdown_ge_1pct")
    if streak >= REDUCE_LOSS_STREAK:
        reduce_reasons.append("three_consecutive_losses")
    if trades >= MIN_RECENT_SAMPLE and expectancy < 0 and pf < REDUCE_PROFIT_FACTOR:
        reduce_reasons.append("recent_profit_factor_below_0_95")

    if hard_reasons:
        state = "HALT_NEW_ENTRIES"
        risk_multiplier = 0.0
    elif reduce_hard_reasons:
        state = "REDUCE_HARD"
        risk_multiplier = 0.25
    elif reduce_reasons:
        state = "REDUCE"
        risk_multiplier = 0.50
    else:
        state = "ALLOW"
        risk_multiplier = 1.0

    return {
        "version": VERSION,
        "paper_only": True,
        "state": state,
        "halt_new_entries": state == "HALT_NEW_ENTRIES",
        "risk_multiplier": risk_multiplier,
        "drawdown_24h_pct": round(drawdown_24h_pct, 4),
        "hard_reasons": hard_reasons,
        "reduce_hard_reasons": reduce_hard_reasons,
        "reduce_reasons": reduce_reasons,
        "metrics": metrics,
        "can_create_entry": False,
        "can_change_direction": False,
        "manages_existing_exits": False,
        "rule": "Quant guard can only reduce or halt NEW PAPER entries. Existing positions remain managed by the canonical Heart/PAPER exit logic.",
    }


async def paper_quant_risk_guard(db: AsyncSession, *, limit: int = 500) -> dict[str, Any]:
    await ensure_paper_schema(db)
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT net_pnl, risk_usdt, side, grade, closed_at,
               COALESCE(NULLIF(metadata->>'strategy_mode',''), 'UNKNOWN') AS strategy_mode
        FROM paper_positions
        WHERE status='CLOSED'
        ORDER BY closed_at DESC
        LIMIT :limit
    """), {"limit": max(20, min(limit, 5000))})).mappings().all()]

    cohorts = {
        (str(row.get("strategy_mode") or "UNKNOWN"), str(row.get("side") or ""), str(row.get("grade") or ""))
        for row in rows
    }
    metrics = build_quant_metrics(rows, recent_window=20, trials=max(1, len(cohorts)))

    net_24h = _f((await db.execute(text("""
        SELECT COALESCE(SUM(net_pnl),0)
        FROM paper_positions
        WHERE status='CLOSED' AND closed_at >= NOW() - INTERVAL '24 hours'
    """))).scalar_one())

    guard = evaluate_quant_guard(metrics=metrics, net_24h=net_24h)
    return {
        **guard,
        "net_24h": round(net_24h, 6),
        "cohort_trials_proxy": max(1, len(cohorts)),
        "statistics_note": "Sharpe/Sortino are per-trade R-multiple diagnostics, not annualized portfolio ratios. Deflated Sharpe is a conservative proxy until the full strategy-search trial history is persisted.",
    }
