from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client


MIN_CALIBRATION_SAMPLE = 30
SIMILARITY_MIN = 0.45
SIMILAR_CASE_LIMIT = 60


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _hit(direction: str, high: float, low: float, level: float, profit: bool) -> bool:
    if level <= 0:
        return False
    if direction == "LONG":
        return high >= level if profit else low <= level
    return low <= level if profit else high >= level


def _mfe_mae(direction: str, entry: float, highs: list[float], lows: list[float]) -> tuple[float, float]:
    if not highs or not lows or entry <= 0:
        return 0.0, 0.0
    if direction == "LONG":
        mfe = (max(highs) - entry) / entry * 100
        mae = (min(lows) - entry) / entry * 100
    else:
        mfe = (entry - min(lows)) / entry * 100
        mae = (entry - max(highs)) / entry * 100
    return mfe, mae


def _ratio_feature(value: Any) -> float:
    """Map ratios around 1.0 to a stable -1..1 feature."""
    return _clamp((_f(value, 1.0) - 1.0) / 0.5, -1.0, 1.0)


def _feature_vector(row: dict[str, Any]) -> dict[str, float]:
    features = _json(row.get("features"))
    metrics = _json(features.get("metrics"))
    seq = _json(features.get("prediction_sequence"))
    cg = _json(features.get("coinglass"))
    cg_oi = _json(cg.get("open_interest"))
    cg_taker = _json(cg.get("taker"))

    return {
        "setup": _clamp(_f(row.get("setup_score")) / 100.0, 0.0, 1.0),
        "pre": _clamp(_f(row.get("preactivation_score")) / 100.0, 0.0, 1.0),
        "risk": _clamp(_f(row.get("risk_score")) / 100.0, 0.0, 1.0),
        "rvol": _clamp(_f(metrics.get("relative_volume"), _f(seq.get("relative_volume"), 1.0)) / 3.0, 0.0, 1.5),
        "vol_accel": _clamp(_f(metrics.get("volume_acceleration"), _f(seq.get("volume_acceleration"), 1.0)) / 3.0, 0.0, 1.5),
        "oi": _clamp(_f(metrics.get("oi_change_pct")) / 3.0, -1.0, 1.0),
        "futures": _clamp(_f(metrics.get("futures_delta_ratio")), -1.0, 1.0),
        "spot": _clamp(_f(metrics.get("spot_delta_ratio")), -1.0, 1.0),
        "book": _clamp(_f(metrics.get("order_book_imbalance")), -1.0, 1.0),
        "taker": _ratio_feature(metrics.get("taker_avg_3")),
        "atr": _clamp(_f(metrics.get("atr_pct")) / 5.0, 0.0, 1.5),
        "change5": _clamp(_f(metrics.get("change_5m_pct")) / 3.0, -1.0, 1.0),
        "change15": _clamp(_f(metrics.get("change_15m_pct")) / 6.0, -1.0, 1.0),
        "cg_oi": _clamp(_f(cg_oi.get("change_15m_pct")) / 3.0, -1.0, 1.0),
        "cg_taker": _ratio_feature(cg_taker.get("buy_sell_ratio")),
    }


FEATURE_WEIGHTS: dict[str, float] = {
    "setup": 1.0,
    "pre": 1.4,
    "risk": 1.0,
    "rvol": 0.9,
    "vol_accel": 1.0,
    "oi": 1.1,
    "futures": 1.2,
    "spot": 1.3,
    "book": 0.8,
    "taker": 0.9,
    "atr": 0.7,
    "change5": 0.8,
    "change15": 0.7,
    "cg_oi": 1.0,
    "cg_taker": 0.8,
}


def _similarity(current: dict[str, Any], past: dict[str, Any]) -> float:
    a = _feature_vector(current)
    b = _feature_vector(past)
    weight_sum = sum(FEATURE_WEIGHTS.values())
    distance = sum(FEATURE_WEIGHTS[k] * abs(a[k] - b[k]) for k in FEATURE_WEIGHTS) / max(weight_sum, 1e-9)

    # Same setup family/regime is rewarded, but not required. Direction is
    # filtered in SQL because LONG/SHORT feature meaning differs materially.
    if str(current.get("prediction_type") or "") != str(past.get("prediction_type") or ""):
        distance += 0.08
    if str(current.get("btc_regime") or "") != str(past.get("btc_regime") or ""):
        distance += 0.05
    if str(current.get("phase") or "") != str(past.get("phase") or ""):
        distance += 0.03
    return round(_clamp(1.0 - distance, 0.0, 1.0), 4)


def _wilson_interval(wins: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return max(0.0, center - margin) * 100, min(1.0, center + margin) * 100


async def record_edge_observation(
    db: AsyncSession,
    *,
    signal_id: str,
    symbol_id: str,
    symbol: str,
    score: dict[str, Any],
    prediction: dict[str, Any],
    market_source: str | None,
    observed_at: datetime | None = None,
) -> None:
    entry_low = _f(score.get("entry_low"))
    entry_high = _f(score.get("entry_high"))
    entry = (entry_low + entry_high) / 2 if entry_low and entry_high else _f(score.get("current_price"))
    max_minutes = int(prediction.get("expected_duration_max_minutes") or score.get("expected_duration_max_minutes") or 240)
    max_minutes = max(30, min(max_minutes, 1440))
    features = {
        "symbol": symbol,
        "metrics": score.get("metrics") or {},
        "components": score.get("components") or {},
        "prediction_sequence": prediction.get("sequence") or {},
        "confirmations": prediction.get("confirmations") or [],
        "conflicts": prediction.get("conflicts") or [],
        "coinglass": score.get("coinglass") or {},
    }
    await db.execute(
        text(
            """
            INSERT INTO edge_observations (
                signal_id, symbol_id, observed_at, due_at, direction,
                prediction_type, phase, setup_score, preactivation_score, risk_score,
                entry_price, stop_loss, tp1, tp2, tp3,
                btc_regime, market_source, features
            ) VALUES (
                :signal_id, :symbol_id, COALESCE(:observed_at, NOW()),
                COALESCE(:observed_at, NOW()) + (:minutes || ' minutes')::interval,
                :direction, :prediction_type, :phase, :setup_score, :preactivation_score,
                :risk_score, :entry_price, :stop_loss, :tp1, :tp2, :tp3,
                :btc_regime, :market_source, CAST(:features AS JSONB)
            ) ON CONFLICT (signal_id) DO NOTHING
            """
        ),
        {
            "signal_id": signal_id,
            "symbol_id": symbol_id,
            "observed_at": observed_at,
            "minutes": str(max_minutes),
            "direction": score.get("direction"),
            "prediction_type": prediction.get("type"),
            "phase": prediction.get("phase"),
            "setup_score": _f(score.get("setup_score")),
            "preactivation_score": _f(prediction.get("preactivation_score")),
            "risk_score": _f(score.get("risk_score")),
            "entry_price": entry,
            "stop_loss": _f(score.get("stop_loss")),
            "tp1": _f(score.get("tp1")),
            "tp2": _f(score.get("tp2")),
            "tp3": _f(score.get("tp3")),
            "btc_regime": str((score.get("metrics") or {}).get("btc_trend") or "NEUTRAL"),
            "market_source": market_source,
            "features": json.dumps(features),
        },
    )


async def capture_recent_signals(db: AsyncSession, limit: int = 100) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT s.id::text AS signal_id, s.symbol_id::text, sy.symbol, s.created_at,
                   s.direction, s.state, s.setup_score, s.risk_score, s.current_price,
                   s.entry_low, s.entry_high, s.stop_loss, s.tp1, s.tp2, s.tp3,
                   s.expected_duration_max_minutes, s.reason
            FROM signals s
            JOIN symbols sy ON sy.id=s.symbol_id
            LEFT JOIN edge_observations eo ON eo.signal_id=s.id
            WHERE eo.signal_id IS NULL
              AND s.created_at >= NOW() - INTERVAL '24 hours'
            ORDER BY s.created_at ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows = [dict(r) for r in result.mappings().all()]
    captured = 0
    for row in rows:
        reason = _json(row.get("reason"))
        prediction = _json(reason.get("prediction"))
        metrics = _json(reason.get("metrics"))
        components = _json(reason.get("components"))
        coinglass = _json(reason.get("coinglass"))
        if not prediction:
            continue
        score = {
            "direction": row.get("direction"),
            "state": row.get("state"),
            "setup_score": _f(row.get("setup_score")),
            "risk_score": _f(row.get("risk_score")),
            "current_price": _f(row.get("current_price")),
            "entry_low": _f(row.get("entry_low")),
            "entry_high": _f(row.get("entry_high")),
            "stop_loss": _f(row.get("stop_loss")),
            "tp1": _f(row.get("tp1")),
            "tp2": _f(row.get("tp2")),
            "tp3": _f(row.get("tp3")),
            "expected_duration_max_minutes": row.get("expected_duration_max_minutes"),
            "metrics": metrics,
            "components": components,
            "coinglass": coinglass,
        }
        await record_edge_observation(
            db,
            signal_id=row["signal_id"],
            symbol_id=row["symbol_id"],
            symbol=row["symbol"],
            score=score,
            prediction=prediction,
            market_source=str(reason.get("market_data_source") or "scanner"),
            observed_at=row.get("created_at"),
        )
        captured += 1
    await db.commit()
    return {"seen": len(rows), "captured": captured}


async def label_due_observations(db: AsyncSession, limit: int = 40) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT eo.signal_id::text, sy.symbol, eo.observed_at, eo.due_at,
                   eo.direction, eo.entry_price, eo.stop_loss, eo.tp1, eo.tp2, eo.tp3
            FROM edge_observations eo
            JOIN symbols sy ON sy.id = eo.symbol_id
            WHERE eo.status = 'PENDING' AND eo.due_at <= NOW()
            ORDER BY eo.due_at ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows = [dict(r) for r in result.mappings().all()]
    labeled = 0
    errors: list[str] = []

    for row in rows:
        try:
            observed_at: datetime = row["observed_at"]
            due_at: datetime = row["due_at"]
            total_minutes = max(30, int((due_at - observed_at).total_seconds() / 60))
            candle_limit = max(30, min(1500, total_minutes + 10))
            klines = await binance_client.klines(row["symbol"], interval="1m", limit=candle_limit)
            start_ms = int(observed_at.timestamp() * 1000)
            end_ms = int(due_at.timestamp() * 1000)
            future = [k for k in klines if len(k) >= 5 and start_ms <= int(k[0]) <= end_ms]
            if not future:
                errors.append(f"{row['symbol']}: no future candles")
                continue

            direction = str(row["direction"])
            entry = _f(row["entry_price"])
            stop = _f(row["stop_loss"])
            tp1 = _f(row["tp1"])
            highs = [_f(k[2]) for k in future]
            lows = [_f(k[3]) for k in future]
            closes = [_f(k[4]) for k in future]

            barrier = "NONE"
            barrier_at: datetime | None = None
            for k in future:
                high = _f(k[2])
                low = _f(k[3])
                stop_hit = _hit(direction, high, low, stop, False)
                tp_hit = _hit(direction, high, low, tp1, True)
                if stop_hit and tp_hit:
                    barrier = "STOP"
                    barrier_at = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc)
                    break
                if stop_hit:
                    barrier = "STOP"
                    barrier_at = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc)
                    break
                if tp_hit:
                    barrier = "TP1"
                    barrier_at = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc)
                    break

            mfe, mae = _mfe_mae(direction, entry, highs, lows)
            end_price = closes[-1]
            risk_per_unit = abs(entry - stop)
            if barrier == "TP1" and risk_per_unit > 0:
                outcome_r = abs(tp1 - entry) / risk_per_unit
                label = "WIN"
            elif barrier == "STOP":
                outcome_r = -1.0
                label = "LOSS"
            else:
                pnl_per_unit = (end_price - entry) if direction == "LONG" else (entry - end_price)
                outcome_r = pnl_per_unit / risk_per_unit if risk_per_unit > 0 else 0.0
                label = "UNRESOLVED"

            await db.execute(
                text(
                    """
                    UPDATE edge_observations
                    SET status='LABELED', label=:label, barrier_hit=:barrier,
                        barrier_hit_at=:barrier_at, end_price=:end_price,
                        mfe_pct=:mfe, mae_pct=:mae, outcome_r=:outcome_r,
                        labeled_at=NOW()
                    WHERE signal_id=:signal_id
                    """
                ),
                {
                    "signal_id": row["signal_id"],
                    "label": label,
                    "barrier": barrier,
                    "barrier_at": barrier_at,
                    "end_price": end_price,
                    "mfe": mfe,
                    "mae": mae,
                    "outcome_r": outcome_r,
                },
            )
            labeled += 1
        except Exception as exc:
            errors.append(f"{row.get('symbol')}: {type(exc).__name__}: {str(exc)[:180]}")

    await db.commit()
    return {"checked": len(rows), "labeled": labeled, "errors": errors[:10]}


async def similar_case_summary(db: AsyncSession, symbol: str) -> dict[str, Any]:
    current_result = await db.execute(
        text(
            """
            SELECT eo.signal_id::text, sy.symbol, eo.direction, eo.prediction_type,
                   eo.phase, eo.btc_regime, eo.setup_score, eo.preactivation_score,
                   eo.risk_score, eo.features
            FROM edge_observations eo
            JOIN symbols sy ON sy.id=eo.symbol_id
            WHERE sy.symbol=:symbol
            ORDER BY eo.observed_at DESC
            LIMIT 1
            """
        ),
        {"symbol": symbol},
    )
    current_row = current_result.mappings().first()
    if not current_row:
        return {
            "available": False,
            "sample": 0,
            "decided": 0,
            "calibration_status": "NO_CURRENT_OBSERVATION",
            "observed_win_rate_pct": None,
            "weighted_win_rate_pct": None,
            "note": "Todavía no existe una observación Edge para comparar este setup.",
        }

    current = dict(current_row)
    candidates_result = await db.execute(
        text(
            """
            SELECT eo.signal_id::text, sy.symbol, eo.observed_at, eo.direction,
                   eo.prediction_type, eo.phase, eo.btc_regime, eo.setup_score,
                   eo.preactivation_score, eo.risk_score, eo.features,
                   eo.label, eo.outcome_r, eo.mfe_pct, eo.mae_pct
            FROM edge_observations eo
            JOIN symbols sy ON sy.id=eo.symbol_id
            WHERE eo.status='LABELED'
              AND eo.label IN ('WIN','LOSS')
              AND eo.direction=:direction
              AND eo.signal_id<>CAST(:signal_id AS UUID)
            ORDER BY eo.labeled_at DESC
            LIMIT 1500
            """
        ),
        {"direction": current.get("direction"), "signal_id": current.get("signal_id")},
    )

    scored: list[dict[str, Any]] = []
    for row in candidates_result.mappings().all():
        item = dict(row)
        sim = _similarity(current, item)
        if sim < SIMILARITY_MIN:
            continue
        item["similarity"] = sim
        scored.append(item)

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    selected = scored[:SIMILAR_CASE_LIMIT]
    wins = sum(1 for x in selected if x.get("label") == "WIN")
    losses = sum(1 for x in selected if x.get("label") == "LOSS")
    decided = wins + losses
    calibrated = decided >= MIN_CALIBRATION_SAMPLE

    weighted_total = sum(float(x["similarity"]) ** 2 for x in selected)
    weighted_wins = sum(float(x["similarity"]) ** 2 for x in selected if x.get("label") == "WIN")
    weighted_rate = (weighted_wins / weighted_total * 100) if calibrated and weighted_total > 0 else None
    raw_rate = (wins / decided * 100) if calibrated and decided else None
    weighted_avg_r = (
        sum(_f(x.get("outcome_r")) * float(x["similarity"]) ** 2 for x in selected) / weighted_total
        if selected and weighted_total > 0
        else None
    )
    avg_similarity = sum(float(x["similarity"]) for x in selected) / len(selected) if selected else 0.0
    wilson_low, wilson_high = _wilson_interval(wins, decided) if calibrated else (None, None)

    examples = [
        {
            "symbol": x.get("symbol"),
            "at": x.get("observed_at").isoformat() if isinstance(x.get("observed_at"), datetime) else str(x.get("observed_at") or ""),
            "type": x.get("prediction_type"),
            "regime": x.get("btc_regime"),
            "outcome": x.get("label"),
            "outcome_r": _f(x.get("outcome_r")),
            "similarity_pct": round(float(x["similarity"]) * 100, 1),
        }
        for x in selected[:6]
    ]

    return {
        "available": bool(selected),
        "sample": len(selected),
        "decided": decided,
        "wins": wins,
        "losses": losses,
        "avg_similarity_pct": round(avg_similarity * 100, 1),
        "observed_win_rate_pct": round(raw_rate, 2) if raw_rate is not None else None,
        "weighted_win_rate_pct": round(weighted_rate, 2) if weighted_rate is not None else None,
        "weighted_avg_r": round(weighted_avg_r, 3) if weighted_avg_r is not None else None,
        "wilson_low_pct": round(wilson_low, 2) if wilson_low is not None else None,
        "wilson_high_pct": round(wilson_high, 2) if wilson_high is not None else None,
        "calibration_status": "CALIBRATED" if calibrated else "INSUFFICIENT_SIMILAR_CASES",
        "minimum_decided_for_probability": MIN_CALIBRATION_SAMPLE,
        "similarity_threshold_pct": SIMILARITY_MIN * 100,
        "examples": examples,
        "note": "Tasa empírica basada en setups etiquetados parecidos de todo el mercado. No es garantía del siguiente trade.",
    }


async def edge_summary(db: AsyncSession, *, symbol: str | None = None) -> dict[str, Any]:
    filters = "WHERE eo.status='LABELED'"
    params: dict[str, Any] = {}
    if symbol:
        filters += " AND sy.symbol=:symbol"
        params["symbol"] = symbol

    result = await db.execute(
        text(
            f"""
            SELECT COUNT(*)::int AS n,
                   COUNT(*) FILTER (WHERE eo.label='WIN')::int AS wins,
                   COUNT(*) FILTER (WHERE eo.label='LOSS')::int AS losses,
                   COUNT(*) FILTER (WHERE eo.label='UNRESOLVED')::int AS unresolved,
                   AVG(eo.outcome_r)::float AS avg_r,
                   AVG(eo.mfe_pct)::float AS avg_mfe_pct,
                   AVG(eo.mae_pct)::float AS avg_mae_pct
            FROM edge_observations eo
            JOIN symbols sy ON sy.id=eo.symbol_id
            {filters}
            """
        ),
        params,
    )
    row = dict(result.mappings().first() or {})
    n = int(row.get("n") or 0)
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    decided = wins + losses
    calibrated = decided >= MIN_CALIBRATION_SAMPLE
    win_rate = (wins / decided * 100) if calibrated and decided else None

    cohort_result = await db.execute(
        text(
            f"""
            SELECT eo.direction, eo.prediction_type, eo.btc_regime,
                   COUNT(*)::int AS n,
                   COUNT(*) FILTER (WHERE eo.label='WIN')::int AS wins,
                   COUNT(*) FILTER (WHERE eo.label='LOSS')::int AS losses,
                   AVG(eo.outcome_r)::float AS avg_r
            FROM edge_observations eo
            JOIN symbols sy ON sy.id=eo.symbol_id
            {filters}
            GROUP BY eo.direction, eo.prediction_type, eo.btc_regime
            HAVING COUNT(*) >= 3
            ORDER BY COUNT(*) DESC
            LIMIT 20
            """
        ),
        params,
    )
    cohorts = []
    for r in cohort_result.mappings().all():
        d = dict(r)
        decided_c = int(d.get("wins") or 0) + int(d.get("losses") or 0)
        d["observed_win_rate_pct"] = round((int(d.get("wins") or 0) / decided_c * 100), 2) if decided_c >= MIN_CALIBRATION_SAMPLE else None
        d["calibration_status"] = "CALIBRATED" if decided_c >= MIN_CALIBRATION_SAMPLE else "INSUFFICIENT_SAMPLE"
        cohorts.append(d)

    similar = await similar_case_summary(db, symbol) if symbol else None
    return {
        "sample": n,
        "decided": decided,
        "wins": wins,
        "losses": losses,
        "unresolved": int(row.get("unresolved") or 0),
        "observed_win_rate_pct": round(win_rate, 2) if win_rate is not None else None,
        "avg_r": row.get("avg_r"),
        "avg_mfe_pct": row.get("avg_mfe_pct"),
        "avg_mae_pct": row.get("avg_mae_pct"),
        "calibration_status": "CALIBRATED" if calibrated else "INSUFFICIENT_SAMPLE",
        "minimum_decided_for_probability": MIN_CALIBRATION_SAMPLE,
        "cohorts": cohorts,
        "similar_cases": similar,
        "note": "Probabilidades solo se muestran con muestra decidida suficiente; antes el sistema se abstiene de presentarlas como certeza.",
    }
