from __future__ import annotations

import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.paper_portfolio import ensure_paper_schema

EDGE_LAB_VERSION = "adaptive_edge_lab_v1"
MIN_BUCKET_SAMPLE = 15
PROMISING_SAMPLE = 20
PROVEN_SAMPLE = 40
EIGHTY_TARGET_SAMPLE = 50


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    """95% Wilson score interval for a binomial win rate."""
    if total <= 0:
        return None, None
    p = wins / total
    z2 = z * z
    denom = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)


def classify_bucket(metrics: dict[str, Any]) -> dict[str, Any]:
    total = int(metrics.get("trades") or 0)
    wins = int(metrics.get("wins") or 0)
    net = _f(metrics.get("net_pnl"))
    expectancy = _f(metrics.get("expectancy_net"))
    profit_factor = _f(metrics.get("profit_factor"))
    win_rate = (wins / total * 100.0) if total else 0.0
    lower, upper = wilson_interval(wins, total)
    lower_pct = (lower * 100.0) if lower is not None else None
    upper_pct = (upper * 100.0) if upper is not None else None

    if total < MIN_BUCKET_SAMPLE:
        state = "INSUFFICIENT_SAMPLE"
        risk_multiplier = 0.35
        reason = "Todavía hay pocos trades; solo exploración PAPER con riesgo reducido."
    elif total >= PROMISING_SAMPLE and (expectancy < -0.05 or profit_factor < 0.80 or net < -3.0):
        state = "PAUSE"
        risk_multiplier = 0.0
        reason = "La combinación está destruyendo expectativa neta; debe pausarse en PAPER y revisarse."
    elif (
        total >= EIGHTY_TARGET_SAMPLE
        and win_rate >= 80.0
        and (lower_pct or 0.0) >= 65.0
        and expectancy > 0.0
        and profit_factor >= 1.25
    ):
        state = "EIGHTY_TARGET_RESEARCH"
        risk_multiplier = 1.0
        reason = "Alcanzó el objetivo de investigación 80/20 con muestra y expectativa positiva; aún no implica probabilidad futura garantizada."
    elif (
        total >= PROVEN_SAMPLE
        and win_rate >= 60.0
        and (lower_pct or 0.0) >= 45.0
        and expectancy > 0.0
        and profit_factor >= 1.20
    ):
        state = "PROVEN_PAPER"
        risk_multiplier = 1.0
        reason = "Bucket consistente en PAPER: expectativa positiva, profit factor y muestra suficientes."
    elif total >= PROMISING_SAMPLE and expectancy > 0.0 and profit_factor >= 1.05:
        state = "PROMISING"
        risk_multiplier = 0.65
        reason = "Tiene ventaja neta preliminar, pero todavía requiere más operaciones."
    else:
        state = "EXPLORATION"
        risk_multiplier = 0.35
        reason = "Sin evidencia suficiente para aumentar riesgo; continuar aprendizaje con tamaño pequeño."

    return {
        "state": state,
        "risk_multiplier": risk_multiplier,
        "win_rate_pct": round(win_rate, 2),
        "win_rate_wilson_low_pct": round(lower_pct, 2) if lower_pct is not None else None,
        "win_rate_wilson_high_pct": round(upper_pct, 2) if upper_pct is not None else None,
        "reason": reason,
        "is_probability_forecast": False,
    }


def _decorate(raw: dict[str, Any]) -> dict[str, Any]:
    trades = int(raw.get("trades") or 0)
    wins = int(raw.get("wins") or 0)
    losses = int(raw.get("losses") or 0)
    net_pnl = _f(raw.get("net_pnl"))
    positive_net = _f(raw.get("positive_net"))
    negative_net = abs(_f(raw.get("negative_net")))
    costs = _f(raw.get("costs"))
    avg_win = _f(raw.get("avg_win"))
    avg_loss = abs(_f(raw.get("avg_loss")))
    expectancy = net_pnl / trades if trades else 0.0
    profit_factor = positive_net / negative_net if negative_net > 0 else (999.0 if positive_net > 0 else 0.0)
    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else None
    breakeven_win_rate = (avg_loss / (avg_win + avg_loss) * 100.0) if avg_win > 0 and avg_loss > 0 else None
    cost_per_trade = costs / trades if trades else 0.0

    metrics = {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "net_pnl": round(net_pnl, 6),
        "expectancy_net": round(expectancy, 6),
        "profit_factor": round(profit_factor, 4),
        "avg_win": round(avg_win, 6),
        "avg_loss": round(avg_loss, 6),
        "payoff_ratio": round(payoff_ratio, 4) if payoff_ratio is not None else None,
        "breakeven_win_rate_pct": round(breakeven_win_rate, 2) if breakeven_win_rate is not None else None,
        "costs": round(costs, 6),
        "cost_per_trade": round(cost_per_trade, 6),
    }
    return {**metrics, **classify_bucket(metrics)}


async def edge_lab_report(db: AsyncSession, *, days: int = 30) -> dict[str, Any]:
    await ensure_paper_schema(db)
    days = max(1, min(365, int(days)))

    rows = (await db.execute(text("""
        SELECT
            COALESCE(NULLIF(metadata->>'strategy_mode',''), 'TREND_PREMOVE') AS strategy_mode,
            COALESCE(
                NULLIF(metadata->>'micro_setup_type',''),
                NULLIF(metadata->>'setup_type',''),
                NULLIF(grade,''),
                'GENERAL'
            ) AS setup_type,
            side,
            COUNT(*) AS trades,
            COUNT(*) FILTER (WHERE net_pnl > 0) AS wins,
            COUNT(*) FILTER (WHERE net_pnl <= 0) AS losses,
            COALESCE(SUM(net_pnl),0) AS net_pnl,
            COALESCE(SUM(net_pnl) FILTER (WHERE net_pnl > 0),0) AS positive_net,
            COALESCE(SUM(net_pnl) FILTER (WHERE net_pnl < 0),0) AS negative_net,
            COALESCE(AVG(net_pnl) FILTER (WHERE net_pnl > 0),0) AS avg_win,
            COALESCE(AVG(net_pnl) FILTER (WHERE net_pnl < 0),0) AS avg_loss,
            COALESCE(SUM(COALESCE(fees,0)+COALESCE(slippage,0)+COALESCE(funding_estimate,0)),0) AS costs
        FROM paper_positions
        WHERE status='CLOSED'
          AND closed_at >= NOW() - (:days * INTERVAL '1 day')
        GROUP BY 1,2,3
        ORDER BY COUNT(*) DESC, COALESCE(SUM(net_pnl),0) DESC
    """), {"days": days})).mappings().all()

    buckets: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(row)
        metrics = _decorate(raw)
        buckets.append({
            "strategy_mode": raw["strategy_mode"],
            "setup_type": raw["setup_type"],
            "side": raw["side"],
            **metrics,
        })

    overall_raw = dict((await db.execute(text("""
        SELECT
            COUNT(*) AS trades,
            COUNT(*) FILTER (WHERE net_pnl > 0) AS wins,
            COUNT(*) FILTER (WHERE net_pnl <= 0) AS losses,
            COALESCE(SUM(net_pnl),0) AS net_pnl,
            COALESCE(SUM(net_pnl) FILTER (WHERE net_pnl > 0),0) AS positive_net,
            COALESCE(SUM(net_pnl) FILTER (WHERE net_pnl < 0),0) AS negative_net,
            COALESCE(AVG(net_pnl) FILTER (WHERE net_pnl > 0),0) AS avg_win,
            COALESCE(AVG(net_pnl) FILTER (WHERE net_pnl < 0),0) AS avg_loss,
            COALESCE(SUM(COALESCE(fees,0)+COALESCE(slippage,0)+COALESCE(funding_estimate,0)),0) AS costs
        FROM paper_positions
        WHERE status='CLOSED'
          AND closed_at >= NOW() - (:days * INTERVAL '1 day')
    """), {"days": days})).mappings().one())

    overall = _decorate(overall_raw)
    ranked = sorted(
        buckets,
        key=lambda b: (
            b["state"] in {"EIGHTY_TARGET_RESEARCH", "PROVEN_PAPER", "PROMISING"},
            b["expectancy_net"],
            b["trades"],
        ),
        reverse=True,
    )
    pause = [b for b in buckets if b["state"] == "PAUSE"]
    research_80 = [b for b in buckets if b["state"] == "EIGHTY_TARGET_RESEARCH"]

    return {
        "version": EDGE_LAB_VERSION,
        "paper_only": True,
        "window_days": days,
        "goal": "maximize net expectancy after costs; 80/20 is a research target, not a guaranteed forecast",
        "overall": overall,
        "buckets": ranked,
        "best_buckets": ranked[:8],
        "pause_candidates": pause,
        "eighty_target_buckets": research_80,
        "policy": {
            "insufficient_sample_risk_multiplier": 0.35,
            "promising_risk_multiplier": 0.65,
            "proven_paper_risk_multiplier": 1.0,
            "pause_risk_multiplier": 0.0,
            "min_bucket_sample": MIN_BUCKET_SAMPLE,
            "proven_sample": PROVEN_SAMPLE,
            "eighty_target_sample": EIGHTY_TARGET_SAMPLE,
        },
        "safety": {
            "does_not_place_real_orders": True,
            "does_not_claim_future_probability": True,
            "does_not_auto_promote_to_real_money": True,
        },
    }
