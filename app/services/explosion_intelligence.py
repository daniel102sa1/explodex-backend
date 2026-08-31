from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "explosion_intelligence_v1"
MODEL_CACHE_SECONDS = 60.0
_model_cache: tuple[float, dict[str, Any]] | None = None


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


def _bucket(score: float) -> str:
    if score >= 88:
        return "88+"
    if score >= 82:
        return "82-87"
    if score >= 76:
        return "76-81"
    if score >= 70:
        return "70-75"
    return "<70"


def extract_signal_features(reason: Any) -> dict[str, Any]:
    bundle = _d(reason)
    prediction = _d(bundle.get("prediction"))
    heart = _d(bundle.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
    ignition = _d(heart.get("ignition"))
    decision = _d(heart.get("action_decision"))
    liquidity = _d(heart.get("liquidity_intelligence"))
    htf = _d(heart.get("higher_timeframe"))
    htf_alignment = _d(heart.get("higher_timeframe_alignment"))
    htf_frames = _d(htf.get("frames"))
    metrics = _d(bundle.get("metrics"))
    coinglass = _d(bundle.get("coinglass"))
    if not coinglass:
        coinglass = _d(metrics.get("coinglass"))
    cg_oi = _d(coinglass.get("open_interest"))
    cg_taker = _d(coinglass.get("taker"))
    cg_funding = _d(coinglass.get("funding"))
    cg_liq = _d(coinglass.get("liquidations"))
    sequence = _d(prediction.get("sequence"))
    context = _d(prediction.get("context_engine"))
    regime = _d(context.get("regime"))

    return {
        "feature_version": VERSION,
        "heart_action": decision.get("action"),
        "heart_action_via": decision.get("via"),
        "ignition_score": ignition.get("score"),
        "ignition_stage": ignition.get("stage"),
        "ignition_supporting": ignition.get("supporting_components"),
        "ignition_strong": ignition.get("strong_components"),
        "ignition_components": ignition.get("components") or {},
        "preactivation_score": prediction.get("preactivation_score"),
        "prediction_type": prediction.get("type"),
        "prediction_phase": prediction.get("phase"),
        "market_regime": regime.get("regime") or sequence.get("market_regime"),
        "change_5m_pct": metrics.get("change_5m_pct"),
        "change_15m_pct": metrics.get("change_15m_pct"),
        "change_1h_pct": metrics.get("change_1h_pct"),
        "atr_pct": metrics.get("atr_pct"),
        "compression_ratio": metrics.get("compression_ratio"),
        "relative_volume": metrics.get("relative_volume"),
        "volume_acceleration": metrics.get("volume_acceleration"),
        "oi_change_pct": metrics.get("oi_change_pct"),
        "futures_delta_ratio": metrics.get("futures_delta_ratio"),
        "spot_delta_ratio": metrics.get("spot_delta_ratio"),
        "order_book_imbalance": metrics.get("order_book_imbalance"),
        "funding_rate": metrics.get("funding_rate"),
        "cg_oi_5m_pct": cg_oi.get("change_5m_pct"),
        "cg_oi_15m_pct": cg_oi.get("change_15m_pct"),
        "cg_oi_1h_pct": cg_oi.get("change_1h_pct"),
        "cg_oi_4h_pct": cg_oi.get("change_4h_pct"),
        "cg_oi_24h_pct": cg_oi.get("change_24h_pct"),
        "cg_taker_buy_sell_ratio": cg_taker.get("buy_sell_ratio"),
        "cg_funding_median_pct": cg_funding.get("median_rate_pct"),
        "cg_long_liq_1h": cg_liq.get("long_1h"),
        "cg_short_liq_1h": cg_liq.get("short_1h"),
        "cg_long_liq_4h": cg_liq.get("long_4h"),
        "cg_short_liq_4h": cg_liq.get("short_4h"),
        "cg_liq_imbalance_1h": cg_liq.get("short_minus_long_imbalance_1h"),
        "liquidity_attraction_direction": liquidity.get("attraction_direction"),
        "liquidity_up_score": liquidity.get("upward_attraction_score"),
        "liquidity_down_score": liquidity.get("downward_attraction_score"),
        "liquidity_aligned": liquidity.get("aligned_with_thesis"),
        "htf_bias": htf.get("bias"),
        "htf_aligned_frames": htf_alignment.get("aligned_frames"),
        "htf_conflicting_frames": htf_alignment.get("conflicting_frames"),
        "htf_4h_trend": _d(htf_frames.get("4h")).get("trend"),
        "htf_6h_trend": _d(htf_frames.get("6h")).get("trend"),
        "htf_1d_trend": _d(htf_frames.get("1d")).get("trend"),
        "htf_4h_change_pct": _d(htf_frames.get("4h")).get("change_pct"),
        "htf_6h_change_pct": _d(htf_frames.get("6h")).get("change_pct"),
        "htf_1d_change_pct": _d(htf_frames.get("1d")).get("change_pct"),
        "sweep_low": bool(sequence.get("sweep_low")),
        "sweep_high": bool(sequence.get("sweep_high")),
        "sell_absorption_rebound": bool(sequence.get("sell_absorption_rebound")),
        "buy_absorption_rejection": bool(sequence.get("buy_absorption_rejection")),
        "chase_risk": bool(sequence.get("chase_risk")),
    }


async def enrich_verdict_features(db: AsyncSession, limit: int = 250) -> dict[str, int]:
    rows = (await db.execute(text("""
        SELECT vm.id::text AS id, vm.metadata, s.reason
        FROM verdict_memory vm
        JOIN signals s ON s.id=vm.signal_id
        WHERE COALESCE(vm.metadata->>'feature_version','') <> :version
        ORDER BY vm.observed_at DESC
        LIMIT :limit
    """), {"version": VERSION, "limit": max(10, min(int(limit), 500))})).mappings().all()
    updated = 0
    for raw in rows:
        row = dict(raw)
        metadata = _d(row.get("metadata"))
        metadata.update(extract_signal_features(row.get("reason")))
        await db.execute(text("""
            UPDATE verdict_memory
            SET metadata=CAST(:metadata AS JSONB), evaluated_at=COALESCE(evaluated_at, NOW())
            WHERE id=CAST(:id AS UUID)
        """), {"id": row["id"], "metadata": json.dumps(metadata)})
        updated += 1
    await db.commit()
    return {"seen": len(rows), "updated": updated}


def classify_horizons(direction: str, outcomes: dict[str, Any]) -> dict[str, Any] | None:
    if not outcomes:
        return None
    ordered = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "6h", "24h"]
    available = [(label, _d(outcomes.get(label))) for label in ordered if isinstance(outcomes.get(label), dict)]
    if not available:
        return None

    short_labels = {"1m", "3m", "5m", "10m", "15m", "30m", "1h"}
    long_labels = {"4h", "6h", "24h"}
    short = [(l, o) for l, o in available if l in short_labels]
    long = [(l, o) for l, o in available if l in long_labels]

    max_fav = max((_f(o.get("favorable_r")) for _, o in available), default=0.0)
    max_adv = max((_f(o.get("adverse_r")) for _, o in available), default=0.0)
    short_fav = max((_f(o.get("favorable_r")) for _, o in short), default=0.0)
    short_adv = max((_f(o.get("adverse_r")) for _, o in short), default=0.0)
    long_fav = max((_f(o.get("favorable_r")) for _, o in long), default=0.0)

    explosion_horizon = None
    for label, outcome in available:
        if _f(outcome.get("favorable_r")) >= 2.0:
            explosion_horizon = label
            break

    early_positive = any(_f(o.get("directional_return_pct")) > 0 and _f(o.get("favorable_r")) >= 0.45 for _, o in short[:5])
    later_negative = any(_f(o.get("directional_return_pct")) < 0 and _f(o.get("adverse_r")) >= 0.9 for _, o in short[3:])
    early_swept = short_adv >= 0.8
    later_recovered = max(short_fav, long_fav) >= 1.5

    if short_fav >= 2.0 and short_adv < 1.0:
        label = f"EXPLOSION_{str(direction).upper()}"
        timing = "GOOD"
    elif early_swept and later_recovered and max_fav > max_adv:
        label = "SWEEP_AND_REVERSE_TO_THESIS"
        timing = "EARLY_RISKY"
    elif early_positive and later_negative:
        label = "FAKE_BREAKOUT"
        timing = "FALSE_START"
    elif short_fav < 0.8 and long_fav >= 2.0:
        label = "DELAYED_EXPLOSION"
        timing = "TOO_EARLY"
        if explosion_horizon is None:
            explosion_horizon = next((l for l, o in long if _f(o.get("favorable_r")) >= 2.0), None)
    elif max_adv >= 1.0 and max_fav < 0.8:
        label = "DIRECTION_WRONG"
        timing = "BAD"
    elif max_fav < 0.7 and max_adv < 0.7:
        label = "NO_MOVE"
        timing = "NO_EDGE"
    elif max_fav >= 1.0:
        label = "DIRECTION_CORRECT_SMALL"
        timing = "MIXED"
    else:
        label = "INCONCLUSIVE"
        timing = "MIXED"

    return {
        "label": label,
        "timing_quality": timing,
        "explosion_horizon": explosion_horizon,
        "max_favorable_r": round(max_fav, 4),
        "max_adverse_r": round(max_adv, 4),
        "short_horizon_favorable_r": round(short_fav, 4),
        "short_horizon_adverse_r": round(short_adv, 4),
        "long_horizon_favorable_r": round(long_fav, 4),
    }


async def label_explosion_outcomes(db: AsyncSession, limit: int = 250) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT id::text, direction, metadata
        FROM verdict_memory
        WHERE metadata ? 'horizon_outcomes'
          AND COALESCE(metadata->>'explosion_label_version','') <> :version
        ORDER BY observed_at ASC
        LIMIT :limit
    """), {"version": VERSION, "limit": max(10, min(int(limit), 500))})).mappings().all()
    updated = 0
    labels: defaultdict[str, int] = defaultdict(int)
    for raw in rows:
        row = dict(raw)
        metadata = _d(row.get("metadata"))
        result = classify_horizons(str(row.get("direction") or ""), _d(metadata.get("horizon_outcomes")))
        if not result:
            continue
        metadata["explosion_label_version"] = VERSION
        metadata["explosion_label"] = result["label"]
        metadata["timing_quality"] = result["timing_quality"]
        metadata["explosion_evaluation"] = result
        await db.execute(text("""
            UPDATE verdict_memory SET metadata=CAST(:metadata AS JSONB), evaluated_at=NOW()
            WHERE id=CAST(:id AS UUID)
        """), {"id": row["id"], "metadata": json.dumps(metadata)})
        labels[result["label"]] += 1
        updated += 1
    await db.commit()
    global _model_cache
    if updated:
        _model_cache = None
    return {"seen": len(rows), "updated": updated, "labels": dict(labels)}


async def load_timing_model(db: AsyncSession, *, force: bool = False) -> dict[str, Any]:
    global _model_cache
    now = time.monotonic()
    if not force and _model_cache and now - _model_cache[0] < MODEL_CACHE_SECONDS:
        return _model_cache[1]

    rows = (await db.execute(text("""
        SELECT
          CASE
            WHEN COALESCE((metadata->>'ignition_score')::numeric,0) >= 88 THEN '88+'
            WHEN COALESCE((metadata->>'ignition_score')::numeric,0) >= 82 THEN '82-87'
            WHEN COALESCE((metadata->>'ignition_score')::numeric,0) >= 76 THEN '76-81'
            WHEN COALESCE((metadata->>'ignition_score')::numeric,0) >= 70 THEN '70-75'
            ELSE '<70'
          END AS bucket,
          COUNT(*) AS sample,
          COUNT(*) FILTER (WHERE metadata->>'explosion_label' IN ('EXPLOSION_LONG','EXPLOSION_SHORT')) AS fast_explosions,
          COUNT(*) FILTER (WHERE metadata->>'explosion_label'='DELAYED_EXPLOSION') AS delayed_explosions,
          COUNT(*) FILTER (WHERE metadata->>'explosion_label'='FAKE_BREAKOUT') AS fake_breakouts,
          COUNT(*) FILTER (WHERE metadata->>'explosion_label'='DIRECTION_WRONG') AS direction_wrong,
          AVG((metadata->'explosion_evaluation'->>'short_horizon_favorable_r')::numeric) AS avg_short_favorable_r,
          AVG((metadata->'explosion_evaluation'->>'short_horizon_adverse_r')::numeric) AS avg_short_adverse_r
        FROM verdict_memory
        WHERE metadata ? 'explosion_label'
          AND metadata ? 'ignition_score'
        GROUP BY bucket
    """))).mappings().all()

    buckets: dict[str, Any] = {}
    total = 0
    total_fast = 0
    for raw in rows:
        row = dict(raw)
        sample = int(row.get("sample") or 0)
        fast = int(row.get("fast_explosions") or 0)
        fake = int(row.get("fake_breakouts") or 0)
        wrong = int(row.get("direction_wrong") or 0)
        total += sample
        total_fast += fast
        buckets[str(row["bucket"])] = {
            "sample": sample,
            "sample_status": "USABLE" if sample >= 30 else "CALIBRATING",
            "fast_explosion_rate_pct": round(fast / sample * 100.0, 2) if sample else None,
            "delayed_explosion_rate_pct": round(int(row.get("delayed_explosions") or 0) / sample * 100.0, 2) if sample else None,
            "fake_breakout_rate_pct": round(fake / sample * 100.0, 2) if sample else None,
            "direction_wrong_rate_pct": round(wrong / sample * 100.0, 2) if sample else None,
            "avg_short_favorable_r": round(_f(row.get("avg_short_favorable_r")), 4),
            "avg_short_adverse_r": round(_f(row.get("avg_short_adverse_r")), 4),
        }

    model = {
        "version": VERSION,
        "sample": total,
        "global_fast_explosion_rate_pct": round(total_fast / total * 100.0, 2) if total else None,
        "buckets": buckets,
        "rule": "Historical timing can adjust confidence only after >=30 labeled samples in the matching ignition bucket.",
    }
    _model_cache = (now, model)
    return model


def timing_memory_adjustment(ignition: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    raw_score = _f(ignition.get("score"))
    bucket_name = _bucket(raw_score)
    bucket = _d(_d(model).get("buckets", {}).get(bucket_name))
    sample = int(bucket.get("sample") or 0)
    if sample < 30:
        return {
            "bucket": bucket_name,
            "sample": sample,
            "status": "CALIBRATING",
            "adjustment": 0.0,
            "adjusted_score": round(raw_score, 1),
            "can_influence_entry": False,
        }

    fast = _f(bucket.get("fast_explosion_rate_pct"))
    fake = _f(bucket.get("fake_breakout_rate_pct"))
    wrong = _f(bucket.get("direction_wrong_rate_pct"))
    favorable = _f(bucket.get("avg_short_favorable_r"))
    adverse = _f(bucket.get("avg_short_adverse_r"))

    adjustment = 0.0
    if fast >= 55 and favorable > adverse:
        adjustment += 3.0
    elif fast <= 30:
        adjustment -= 3.0
    if fake >= 25:
        adjustment -= 2.0
    if wrong >= 25:
        adjustment -= 2.0
    adjustment = max(-5.0, min(5.0, adjustment))
    return {
        "bucket": bucket_name,
        "sample": sample,
        "status": "USABLE",
        "adjustment": adjustment,
        "adjusted_score": round(max(0.0, min(100.0, raw_score + adjustment)), 1),
        "can_influence_entry": True,
        "fast_explosion_rate_pct": fast,
        "fake_breakout_rate_pct": fake,
        "direction_wrong_rate_pct": wrong,
    }
