from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EVALUATION_GENERATION = "EXPLODEX_VNEXT_2026_09_20"
VERSION = "explodex_vnext_evaluation_v1"
SHADOW_HORIZONS = ("15m", "1h", "4h", "6h", "24h")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def vnext_evaluation_report(db: AsyncSession) -> dict[str, Any]:
    paper = dict((await db.execute(text("""
        SELECT
            COUNT(*) AS trades,
            COUNT(*) FILTER (WHERE status='OPEN') AS open_trades,
            COUNT(*) FILTER (WHERE status='CLOSED') AS closed_trades,
            COUNT(*) FILTER (WHERE status='CLOSED' AND net_pnl > 0) AS winners,
            COUNT(*) FILTER (WHERE status='CLOSED' AND net_pnl <= 0) AS losers,
            COALESCE(SUM(net_pnl) FILTER (WHERE status='CLOSED'),0) AS net_pnl,
            COALESCE(SUM(net_pnl) FILTER (WHERE status='CLOSED' AND net_pnl > 0),0) AS gross_wins,
            COALESCE(ABS(SUM(net_pnl) FILTER (WHERE status='CLOSED' AND net_pnl < 0)),0) AS gross_losses,
            COUNT(*) FILTER (WHERE exit_reason='PRE_TP1_PROTECT_STOP') AS pre_tp1_protect_stops,
            COUNT(*) FILTER (WHERE exit_reason IN ('PROFIT_LOCK_STOP','AMBIGUOUS_PROFIT_LOCK_STOP')) AS post_tp_profit_lock_stops,
            COUNT(*) FILTER (WHERE exit_reason='TP1') AS tp1_exits,
            COUNT(*) FILTER (WHERE exit_reason='TIME_EXIT') AS time_exits
        FROM paper_positions
        WHERE metadata->>'evaluation_generation'=:generation
    """), {"generation": EVALUATION_GENERATION})).mappings().one())

    closed = int(paper.get("closed_trades") or 0)
    winners = int(paper.get("winners") or 0)
    gross_wins = _f(paper.get("gross_wins"))
    gross_losses = _f(paper.get("gross_losses"))
    paper_report = {
        "trades": int(paper.get("trades") or 0),
        "open_trades": int(paper.get("open_trades") or 0),
        "closed_trades": closed,
        "winners": winners,
        "losers": int(paper.get("losers") or 0),
        "win_rate_pct": round(winners / closed * 100.0, 2) if closed else None,
        "net_pnl": round(_f(paper.get("net_pnl")), 6),
        "expectancy_net": round(_f(paper.get("net_pnl")) / closed, 6) if closed else None,
        "profit_factor": round(gross_wins / gross_losses, 4) if gross_losses > 1e-12 else (999.0 if gross_wins > 0 else None),
        "pre_tp1_protect_stops": int(paper.get("pre_tp1_protect_stops") or 0),
        "post_tp_profit_lock_stops": int(paper.get("post_tp_profit_lock_stops") or 0),
        "tp1_exits": int(paper.get("tp1_exits") or 0),
        "time_exits": int(paper.get("time_exits") or 0),
        "status": "USABLE" if closed >= 30 else "CALIBRATING",
        "minimum_comparable_closed_trades": 30,
    }

    shadow_total = int((await db.execute(text("""
        SELECT COUNT(*)
        FROM heart_shadow_forecasts
        WHERE metadata->>'evaluation_generation'=:generation
    """), {"generation": EVALUATION_GENERATION})).scalar_one() or 0)

    horizons: dict[str, Any] = {}
    for horizon in SHADOW_HORIZONS:
        row = dict((await db.execute(text("""
            SELECT
                COUNT(*) FILTER (
                    WHERE COALESCE((outcomes #>> ARRAY[:h,'mature'])::boolean,FALSE)
                ) AS sample,
                COUNT(*) FILTER (
                    WHERE COALESCE((outcomes #>> ARRAY[:h,'mature'])::boolean,FALSE)
                      AND COALESCE((outcomes #>> ARRAY[:h,'correct'])::boolean,FALSE)
                ) AS correct,
                AVG(NULLIF(outcomes #>> ARRAY[:h,'directional_return_pct'],'')::double precision)
                  FILTER (WHERE COALESCE((outcomes #>> ARRAY[:h,'mature'])::boolean,FALSE)) AS avg_directional_return_pct,
                AVG(NULLIF(outcomes #>> ARRAY[:h,'mfe_pct'],'')::double precision)
                  FILTER (WHERE COALESCE((outcomes #>> ARRAY[:h,'mature'])::boolean,FALSE)) AS avg_mfe_pct,
                AVG(NULLIF(outcomes #>> ARRAY[:h,'mae_pct'],'')::double precision)
                  FILTER (WHERE COALESCE((outcomes #>> ARRAY[:h,'mature'])::boolean,FALSE)) AS avg_mae_pct
            FROM heart_shadow_forecasts
            WHERE metadata->>'evaluation_generation'=:generation
        """), {"generation": EVALUATION_GENERATION, "h": horizon})).mappings().one())
        sample = int(row.get("sample") or 0)
        correct = int(row.get("correct") or 0)
        horizons[horizon] = {
            "sample": sample,
            "correct": correct,
            "accuracy_pct": round(correct / sample * 100.0, 2) if sample else None,
            "avg_directional_return_pct": round(_f(row.get("avg_directional_return_pct")), 4) if row.get("avg_directional_return_pct") is not None else None,
            "avg_mfe_pct": round(_f(row.get("avg_mfe_pct")), 4) if row.get("avg_mfe_pct") is not None else None,
            "avg_mae_pct": round(_f(row.get("avg_mae_pct")), 4) if row.get("avg_mae_pct") is not None else None,
            "status": "USABLE" if sample >= 30 else "CALIBRATING",
        }

    return {
        "version": VERSION,
        "generation": EVALUATION_GENERATION,
        "paper_only": True,
        "paper": paper_report,
        "shadow": {
            "captured_signals": shadow_total,
            "horizons": horizons,
            "continues_while_paper_kill_switch_is_active": True,
        },
        "evaluation_rules": {
            "do_not_compare_new_logic_to_all_legacy_trades_as_one_sample": True,
            "minimum_comparable_sample": 30,
            "pre_tp1_protection_is_experimental": True,
            "shadow_scores_are_not_next_trade_probabilities": True,
            "paper_kill_switch_is_not_bypassed": True,
        },
        "note": "VNext is evaluated as its own cohort. Shadow forecasts keep learning even when the visible PAPER risk guard halts new positions.",
    }
