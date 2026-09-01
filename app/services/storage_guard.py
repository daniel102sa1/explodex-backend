from __future__ import annotations

import json as _json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "storage_guard_v1"
SNAPSHOT_RETENTION_HOURS = 24
DELETE_BATCH = 2500
MAX_DELETE_BATCHES_PER_SCAN = 4


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _compact_score(score: Any) -> dict[str, Any]:
    src = _d(score)
    metrics = _d(src.get("metrics"))
    keep_metrics = {
        k: metrics.get(k)
        for k in (
            "oi_change_pct", "taker_avg_3", "funding_rate", "relative_volume",
            "atr_pct", "btc_trend", "change_5m_pct", "change_15m_pct",
            "change_1h_pct", "futures_delta_ratio", "spot_delta_ratio",
            "order_book_imbalance", "pre_move_type", "pre_move_phase",
            "pre_move_score", "pre_move_trigger", "pre_move_direction_match",
        )
        if k in metrics
    }
    return {
        "direction": src.get("direction"),
        "state": src.get("state"),
        "setup_score": src.get("setup_score"),
        "risk_score": src.get("risk_score"),
        "confidence_pct": src.get("confidence_pct"),
        "current_price": src.get("current_price"),
        "entry_low": src.get("entry_low"),
        "entry_high": src.get("entry_high"),
        "stop_loss": src.get("stop_loss"),
        "tp1": src.get("tp1"),
        "tp2": src.get("tp2"),
        "tp3": src.get("tp3"),
        "metrics": keep_metrics,
        "components": _d(src.get("components")),
    }


def _compact_prediction(prediction: Any) -> dict[str, Any]:
    src = _d(prediction)
    sequence = _d(src.get("sequence"))
    return {
        "type": src.get("type"),
        "direction": src.get("direction"),
        "phase": src.get("phase"),
        "preactivation_score": src.get("preactivation_score"),
        "trigger_price": src.get("trigger_price"),
        "entry_low": src.get("entry_low"),
        "entry_high": src.get("entry_high"),
        "invalidation_price": src.get("invalidation_price"),
        "stop_loss": src.get("stop_loss"),
        "tp1": src.get("tp1"),
        "tp2": src.get("tp2"),
        "tp3": src.get("tp3"),
        "sequence": {
            k: sequence.get(k)
            for k in (
                "chase_risk", "risk_guard_pass", "risk_guard_blocks",
                "market_regime", "regime_directional_bias", "regime_confidence",
                "early_context_score", "microstructure_score",
            )
            if k in sequence
        },
    }


def compact_market_snapshot_bundle(value: Any) -> Any:
    """Compact only the scanner's duplicated market_snapshots.raw_data bundle.

    The canonical signal reason/Heart memory stays untouched in signals and the
    dedicated learning tables. This avoids storing the same large nested objects
    multiple times every scanner cycle.
    """
    if not isinstance(value, dict):
        return value
    marker_keys = {"score", "local_score_before_coinglass", "ticker", "btc_context", "market_data_source", "coinglass", "prediction"}
    if not marker_keys.issubset(set(value.keys())):
        return value
    ticker = _d(value.get("ticker"))
    coinglass = _d(value.get("coinglass"))
    return {
        "storage_version": VERSION,
        "score": _compact_score(value.get("score")),
        "local_score_before_coinglass": _compact_score(value.get("local_score_before_coinglass")),
        "ticker": {
            "symbol": ticker.get("symbol"),
            "priceChangePercent": ticker.get("priceChangePercent"),
            "quoteVolume": ticker.get("quoteVolume"),
        },
        "btc_context": _d(value.get("btc_context")),
        "market_data_source": value.get("market_data_source"),
        "coinglass_summary": {
            "available": coinglass.get("available"),
            "status": coinglass.get("status"),
            "configured": coinglass.get("configured"),
        },
        "prediction": _compact_prediction(value.get("prediction")),
        "canonical_details_live_in": "signals.reason + dedicated Heart/learning tables",
    }


class ScannerJsonProxy:
    """Module-like json proxy used only by scanner.py."""

    def dumps(self, obj: Any, *args: Any, **kwargs: Any) -> str:
        return _json.dumps(compact_market_snapshot_bundle(obj), *args, **kwargs)

    def loads(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        return _json.loads(value, *args, **kwargs)


SCANNER_JSON_PROXY = ScannerJsonProxy()


async def prune_market_snapshots(db: AsyncSession) -> dict[str, Any]:
    """Bounded cleanup so one scan never spends unbounded time deleting rows."""
    deleted = 0
    batches = 0
    for _ in range(MAX_DELETE_BATCHES_PER_SCAN):
        result = await db.execute(text("""
            WITH doomed AS (
                SELECT ctid
                FROM market_snapshots
                WHERE captured_at < NOW() - (:hours * INTERVAL '1 hour')
                ORDER BY captured_at ASC
                LIMIT :batch
            )
            DELETE FROM market_snapshots m
            USING doomed d
            WHERE m.ctid = d.ctid
        """), {"hours": SNAPSHOT_RETENTION_HOURS, "batch": DELETE_BATCH})
        count = int(result.rowcount or 0)
        deleted += count
        batches += 1
        await db.commit()
        if count < DELETE_BATCH:
            break
    return {
        "version": VERSION,
        "deleted": deleted,
        "batches": batches,
        "retention_hours": SNAPSHOT_RETENTION_HOURS,
        "preserves_learning_tables": True,
    }
