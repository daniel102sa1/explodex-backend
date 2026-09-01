from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client

VERSION = "shadow_forecast_memory_v1"
HORIZONS = {"15m": 15, "1h": 60, "4h": 240, "6h": 360, "24h": 1440}
MIN_SAMPLE = 30


def _d(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def ensure_shadow_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS heart_shadow_forecasts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            signal_id UUID NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            entry_price DOUBLE PRECISION NOT NULL,
            primary_direction TEXT,
            primary_action TEXT,
            permitted_paper_lane TEXT,
            pre_event_type TEXT,
            breadth_regime TEXT,
            event_type TEXT,
            forecast JSONB NOT NULL DEFAULT '{}'::jsonb,
            outcomes JSONB NOT NULL DEFAULT '{}'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_evaluated_at TIMESTAMPTZ
        )
    """))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_shadow_symbol_time ON heart_shadow_forecasts(symbol, observed_at DESC)"))
    await db.commit()


async def capture_shadow_forecasts_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    await ensure_shadow_schema(db)
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.created_at, s.current_price,
               s.direction, s.state, s.reason
        FROM signals s JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.created_at ASC
    """), {"run_id": run_id})).mappings().all()
    inserted = 0
    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason")); prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        contract = _d(heart.get("execution_contract"))
        matrix = _d(contract.get("forecast_matrix")) or _d(heart.get("forecast_matrix"))
        horizons = _d(matrix.get("horizons"))
        forecast = {}
        for label in HORIZONS:
            h = _d(horizons.get(label))
            forecast[label] = {
                "direction": h.get("direction"),
                "edge": _f(h.get("edge")),
                "long_score": _f(h.get("long_score")),
                "short_score": _f(h.get("short_score")),
            }
        pre = _d(contract.get("pre_event_prediction")) or _d(heart.get("pre_event_prediction"))
        breadth = _d(contract.get("market_breadth")) or _d(heart.get("market_breadth"))
        event = _d(contract.get("event_risk")) or _d(heart.get("event_risk"))
        metadata = {
            "heart_version": heart.get("version"),
            "matrix_consensus": matrix.get("consensus"),
            "horizon_conflict": matrix.get("horizon_conflict"),
            "pre_event_phase": pre.get("phase"),
            "pre_event_score": pre.get("preparation_score"),
            "breadth_score": breadth.get("breadth_score"),
            "event_severity": event.get("severity"),
            "risk_score": reason.get("risk_score"),
            "captured_even_without_trade": True,
        }
        result = await db.execute(text("""
            INSERT INTO heart_shadow_forecasts (
                signal_id, symbol, observed_at, entry_price, primary_direction,
                primary_action, permitted_paper_lane, pre_event_type, breadth_regime,
                event_type, forecast, outcomes, metadata
            ) VALUES (
                CAST(:signal_id AS UUID), :symbol, :observed_at, :entry_price, :direction,
                :action, :lane, :pre_event, :breadth, :event, CAST(:forecast AS JSONB),
                '{}'::jsonb, CAST(:metadata AS JSONB)
            ) ON CONFLICT (signal_id) DO NOTHING
        """), {
            "signal_id": row["signal_id"], "symbol": row["symbol"], "observed_at": row["created_at"],
            "entry_price": _f(row.get("current_price")),
            "direction": contract.get("primary_direction") or heart.get("direction") or row.get("direction"),
            "action": contract.get("primary_action") or row.get("state"),
            "lane": contract.get("permitted_paper_lane"),
            "pre_event": pre.get("pre_event_type"), "breadth": breadth.get("regime"),
            "event": event.get("event_type"), "forecast": json.dumps(forecast), "metadata": json.dumps(metadata),
        })
        inserted += int(result.rowcount or 0)
    await db.commit()
    return {"version": VERSION, "seen": len(rows), "inserted": inserted, "captures_no_trade": True}


def _interval_for_minutes(minutes: int) -> str:
    if minutes <= 60:
        return "5m"
    if minutes <= 360:
        return "15m"
    return "1h"


def _label(direction: str, entry: float, final_price: float, max_high: float, min_low: float) -> dict[str, Any]:
    if entry <= 0 or final_price <= 0:
        return {"mature": False}
    ret = (final_price - entry) / entry * 100.0
    directional_ret = ret if direction == "LONG" else -ret if direction == "SHORT" else 0.0
    threshold = 0.15
    if direction not in {"LONG", "SHORT"}:
        correct = None
    else:
        correct = directional_ret >= threshold
    favorable = ((max_high - entry) / entry * 100.0) if direction == "LONG" else ((entry - min_low) / entry * 100.0) if direction == "SHORT" else 0.0
    adverse = ((entry - min_low) / entry * 100.0) if direction == "LONG" else ((max_high - entry) / entry * 100.0) if direction == "SHORT" else 0.0
    return {
        "mature": True,
        "correct": correct,
        "return_pct": round(ret, 4),
        "directional_return_pct": round(directional_ret, 4),
        "mfe_pct": round(max(0.0, favorable), 4),
        "mae_pct": round(max(0.0, adverse), 4),
        "threshold_pct": threshold,
    }


async def evaluate_shadow_forecasts(db: AsyncSession, limit: int = 80) -> dict[str, Any]:
    await ensure_shadow_schema(db)
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT id::text, symbol, observed_at, entry_price, forecast, outcomes
        FROM heart_shadow_forecasts
        WHERE observed_at <= NOW() - INTERVAL '15 minutes'
          AND observed_at >= NOW() - INTERVAL '8 days'
        ORDER BY observed_at ASC
        LIMIT :limit
    """), {"limit": limit})).mappings().all()]
    evaluated = 0
    matured = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        age_min = (now - row["observed_at"]).total_seconds() / 60.0
        forecast = _d(row.get("forecast")); outcomes = _d(row.get("outcomes")); changed = False
        due = [label for label, mins in HORIZONS.items() if age_min >= mins and not _d(outcomes.get(label)).get("mature")]
        if not due:
            continue
        max_minutes = max(HORIZONS[label] for label in due)
        interval = _interval_for_minutes(max_minutes)
        try:
            candles = await binance_client.klines(row["symbol"], interval=interval, limit=300)
        except Exception:
            continue
        parsed = []
        for c in candles:
            if len(c) < 5:
                continue
            parsed.append((int(c[0]), _f(c[2]), _f(c[3]), _f(c[4])))
        start_ms = int(row["observed_at"].timestamp() * 1000)
        entry = _f(row.get("entry_price"))
        for label in due:
            end_ms = start_ms + HORIZONS[label] * 60_000
            window = [c for c in parsed if start_ms <= c[0] <= end_ms]
            if not window:
                continue
            final = window[-1][3]; high = max(c[1] for c in window); low = min(c[2] for c in window)
            direction = str(_d(forecast.get(label)).get("direction") or "NEUTRAL").upper()
            outcomes[label] = _label(direction, entry, final, high, low)
            changed = True; matured += 1
        if changed:
            await db.execute(text("UPDATE heart_shadow_forecasts SET outcomes=CAST(:outcomes AS JSONB), last_evaluated_at=NOW() WHERE id=CAST(:id AS UUID)"), {
                "id": row["id"], "outcomes": json.dumps(outcomes)
            })
            evaluated += 1
    await db.commit()
    return {"version": VERSION, "rows_checked": len(rows), "rows_updated": evaluated, "horizons_matured": matured}


async def shadow_calibration_report(db: AsyncSession, horizon: str = "1h") -> dict[str, Any]:
    await ensure_shadow_schema(db)
    if horizon not in HORIZONS:
        horizon = "1h"
    path = f"$.{horizon}"
    result = await db.execute(text("""
        SELECT primary_direction,
               COUNT(*) FILTER (WHERE (outcomes #> ARRAY[:h]) ? 'correct') AS sample,
               COUNT(*) FILTER (WHERE (outcomes #>> ARRAY[:h,'correct'])::boolean IS TRUE) AS correct,
               AVG((outcomes #>> ARRAY[:h,'directional_return_pct'])::double precision) AS avg_directional_return
        FROM heart_shadow_forecasts
        WHERE primary_direction IN ('LONG','SHORT')
          AND (outcomes #>> ARRAY[:h,'mature'])::boolean IS TRUE
        GROUP BY primary_direction
    """), {"h": horizon})
    rows = []
    for raw in result.mappings().all():
        item = dict(raw); n = int(item.get("sample") or 0); wins = int(item.get("correct") or 0)
        rate = wins / n * 100.0 if n else None
        adjustment = 0.0
        if n >= MIN_SAMPLE and rate is not None:
            if rate >= 62: adjustment = 5.0
            elif rate >= 56: adjustment = 2.5
            elif rate <= 38: adjustment = -5.0
            elif rate <= 44: adjustment = -2.5
        rows.append({
            "direction": item.get("primary_direction"), "sample": n, "correct": wins,
            "accuracy_pct": round(rate, 2) if rate is not None else None,
            "avg_directional_return_pct": round(_f(item.get("avg_directional_return")), 4),
            "status": "USABLE" if n >= MIN_SAMPLE else "CALIBRATING",
            "bounded_conviction_adjustment": adjustment,
        })
    return {"version": VERSION, "horizon": horizon, "minimum_sample": MIN_SAMPLE, "rows": rows, "score_is_probability": False}


async def persist_shadow_calibration_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    report = await shadow_calibration_report(db, horizon="1h")
    by_direction = {str(r.get("direction")): r for r in report.get("rows", [])}
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, s.direction, s.reason
        FROM signals s WHERE s.scanner_run_id=CAST(:run_id AS UUID)
    """), {"run_id": run_id})).mappings().all()
    updated = 0
    for raw in rows:
        reason = _d(raw.get("reason")); prediction = _d(reason.get("prediction")); heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart:
            continue
        contract = _d(heart.get("execution_contract")); direction = str(contract.get("primary_direction") or raw.get("direction") or "").upper()
        calibration = by_direction.get(direction, {"direction": direction, "sample": 0, "status": "CALIBRATING", "bounded_conviction_adjustment": 0.0})
        contract["shadow_calibration"] = calibration; heart["shadow_calibration"] = calibration
        lanes = _d(contract.get("lanes"))
        for lane in lanes.values():
            if isinstance(lane, dict):
                lane["shadow_calibration_status"] = calibration.get("status")
                lane["shadow_calibration_sample"] = calibration.get("sample")
                lane["shadow_conviction_adjustment"] = calibration.get("bounded_conviction_adjustment", 0.0)
        contract["lanes"] = lanes; heart["execution_contract"] = contract; reason["explodex_heart"] = heart
        if prediction:
            prediction["explodex_heart"] = heart; reason["prediction"] = prediction
        await db.execute(text("UPDATE signals SET reason=CAST(:reason AS JSONB), updated_at=NOW() WHERE id=CAST(:id AS UUID)"), {"id": raw["signal_id"], "reason": json.dumps(reason)})
        updated += 1
    await db.commit()
    return {"version": VERSION, "updated": updated, "calibration": report}
