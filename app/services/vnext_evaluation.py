from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EVALUATION_GENERATION = "EXPLODEX_VNEXT_2026_09_20"
VERSION = "explodex_vnext_evaluation_v2_normalized_risk"
SHADOW_HORIZONS = ("15m", "1h", "4h", "6h", "24h", "3d", "7d")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _normalized_risk_report(
    rows: list[dict[str, Any]],
    *,
    starting_balance: float,
) -> dict[str, Any]:
    scenarios = {
        "risk_0_25_pct": 0.0025,
        "risk_0_50_pct": 0.0050,
        "risk_1_00_pct": 0.0100,
    }
    totals = {key: 0.0 for key in scenarios}
    usable = 0
    details: list[dict[str, Any]] = []
    for row in rows:
        actual_risk = _f(row.get("risk_usdt"))
        net_pnl = _f(row.get("net_pnl"))
        if actual_risk <= 1e-12:
            continue
        usable += 1
        item = {
            "id": row.get("id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "actual_risk_usdt": round(actual_risk, 6),
            "actual_net_pnl": round(net_pnl, 6),
            "realized_r_multiple_net": round(net_pnl / actual_risk, 4),
            "scenarios": {},
        }
        for key, pct in scenarios.items():
            target_risk = max(0.0, starting_balance * pct)
            scale = target_risk / actual_risk
            normalized = net_pnl * scale
            totals[key] += normalized
            item["scenarios"][key] = {
                "target_risk_usdt": round(target_risk, 6),
                "normalized_net_pnl": round(normalized, 6),
                "scale_vs_actual": round(scale, 4),
            }
        details.append(item)
    return {
        "method": "LINEAR_RESCALE_FROM_ACTUAL_NET_PNL_BY_STOP_RISK",
        "paper_only": True,
        "starting_balance": round(starting_balance, 6),
        "usable_closed_trades": usable,
        "scenarios": {
            key: {
                "risk_pct_of_starting_balance": pct * 100.0,
                "aggregate_normalized_net_pnl": round(totals[key], 6),
            }
            for key, pct in scenarios.items()
        },
        "recent_trades": details[-20:],
        "note": "Counterfactual evaluation only. It rescales actual PAPER net PnL by stop-risk budget; it does not change execution or assume extra directional edge.",
    }


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
            COUNT(*) FILTER (WHERE exit_reason='TIME_EXIT') AS time_exits,
            COUNT(*) FILTER (WHERE COALESCE(metadata->>'validation_probation','false')='true') AS probation_trades
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
        "probation_trades": int(paper.get("probation_trades") or 0),
        "status": "USABLE" if closed >= 30 else "CALIBRATING",
        "minimum_comparable_closed_trades": 30,
    }

    account = dict((await db.execute(text("""
        SELECT starting_balance FROM paper_accounts WHERE id=1
    """))).mappings().first() or {})
    starting_balance = _f(account.get("starting_balance"), 1000.0)
    closed_rows = [
        dict(row)
        for row in (await db.execute(text("""
            SELECT id, symbol, side, risk_usdt, net_pnl, closed_at
            FROM paper_positions
            WHERE status='CLOSED'
              AND metadata->>'evaluation_generation'=:generation
            ORDER BY closed_at ASC
        """), {"generation": EVALUATION_GENERATION})).mappings().all()
    ]
    paper_report["normalized_risk"] = _normalized_risk_report(
        closed_rows,
        starting_balance=starting_balance,
    )

    shadow_table_exists = bool((await db.execute(text("SELECT to_regclass('public.heart_shadow_forecasts')"))).scalar_one())
    shadow_total = 0
    horizons: dict[str, Any] = {}
    if shadow_table_exists:
        shadow_total = int((await db.execute(text("""
            SELECT COUNT(*)
            FROM heart_shadow_forecasts
            WHERE metadata->>'evaluation_generation'=:generation
        """), {"generation": EVALUATION_GENERATION})).scalar_one() or 0)

    for horizon in SHADOW_HORIZONS:
        if not shadow_table_exists:
            horizons[horizon] = {
                "sample": 0, "correct": 0, "accuracy_pct": None,
                "avg_directional_return_pct": None, "avg_mfe_pct": None,
                "avg_mae_pct": None, "status": "CALIBRATING",
            }
            continue
        row = dict((await db.execute(text("""
            SELECT
                COUNT(*) FILTER (
                    WHERE COALESCE((outcomes -> :h ->> 'mature')::boolean,FALSE)
                ) AS sample,
                COUNT(*) FILTER (
                    WHERE COALESCE((outcomes -> :h ->> 'mature')::boolean,FALSE)
                      AND COALESCE((outcomes -> :h ->> 'correct')::boolean,FALSE)
                ) AS correct,
                AVG(NULLIF(outcomes -> :h ->> 'directional_return_pct','')::double precision)
                  FILTER (WHERE COALESCE((outcomes -> :h ->> 'mature')::boolean,FALSE)) AS avg_directional_return_pct,
                AVG(NULLIF(outcomes -> :h ->> 'mfe_pct','')::double precision)
                  FILTER (WHERE COALESCE((outcomes -> :h ->> 'mature')::boolean,FALSE)) AS avg_mfe_pct,
                AVG(NULLIF(outcomes -> :h ->> 'mae_pct','')::double precision)
                  FILTER (WHERE COALESCE((outcomes -> :h ->> 'mature')::boolean,FALSE)) AS avg_mae_pct
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
            "continues_while_main_quant_guard_is_halted": True,
        },
        "evaluation_rules": {
            "do_not_compare_new_logic_to_all_legacy_trades_as_one_sample": True,
            "minimum_comparable_sample": 30,
            "pre_tp1_protection_is_experimental": True,
            "shadow_scores_are_not_next_trade_probabilities": True,
            "main_quant_guard_is_not_reset_or_falsified": True,
            "separate_probation_lane_may_collect_tiny_paper_outcomes": True,
        },
        "note": "VNext is evaluated as its own cohort. Shadow forecasts always learn; a separate tiny-risk probation lane may collect actual PAPER outcomes while the main legacy quant guard remains HALT.",
    }
