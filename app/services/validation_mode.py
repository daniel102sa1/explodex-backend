from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client

HORIZONS = (5, 15, 30, 60, 120)
MIN_RESEARCH_SAMPLE = 100


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _hit(direction: str, high: float, low: float, level: float, profit: bool) -> bool:
    if level <= 0:
        return False
    if direction == "LONG":
        return high >= level if profit else low <= level
    return low <= level if profit else high >= level


def evaluate_horizon_candles(
    *,
    direction: str,
    entry: float,
    stop: float,
    tp1: float,
    candles: list[list[Any]],
    atr_pct: float = 0.0,
) -> dict[str, Any]:
    """Evaluate already-observed future candles without look-ahead in live decisions."""
    usable = [row for row in candles if len(row) >= 5]
    if not usable or entry <= 0:
        return {"available": False}

    highs = [_f(row[2]) for row in usable]
    lows = [_f(row[3]) for row in usable]
    closes = [_f(row[4]) for row in usable]

    if direction == "LONG":
        mfe_pct = (max(highs) - entry) / entry * 100.0
        mae_pct = (min(lows) - entry) / entry * 100.0
        directional_return_pct = (closes[-1] - entry) / entry * 100.0
    else:
        mfe_pct = (entry - min(lows)) / entry * 100.0
        mae_pct = (entry - max(highs)) / entry * 100.0
        directional_return_pct = (entry - closes[-1]) / entry * 100.0

    barrier = "NONE"
    barrier_at_ms: int | None = None
    for row in usable:
        high = _f(row[2])
        low = _f(row[3])
        tp_hit = _hit(direction, high, low, tp1, True)
        stop_hit = _hit(direction, high, low, stop, False)
        if tp_hit and stop_hit:
            barrier = "AMBIGUOUS"
            barrier_at_ms = int(row[0])
            break
        if tp_hit:
            barrier = "TP1"
            barrier_at_ms = int(row[0])
            break
        if stop_hit:
            barrier = "STOP"
            barrier_at_ms = int(row[0])
            break

    return {
        "available": True,
        "end_price": closes[-1],
        "mfe_pct": round(mfe_pct, 6),
        "mae_pct": round(mae_pct, 6),
        "directional_return_pct": round(directional_return_pct, 6),
        "mfe_atr": round(mfe_pct / atr_pct, 6) if atr_pct > 0 else None,
        "mae_atr": round(mae_pct / atr_pct, 6) if atr_pct > 0 else None,
        "barrier_hit": barrier,
        "barrier_at_ms": barrier_at_ms,
    }


async def ensure_validation_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS validation_observations (
            signal_id UUID PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
            symbol_id UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
            symbol VARCHAR(32) NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            direction VARCHAR(8) NOT NULL,
            entry_price NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12),
            tp1 NUMERIC(30,12),
            trade_class VARCHAR(24),
            grade VARCHAR(8),
            master_state VARCHAR(16),
            fingerprint_score NUMERIC(10,4),
            locks_passed INTEGER,
            catalyst_state VARCHAR(24),
            path_bias VARCHAR(12),
            data_quality VARCHAR(24),
            atr_pct NUMERIC(12,6),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS validation_horizon_results (
            signal_id UUID NOT NULL REFERENCES validation_observations(signal_id) ON DELETE CASCADE,
            horizon_minutes INTEGER NOT NULL,
            evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            end_price NUMERIC(30,12),
            mfe_pct NUMERIC(14,6),
            mae_pct NUMERIC(14,6),
            directional_return_pct NUMERIC(14,6),
            mfe_atr NUMERIC(14,6),
            mae_atr NUMERIC(14,6),
            barrier_hit VARCHAR(16),
            barrier_hit_at TIMESTAMPTZ,
            PRIMARY KEY (signal_id, horizon_minutes)
        )
    """))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_validation_obs_time ON validation_observations(observed_at DESC)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_validation_class ON validation_observations(trade_class, observed_at DESC)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS idx_validation_horizon ON validation_horizon_results(horizon_minutes, evaluated_at DESC)"))
    await db.commit()


async def capture_validation_observations(db: AsyncSession, limit: int = 250) -> dict[str, Any]:
    await ensure_validation_schema(db)
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, s.symbol_id::text, sy.symbol, s.created_at,
               s.direction, s.current_price, s.entry_low, s.entry_high, s.stop_loss, s.tp1, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        LEFT JOIN validation_observations vo ON vo.signal_id=s.id
        WHERE vo.signal_id IS NULL
          AND s.created_at >= NOW() - INTERVAL '24 hours'
        ORDER BY s.created_at ASC
        LIMIT :limit
    """), {"limit": limit})).mappings().all()

    captured = 0
    skipped = 0
    for raw in rows:
        row = dict(raw)
        reason = _json(row.get("reason"))
        prediction = _json(reason.get("prediction"))
        fingerprint = _json(prediction.get("premove_fingerprint"))
        stack = _json(prediction.get("prediction_stack_v5"))
        master = _json(stack.get("master_decision"))
        catalyst = _json(prediction.get("market_impact"))
        path = _json(prediction.get("path_forecast"))
        metrics = _json(reason.get("metrics"))
        entry_low = _f(row.get("entry_low"))
        entry_high = _f(row.get("entry_high"))
        entry = (entry_low + entry_high) / 2.0 if entry_low > 0 and entry_high > 0 else _f(row.get("current_price"))
        if entry <= 0:
            skipped += 1
            continue

        payload = {
            "prediction_type": prediction.get("type"),
            "phase": prediction.get("phase"),
            "setup_score": reason.get("setup_score"),
            "risk_score": reason.get("risk_score"),
            "fingerprint": fingerprint,
            "prediction_stack_v5": stack,
            "market_impact": catalyst,
            "path_forecast": path,
            "metrics": metrics,
            "components": reason.get("components") or {},
            "coinglass": reason.get("coinglass") or {},
        }
        await db.execute(text("""
            INSERT INTO validation_observations (
                signal_id, symbol_id, symbol, observed_at, direction, entry_price, stop_loss, tp1,
                trade_class, grade, master_state, fingerprint_score, locks_passed,
                catalyst_state, path_bias, data_quality, atr_pct, payload
            ) VALUES (
                CAST(:signal_id AS UUID), CAST(:symbol_id AS UUID), :symbol, :observed_at, :direction,
                :entry_price, :stop_loss, :tp1, :trade_class, :grade, :master_state,
                :fingerprint_score, :locks_passed, :catalyst_state, :path_bias, :data_quality,
                :atr_pct, CAST(:payload AS JSONB)
            ) ON CONFLICT (signal_id) DO NOTHING
        """), {
            "signal_id": row["signal_id"],
            "symbol_id": row["symbol_id"],
            "symbol": row["symbol"],
            "observed_at": row["created_at"],
            "direction": row["direction"],
            "entry_price": entry,
            "stop_loss": _f(row.get("stop_loss")),
            "tp1": _f(row.get("tp1")),
            "trade_class": fingerprint.get("trade_class") or "UNCLASSIFIED",
            "grade": fingerprint.get("grade"),
            "master_state": master.get("state"),
            "fingerprint_score": _f(fingerprint.get("fingerprint_score")),
            "locks_passed": int(_f(fingerprint.get("locks_passed"), 0.0)),
            "catalyst_state": catalyst.get("state"),
            "path_bias": path.get("final_bias"),
            "data_quality": reason.get("data_quality"),
            "atr_pct": _f(metrics.get("atr_pct")),
            "payload": json.dumps(payload),
        })
        captured += 1
    await db.commit()
    return {"seen": len(rows), "captured": captured, "skipped": skipped}


async def evaluate_due_horizons(db: AsyncSession, limit: int = 80) -> dict[str, Any]:
    await ensure_validation_schema(db)
    rows = (await db.execute(text("""
        SELECT vo.signal_id::text, vo.symbol, vo.observed_at, vo.direction, vo.entry_price,
               vo.stop_loss, vo.tp1, vo.atr_pct, h.horizon_minutes
        FROM validation_observations vo
        CROSS JOIN (VALUES (5),(15),(30),(60),(120)) AS h(horizon_minutes)
        LEFT JOIN validation_horizon_results vr
          ON vr.signal_id=vo.signal_id AND vr.horizon_minutes=h.horizon_minutes
        WHERE vr.signal_id IS NULL
          AND vo.observed_at + (h.horizon_minutes || ' minutes')::interval <= NOW()
          AND vo.observed_at >= NOW() - INTERVAL '6 hours'
        ORDER BY vo.observed_at ASC, h.horizon_minutes ASC
        LIMIT :limit
    """), {"limit": limit})).mappings().all()

    by_signal: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        by_signal.setdefault(row["signal_id"], []).append(row)

    evaluated = 0
    errors: list[str] = []
    for signal_rows in by_signal.values():
        base = signal_rows[0]
        try:
            max_h = max(int(r["horizon_minutes"]) for r in signal_rows)
            klines = await binance_client.klines(base["symbol"], interval="1m", limit=min(500, max_h + 30))
            start_ms = int(base["observed_at"].timestamp() * 1000)
            for row in signal_rows:
                horizon = int(row["horizon_minutes"])
                end_ms = start_ms + horizon * 60_000
                future = [k for k in klines if len(k) >= 5 and start_ms <= int(k[0]) <= end_ms]
                result = evaluate_horizon_candles(
                    direction=str(row["direction"]),
                    entry=_f(row["entry_price"]),
                    stop=_f(row["stop_loss"]),
                    tp1=_f(row["tp1"]),
                    candles=future,
                    atr_pct=_f(row["atr_pct"]),
                )
                if not result.get("available"):
                    continue
                barrier_at = None
                if result.get("barrier_at_ms"):
                    barrier_at = datetime.fromtimestamp(result["barrier_at_ms"] / 1000, tz=timezone.utc)
                await db.execute(text("""
                    INSERT INTO validation_horizon_results (
                        signal_id, horizon_minutes, end_price, mfe_pct, mae_pct,
                        directional_return_pct, mfe_atr, mae_atr, barrier_hit, barrier_hit_at
                    ) VALUES (
                        CAST(:signal_id AS UUID), :horizon, :end_price, :mfe_pct, :mae_pct,
                        :directional_return_pct, :mfe_atr, :mae_atr, :barrier_hit, :barrier_hit_at
                    ) ON CONFLICT (signal_id, horizon_minutes) DO NOTHING
                """), {
                    "signal_id": row["signal_id"],
                    "horizon": horizon,
                    "end_price": result["end_price"],
                    "mfe_pct": result["mfe_pct"],
                    "mae_pct": result["mae_pct"],
                    "directional_return_pct": result["directional_return_pct"],
                    "mfe_atr": result["mfe_atr"],
                    "mae_atr": result["mae_atr"],
                    "barrier_hit": result["barrier_hit"],
                    "barrier_hit_at": barrier_at,
                })
                evaluated += 1
        except Exception as exc:
            errors.append(f"{base.get('symbol')}: {type(exc).__name__}: {str(exc)[:180]}")
    await db.commit()
    return {"due": len(rows), "evaluated": evaluated, "errors": errors[:10]}


async def validation_report(db: AsyncSession) -> dict[str, Any]:
    await ensure_validation_schema(db)
    observations = [dict(r) for r in (await db.execute(text("""
        SELECT signal_id::text, symbol, observed_at, direction, trade_class, grade, master_state,
               fingerprint_score, locks_passed, catalyst_state, path_bias, data_quality, atr_pct
        FROM validation_observations
        ORDER BY observed_at DESC
        LIMIT 5000
    """))).mappings().all()]
    results = [dict(r) for r in (await db.execute(text("""
        SELECT vr.signal_id::text, vr.horizon_minutes, vr.mfe_pct, vr.mae_pct,
               vr.directional_return_pct, vr.mfe_atr, vr.mae_atr, vr.barrier_hit
        FROM validation_horizon_results vr
        JOIN validation_observations vo ON vo.signal_id=vr.signal_id
        WHERE vo.observed_at >= NOW() - INTERVAL '30 days'
        ORDER BY vr.evaluated_at DESC
        LIMIT 25000
    """))).mappings().all()]

    obs_by_id = {row["signal_id"]: row for row in observations}
    class_counts: dict[str, int] = {}
    for row in observations:
        key = str(row.get("trade_class") or "UNCLASSIFIED")
        class_counts[key] = class_counts.get(key, 0) + 1

    buckets: dict[tuple[str, int], dict[str, Any]] = {}
    for row in results:
        obs = obs_by_id.get(row["signal_id"])
        if not obs:
            continue
        key = (str(obs.get("trade_class") or "UNCLASSIFIED"), int(row["horizon_minutes"]))
        bucket = buckets.setdefault(key, {
            "trade_class": key[0], "horizon_minutes": key[1], "sample": 0,
            "tp1_first": 0, "stop_first": 0, "ambiguous": 0, "no_barrier": 0,
            "sum_mfe": 0.0, "sum_mae_abs": 0.0, "sum_return": 0.0,
            "sum_mfe_atr": 0.0, "mfe_atr_n": 0,
        })
        bucket["sample"] += 1
        barrier = str(row.get("barrier_hit") or "NONE")
        if barrier == "TP1": bucket["tp1_first"] += 1
        elif barrier == "STOP": bucket["stop_first"] += 1
        elif barrier == "AMBIGUOUS": bucket["ambiguous"] += 1
        else: bucket["no_barrier"] += 1
        bucket["sum_mfe"] += _f(row.get("mfe_pct"))
        bucket["sum_mae_abs"] += abs(_f(row.get("mae_pct")))
        bucket["sum_return"] += _f(row.get("directional_return_pct"))
        if row.get("mfe_atr") is not None:
            bucket["sum_mfe_atr"] += _f(row.get("mfe_atr"))
            bucket["mfe_atr_n"] += 1

    cohort_rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        n = bucket["sample"]
        decided = bucket["tp1_first"] + bucket["stop_first"]
        cohort_rows.append({
            "trade_class": bucket["trade_class"],
            "horizon_minutes": bucket["horizon_minutes"],
            "sample": n,
            "tp1_first": bucket["tp1_first"],
            "stop_first": bucket["stop_first"],
            "ambiguous": bucket["ambiguous"],
            "no_barrier": bucket["no_barrier"],
            "tp1_before_stop_rate_pct": round(bucket["tp1_first"] / decided * 100.0, 2) if decided else None,
            "avg_mfe_pct": round(bucket["sum_mfe"] / n, 4) if n else None,
            "avg_mae_abs_pct": round(bucket["sum_mae_abs"] / n, 4) if n else None,
            "avg_directional_return_pct": round(bucket["sum_return"] / n, 4) if n else None,
            "avg_mfe_atr": round(bucket["sum_mfe_atr"] / bucket["mfe_atr_n"], 4) if bucket["mfe_atr_n"] else None,
            "rate_is_probability": False,
        })
    cohort_rows.sort(key=lambda r: (r["horizon_minutes"], r["trade_class"]))

    sixty = [r for r in results if int(r["horizon_minutes"]) == 60 and r["signal_id"] in obs_by_id]
    false_yes = 0
    missed_proxy = 0
    for row in sixty:
        obs = obs_by_id[row["signal_id"]]
        cls = str(obs.get("trade_class") or "UNCLASSIFIED")
        if cls == "TRADE_NOW" and str(row.get("barrier_hit")) == "STOP":
            false_yes += 1
        if cls not in {"TRADE_NOW", "TRADE_SOON"} and _f(row.get("mfe_atr"), 0.0) >= 1.0 and _f(row.get("mae_atr"), 0.0) > -0.75:
            missed_proxy += 1

    labeled_60 = len(sixty)
    research_status = "CALIBRATING" if labeled_60 < MIN_RESEARCH_SAMPLE else "EARLY_RESEARCH" if labeled_60 < 300 else "RESEARCH_READY"
    actionable = class_counts.get("TRADE_NOW", 0) + class_counts.get("TRADE_SOON", 0)

    return {
        "version": "validation_mode_v1",
        "research_status": research_status,
        "minimum_research_sample": MIN_RESEARCH_SAMPLE,
        "observations": len(observations),
        "horizon_results": len(results),
        "labeled_60m": labeled_60,
        "trade_class_counts": class_counts,
        "actionable_share_pct": round(actionable / len(observations) * 100.0, 2) if observations else 0.0,
        "cohorts": cohort_rows,
        "diagnostics": {
            "false_yes_60m_stop_first": false_yes,
            "missed_opportunity_proxy_60m": missed_proxy,
            "missed_proxy_definition": "No TRADE_NOW/TRADE_SOON, MFE >= 1 ATR and MAE better than -0.75 ATR within 60m.",
        },
        "safety": {
            "paper_research_only": True,
            "rates_are_probabilities": False,
            "changes_live_entry_rules": False,
            "requires_out_of_sample_review_before_calibration": True,
        },
        "note": "Validation Mode measures what happened after prior predictions. It does not use future data in live decisions and does not prove guaranteed profitability.",
    }


async def recent_validation_rows(db: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    await ensure_validation_schema(db)
    rows = (await db.execute(text("""
        SELECT vo.signal_id::text, vo.symbol, vo.observed_at, vo.direction, vo.trade_class,
               vo.grade, vo.master_state, vo.fingerprint_score, vo.locks_passed,
               vo.catalyst_state, vo.path_bias, vo.data_quality,
               vr.horizon_minutes, vr.mfe_pct, vr.mae_pct, vr.directional_return_pct,
               vr.mfe_atr, vr.mae_atr, vr.barrier_hit
        FROM validation_observations vo
        LEFT JOIN validation_horizon_results vr ON vr.signal_id=vo.signal_id
        ORDER BY vo.observed_at DESC, vr.horizon_minutes ASC
        LIMIT :limit
    """), {"limit": max(1, min(limit, 500))})).mappings().all()
    return [dict(r) for r in rows]


async def run_validation_cycle(db: AsyncSession) -> dict[str, Any]:
    captured = await capture_validation_observations(db)
    evaluated = await evaluate_due_horizons(db)
    return {"capture": captured, "evaluate": evaluated}
