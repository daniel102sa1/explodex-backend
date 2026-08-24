from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.market_context import market_context
from app.services.news_context import news_context_for_symbol


def classify_signal(setup_score: float, risk_score: float) -> dict[str, Any]:
    """Classify signal quality without pretending the score is a true probability."""
    if setup_score >= 95 and risk_score <= 20:
        return {"tier": "ELITE", "label": "A+", "action": "HIGH_PRIORITY", "display": "95-100/100 score"}
    if setup_score >= 90 and risk_score <= 30:
        return {"tier": "VERY_STRONG", "label": "A", "action": "HIGH_PRIORITY", "display": "90-94/100 score"}
    if setup_score >= 80 and risk_score <= 35:
        return {"tier": "STRONG", "label": "B+", "action": "TRADE_CANDIDATE", "display": "80-89/100 score"}
    if setup_score >= 70 and risk_score <= 50:
        return {"tier": "WATCH", "label": "B", "action": "WAIT_CONFIRMATION", "display": "70-79/100 score"}
    return {"tier": "NO_TRADE", "label": "C", "action": "DO_NOT_TRADE", "display": "below trade threshold"}


def _bucket(score: float) -> str:
    if score >= 95:
        return "95-100"
    if score >= 90:
        return "90-94"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    return "0-69"


async def calibration_by_score(db: AsyncSession) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT s.setup_score, s.direction, t.pnl_usdt, t.r_multiple
            FROM trades t
            JOIN signals s ON s.id = t.signal_id
            WHERE t.mode = 'PAPER'
              AND t.status IN ('CLOSED','STOPPED')
              AND t.pnl_usdt IS NOT NULL
            """
        )
    )
    rows = [dict(row) for row in result.mappings().all()]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_bucket(float(row["setup_score"]))].append(row)

    output: list[dict[str, Any]] = []
    for name in ["95-100", "90-94", "80-89", "70-79", "0-69"]:
        items = grouped.get(name, [])
        n = len(items)
        wins = sum(1 for x in items if float(x["pnl_usdt"] or 0) > 0)
        pnl = sum(float(x["pnl_usdt"] or 0) for x in items)
        r_values = [float(x["r_multiple"]) for x in items if x["r_multiple"] is not None]
        win_rate = (wins / n) * 100 if n else None

        if n >= 100:
            status = "CALIBRATED"
        elif n >= 30:
            status = "PROVISIONAL"
        else:
            status = "INSUFFICIENT_SAMPLE"

        output.append({
            "score_bucket": name,
            "closed_trades": n,
            "wins": wins,
            "observed_win_rate_pct": round(win_rate, 2) if win_rate is not None else None,
            "net_pnl_usdt": round(pnl, 4),
            "average_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
            "calibration_status": status,
            "can_show_as_probability_estimate": n >= 30,
        })

    return {
        "important": "Observed win rate is historical calibration, not certainty for the next trade.",
        "total_closed_paper_trades": len(rows),
        "buckets": output,
    }


async def ranked_opportunities(db: AsyncSession, limit: int = 50) -> dict[str, Any]:
    calibration, context = await asyncio.gather(
        calibration_by_score(db),
        market_context(),
    )
    calibration_map = {item["score_bucket"]: item for item in calibration["buckets"]}

    result = await db.execute(
        text(
            """
            SELECT s.id::text, sy.symbol, s.created_at, s.direction, s.state,
                   s.setup_score, s.risk_score, s.current_price,
                   s.entry_low, s.entry_high, s.stop_loss, s.tp1, s.tp2, s.tp3,
                   s.expected_move_min_pct, s.expected_move_max_pct,
                   s.expected_duration_min_minutes, s.expected_duration_max_minutes,
                   sm.structure_score, sm.oi_score, sm.taker_score, sm.volume_score,
                   sm.funding_score, sm.btc_score, sm.absorption_score,
                   sm.volatility_score, sm.liquidity_score,
                   sm.oi_change_pct, sm.taker_ratio, sm.funding_rate,
                   sm.relative_volume, sm.absorption_detected,
                   sm.btc_filter_passed, sm.notes
            FROM signals s
            JOIN symbols sy ON sy.id = s.symbol_id
            LEFT JOIN LATERAL (
                SELECT * FROM signal_metrics m
                WHERE m.signal_id = s.id
                ORDER BY m.created_at DESC
                LIMIT 1
            ) sm ON TRUE
            WHERE s.is_active = TRUE
            ORDER BY s.setup_score DESC, s.risk_score ASC, s.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )

    rows = [dict(row) for row in result.mappings().all()]

    # Only enrich the best few with news to keep response time/cost under control.
    news_targets = rows[: max(0, settings.news_max_candidates)] if settings.news_enabled else []
    news_results = await asyncio.gather(
        *(news_context_for_symbol(str(item["symbol"])) for item in news_targets),
        return_exceptions=True,
    ) if news_targets else []

    news_map: dict[str, dict[str, Any]] = {}
    for item, news in zip(news_targets, news_results):
        if isinstance(news, Exception):
            news_map[str(item["symbol"])] = {
                "sentiment": "UNAVAILABLE",
                "score_adjustment": 0.0,
                "headlines": [],
            }
        else:
            news_map[str(item["symbol"])] = news

    opportunities: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        base_score = float(item["setup_score"] or 0)
        risk_score = float(item["risk_score"] or 100)
        direction = str(item["direction"])

        market_adjustment = float(
            context["long_score_adjustment"] if direction == "LONG" else context["short_score_adjustment"]
        )

        news = news_map.get(str(item["symbol"]), {
            "sentiment": "NOT_CHECKED",
            "score_adjustment": 0.0,
            "headlines": [],
            "note": "News enrichment is reserved for top-ranked candidates.",
        })
        raw_news_adjustment = float(news.get("score_adjustment", 0.0) or 0.0)
        # Positive news helps LONG and hurts SHORT; negative news does the reverse.
        directional_news_adjustment = raw_news_adjustment if direction == "LONG" else -raw_news_adjustment

        contextual_score = max(0.0, min(100.0, base_score + market_adjustment + directional_news_adjustment))

        # Strong adverse context increases risk rather than only reducing score.
        contextual_risk = risk_score
        if direction == "LONG" and context["regime"] == "RISK_OFF":
            contextual_risk = min(100.0, contextual_risk + 10)
        elif direction == "SHORT" and context["regime"] == "RISK_ON":
            contextual_risk = min(100.0, contextual_risk + 10)

        if directional_news_adjustment <= -3.75:
            contextual_risk = min(100.0, contextual_risk + 8)

        tier = classify_signal(contextual_score, contextual_risk)
        bucket_name = _bucket(base_score)
        historical = calibration_map.get(bucket_name, {})
        sample = int(historical.get("closed_trades", 0) or 0)
        can_estimate = bool(historical.get("can_show_as_probability_estimate", False))

        item.update(tier)
        item["base_setup_score"] = round(base_score, 2)
        item["contextual_score"] = round(contextual_score, 2)
        item["contextual_risk_score"] = round(contextual_risk, 2)
        item["market_adjustment"] = round(market_adjustment, 2)
        item["news_adjustment"] = round(directional_news_adjustment, 2)
        item["market_regime"] = context["regime"]
        item["news"] = news
        item["score_bucket"] = bucket_name
        item["historical_sample_size"] = sample
        item["historical_win_rate_pct"] = historical.get("observed_win_rate_pct") if can_estimate else None
        item["probability_status"] = historical.get("calibration_status", "INSUFFICIENT_SAMPLE")
        item["probability_note"] = (
            "Historical estimate only; not certainty for this trade."
            if can_estimate
            else "Not enough paper trades yet to display a reliable probability."
        )
        opportunities.append(item)

    opportunities.sort(key=lambda x: (x["contextual_score"], -x["contextual_risk_score"]), reverse=True)

    groups = {
        "elite": [x for x in opportunities if x["tier"] == "ELITE"],
        "very_strong": [x for x in opportunities if x["tier"] == "VERY_STRONG"],
        "strong": [x for x in opportunities if x["tier"] == "STRONG"],
        "watch": [x for x in opportunities if x["tier"] == "WATCH"],
        "no_trade": [x for x in opportunities if x["tier"] == "NO_TRADE"],
    }

    return {
        "warning": "There is no 100% guaranteed LONG or SHORT. 100/100 is a setup score, not a 100% probability.",
        "market_context": context,
        "calibration": calibration,
        "best_opportunity": opportunities[0] if opportunities else None,
        "groups": groups,
    }
