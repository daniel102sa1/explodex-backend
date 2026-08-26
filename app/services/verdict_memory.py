from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import sqrt
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = wins / total
    z2 = z * z
    center = p + z2 / (2 * total)
    margin = z * sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    denominator = 1 + z2 / total
    return max(0.0, (center - margin) / denominator) * 100.0


def _interval_for_age(age: timedelta) -> str:
    if age <= timedelta(hours=24):
        return "5m"
    if age <= timedelta(hours=72):
        return "15m"
    return "1h"


def _normalize_group(row: dict[str, Any]) -> dict[str, Any]:
    decided = int(row.get("decided") or 0)
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    return {
        **row,
        "decided": decided,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (wins / decided * 100.0) if decided else None,
        "wilson_low_pct": _wilson_lower_bound(wins, decided),
        "sample_status": "CALIBRATING" if decided < 30 else "USABLE",
        "can_influence_veto": decided >= 30,
    }


def _reason_bundle(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _learning_metadata(reason: Any) -> dict[str, Any]:
    bundle = _reason_bundle(reason)
    prediction = bundle.get("prediction") if isinstance(bundle.get("prediction"), dict) else {}
    context = prediction.get("context_engine") if isinstance(prediction.get("context_engine"), dict) else {}
    regime = context.get("regime") if isinstance(context.get("regime"), dict) else {}
    micro = context.get("microstructure") if isinstance(context.get("microstructure"), dict) else {}
    sequence = prediction.get("sequence") if isinstance(prediction.get("sequence"), dict) else {}
    decision_guard = prediction.get("decision_guard") if isinstance(prediction.get("decision_guard"), dict) else {}

    return {
        "prediction_type": prediction.get("type"),
        "prediction_phase": prediction.get("phase"),
        "preactivation_score": prediction.get("preactivation_score"),
        "market_regime": regime.get("regime") or sequence.get("market_regime"),
        "regime_bias": regime.get("directional_bias") or sequence.get("regime_directional_bias"),
        "regime_confidence": regime.get("confidence") or sequence.get("regime_confidence"),
        "early_context_score": context.get("early_context_score") or sequence.get("early_context_score"),
        "microstructure_score": micro.get("score") or sequence.get("microstructure_score"),
        "microstructure_inputs": micro.get("available_inputs") or sequence.get("microstructure_inputs"),
        "context_guard_pass": context.get("context_guard_pass") if "context_guard_pass" in context else sequence.get("context_guard_pass"),
        "absorption_proxy": micro.get("absorption_proxy"),
        "risk_guard_pass": decision_guard.get("risk_guard_pass", sequence.get("risk_guard_pass")),
        "direction_stability": decision_guard.get("direction_stability", sequence.get("direction_stability")),
        "reward_risk_tp1": decision_guard.get("reward_risk_tp1", sequence.get("reward_risk_tp1")),
        "source": "server_signal_reason",
    }


async def capture_enter_verdicts(db: AsyncSession, limit: int = 200) -> dict[str, int]:
    """Persist READY decisions with the exact context known at decision time."""
    result = await db.execute(
        text(
            """
            SELECT s.id::text AS signal_id, s.symbol_id::text AS symbol_id, sy.symbol,
                   s.created_at, s.direction, s.setup_score, s.risk_score,
                   s.current_price, s.entry_low, s.entry_high, s.stop_loss,
                   s.tp1, s.tp2, s.tp3, s.reason
            FROM signals s
            JOIN symbols sy ON sy.id = s.symbol_id
            LEFT JOIN verdict_memory vm ON vm.signal_id = s.id
            WHERE s.state = 'READY' AND vm.signal_id IS NULL
            ORDER BY s.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows = result.mappings().all()
    inserted = 0
    for source_row in rows:
        row = dict(source_row)
        row["metadata"] = json.dumps(_learning_metadata(row.get("reason")), separators=(",", ":"))
        await db.execute(
            text(
                """
                INSERT INTO verdict_memory (
                    signal_id, symbol_id, symbol, observed_at, direction,
                    setup_score, risk_score, entry_price, entry_low, entry_high,
                    stop_loss, tp1, tp2, tp3, reason, metadata, outcome
                ) VALUES (
                    CAST(:signal_id AS uuid), CAST(:symbol_id AS uuid), :symbol, :observed_at, :direction,
                    :setup_score, :risk_score, :entry_price, :entry_low, :entry_high,
                    :stop_loss, :tp1, :tp2, :tp3, :reason, CAST(:metadata AS JSONB), 'UNRESOLVED'
                )
                ON CONFLICT (signal_id) DO NOTHING
                """
            ),
            row,
        )
        inserted += 1
    await db.commit()
    return {"seen": len(rows), "inserted": inserted}


async def resolve_verdict_outcomes(db: AsyncSession, limit: int = 60) -> dict[str, int]:
    result = await db.execute(
        text(
            """
            SELECT id::text, symbol, observed_at, direction, entry_price, stop_loss, tp1
            FROM verdict_memory
            WHERE outcome = 'UNRESOLVED'
              AND observed_at <= NOW() - INTERVAL '5 minutes'
              AND observed_at >= NOW() - INTERVAL '7 days'
            ORDER BY observed_at ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows = [dict(r) for r in result.mappings().all()]
    if not rows:
        return {"checked": 0, "resolved": 0, "ambiguous": 0}

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["symbol"]].append(row)

    resolved = 0
    ambiguous = 0
    now = datetime.now(timezone.utc)

    for symbol, group in grouped.items():
        oldest = min(r["observed_at"] for r in group)
        interval = _interval_for_age(now - oldest)
        candles = await binance_client.klines(symbol, interval=interval, limit=300)
        parsed = []
        for c in candles:
            if len(c) < 5:
                continue
            parsed.append({"time": int(c[0]), "high": _f(c[2]), "low": _f(c[3])})

        for row in group:
            direction = row["direction"]
            start_ms = _ms(row["observed_at"])
            stop = _f(row["stop_loss"])
            tp1 = _f(row["tp1"])
            entry = _f(row["entry_price"])
            if not entry or not stop or not tp1:
                continue

            best = entry
            worst = entry
            outcome = None
            outcome_at = None
            mfe_pct = 0.0
            mae_pct = 0.0

            for c in parsed:
                if c["time"] < start_ms:
                    continue
                high, low = c["high"], c["low"]
                hit_tp = high >= tp1 if direction == "LONG" else low <= tp1
                hit_stop = low <= stop if direction == "LONG" else high >= stop

                if direction == "LONG":
                    best = max(best, high)
                    worst = min(worst, low)
                    mfe_pct = (best - entry) / entry * 100
                    mae_pct = (entry - worst) / entry * 100
                else:
                    best = min(best, low)
                    worst = max(worst, high)
                    mfe_pct = (entry - best) / entry * 100
                    mae_pct = (worst - entry) / entry * 100

                if hit_tp and hit_stop:
                    outcome = "AMBIGUOUS"
                    ambiguous += 1
                elif hit_tp:
                    outcome = "TP1_FIRST"
                elif hit_stop:
                    outcome = "STOP_FIRST"
                if outcome:
                    outcome_at = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
                    break

            if not outcome:
                continue

            minutes = max(0.0, (outcome_at - row["observed_at"]).total_seconds() / 60) if outcome_at else None
            await db.execute(
                text(
                    """
                    UPDATE verdict_memory
                    SET outcome = :outcome,
                        outcome_at = :outcome_at,
                        minutes_to_outcome = :minutes,
                        mfe_pct = :mfe_pct,
                        mae_pct = :mae_pct,
                        evaluated_at = NOW()
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {
                    "id": row["id"],
                    "outcome": outcome,
                    "outcome_at": outcome_at,
                    "minutes": minutes,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                },
            )
            resolved += 1

    await db.commit()
    return {"checked": len(rows), "resolved": resolved, "ambiguous": ambiguous}


async def _json_cohort(db: AsyncSession, expression: str, alias: str, *, limit: int = 40) -> list[dict[str, Any]]:
    result = await db.execute(
        text(
            f"""
            SELECT {expression} AS {alias},
                   COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS decided,
                   COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                   COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                   COUNT(*) FILTER (WHERE outcome = 'UNRESOLVED') AS unresolved,
                   AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                   AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct,
                   AVG(minutes_to_outcome) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_minutes
            FROM verdict_memory
            WHERE {expression} IS NOT NULL
            GROUP BY {alias}
            ORDER BY decided DESC NULLS LAST, {alias}
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    output: list[dict[str, Any]] = []
    for source in result.mappings().all():
        item = _normalize_group(dict(source))
        item["unresolved"] = int(item.get("unresolved") or 0)
        output.append(item)
    return output


async def verdict_memory_stats(db: AsyncSession) -> dict[str, Any]:
    global_result = await db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS decided,
                COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                COUNT(*) FILTER (WHERE outcome = 'AMBIGUOUS') AS ambiguous,
                COUNT(*) FILTER (WHERE outcome = 'UNRESOLVED') AS unresolved,
                AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct,
                AVG(minutes_to_outcome) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_minutes,
                MAX(evaluated_at) AS last_evaluated_at,
                MAX(observed_at) AS last_observed_at
            FROM verdict_memory
            """
        )
    )
    row = _normalize_group(dict(global_result.mappings().one()))
    row["ambiguous"] = int(row.get("ambiguous") or 0)
    row["unresolved"] = int(row.get("unresolved") or 0)

    direction_result = await db.execute(
        text(
            """
            SELECT direction,
                   COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS decided,
                   COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                   COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                   AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                   AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct,
                   AVG(minutes_to_outcome) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_minutes
            FROM verdict_memory
            GROUP BY direction
            ORDER BY direction
            """
        )
    )
    row["by_direction"] = [_normalize_group(dict(r)) for r in direction_result.mappings().all()]

    score_result = await db.execute(
        text(
            """
            SELECT CASE
                     WHEN setup_score >= 90 THEN '90-100'
                     WHEN setup_score >= 85 THEN '85-89'
                     WHEN setup_score >= 80 THEN '80-84'
                     ELSE '<80'
                   END AS score_bucket,
                   COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS decided,
                   COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                   COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                   AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                   AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct
            FROM verdict_memory
            GROUP BY score_bucket
            ORDER BY score_bucket DESC
            """
        )
    )
    row["by_score"] = [_normalize_group(dict(r)) for r in score_result.mappings().all()]

    symbol_result = await db.execute(
        text(
            """
            SELECT symbol,
                   COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS decided,
                   COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                   COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                   COUNT(*) FILTER (WHERE outcome = 'UNRESOLVED') AS unresolved,
                   AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                   AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct,
                   AVG(minutes_to_outcome) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_minutes
            FROM verdict_memory
            GROUP BY symbol
            HAVING COUNT(*) > 0
            ORDER BY decided DESC, symbol ASC
            LIMIT 80
            """
        )
    )
    by_symbol = []
    for source in symbol_result.mappings().all():
        item = _normalize_group(dict(source))
        item["unresolved"] = int(item.get("unresolved") or 0)
        by_symbol.append(item)
    row["by_symbol"] = by_symbol

    row["by_regime"] = await _json_cohort(db, "metadata->>'market_regime'", "market_regime")
    row["by_prediction_type"] = await _json_cohort(db, "metadata->>'prediction_type'", "prediction_type")
    row["by_absorption"] = await _json_cohort(db, "metadata->>'absorption_proxy'", "absorption_proxy")

    context_result = await db.execute(
        text(
            """
            SELECT CASE
                     WHEN NULLIF(metadata->>'early_context_score','')::numeric >= 80 THEN '80-100'
                     WHEN NULLIF(metadata->>'early_context_score','')::numeric >= 65 THEN '65-79'
                     WHEN NULLIF(metadata->>'early_context_score','')::numeric >= 50 THEN '50-64'
                     ELSE '<50_OR_ND'
                   END AS context_bucket,
                   COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS decided,
                   COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                   COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                   COUNT(*) FILTER (WHERE outcome = 'UNRESOLVED') AS unresolved,
                   AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                   AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct,
                   AVG(minutes_to_outcome) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_minutes
            FROM verdict_memory
            GROUP BY context_bucket
            ORDER BY context_bucket DESC
            """
        )
    )
    row["by_context_score"] = []
    for source in context_result.mappings().all():
        item = _normalize_group(dict(source))
        item["unresolved"] = int(item.get("unresolved") or 0)
        row["by_context_score"].append(item)

    micro_result = await db.execute(
        text(
            """
            SELECT CASE
                     WHEN NULLIF(metadata->>'microstructure_score','')::numeric >= 70 THEN '70-100'
                     WHEN NULLIF(metadata->>'microstructure_score','')::numeric >= 55 THEN '55-69'
                     WHEN NULLIF(metadata->>'microstructure_score','')::numeric >= 40 THEN '40-54'
                     ELSE '<40_OR_ND'
                   END AS micro_bucket,
                   COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS decided,
                   COUNT(*) FILTER (WHERE outcome = 'TP1_FIRST') AS wins,
                   COUNT(*) FILTER (WHERE outcome = 'STOP_FIRST') AS losses,
                   COUNT(*) FILTER (WHERE outcome = 'UNRESOLVED') AS unresolved,
                   AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
                   AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct
            FROM verdict_memory
            GROUP BY micro_bucket
            ORDER BY micro_bucket DESC
            """
        )
    )
    row["by_microstructure_score"] = []
    for source in micro_result.mappings().all():
        item = _normalize_group(dict(source))
        item["unresolved"] = int(item.get("unresolved") or 0)
        row["by_microstructure_score"].append(item)

    row["context_learning_note"] = "Context/regime cohorts stay CALIBRATING below 30 decided cases and cannot create entries or raise leverage."
    return row
