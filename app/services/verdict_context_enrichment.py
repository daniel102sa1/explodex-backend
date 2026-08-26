from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _extract(reason: Any) -> dict[str, Any]:
    bundle = _json(reason)
    prediction = bundle.get("prediction") if isinstance(bundle.get("prediction"), dict) else {}
    context = prediction.get("context_engine") if isinstance(prediction.get("context_engine"), dict) else {}
    micro = context.get("microstructure") if isinstance(context.get("microstructure"), dict) else {}
    sequential = context.get("sequential_microstructure") if isinstance(context.get("sequential_microstructure"), dict) else {}
    cascade = context.get("liquidation_cascade") if isinstance(context.get("liquidation_cascade"), dict) else {}
    leadlag = prediction.get("exchange_lead_lag") if isinstance(prediction.get("exchange_lead_lag"), dict) else {}

    return {
        "sequential_ready": sequential.get("ready"),
        "sequential_snapshot_count": sequential.get("snapshot_count"),
        "ofi": sequential.get("ofi") if "ofi" in sequential else micro.get("ofi"),
        "replenishment": sequential.get("replenishment") if "replenishment" in sequential else micro.get("replenishment"),
        "replenishment_side": sequential.get("replenishment_side") or micro.get("replenishment_side"),
        "liquidity_speed": sequential.get("liquidity_speed") if "liquidity_speed" in sequential else micro.get("liquidity_speed"),
        "sequential_absorption": sequential.get("sequential_absorption") if "sequential_absorption" in sequential else micro.get("sequential_absorption"),
        "sequential_absorption_label": sequential.get("sequential_absorption_label") or micro.get("sequential_absorption_label"),
        "cascade_status": cascade.get("status"),
        "cascade_score": cascade.get("cascade_score"),
        "cascade_bias": cascade.get("cascade_bias"),
        "cascade_risk_to_direction": cascade.get("risk_to_direction"),
        "cascade_supports_direction": cascade.get("supports_direction"),
        "cascade_burst_ratio": cascade.get("burst_ratio_vs_4h_hourly"),
        "cascade_deleveraging": cascade.get("deleveraging"),
        "cascade_fresh_leverage": cascade.get("fresh_leverage"),
        "exchange_leadlag_status": leadlag.get("status"),
        "exchange_leader": leadlag.get("leader"),
        "exchange_leader_bias": leadlag.get("leader_bias"),
        "exchange_aggregate_bias": leadlag.get("aggregate_bias"),
        "exchange_agreement": leadlag.get("agreement"),
        "exchange_dispersion": leadlag.get("dispersion"),
        "exchange_support_direction": leadlag.get("support_direction"),
        "exchange_conflict_direction": leadlag.get("conflict_direction"),
        "advanced_context_version": "v2",
    }


async def enrich_verdict_memory_context(db: AsyncSession, limit: int = 300) -> dict[str, int]:
    """Backfill/refresh advanced decision-time context from immutable signal.reason.

    This never uses future outcome data. It only copies fields that were already
    present when the signal was created, so the learning dataset remains leakage-safe.
    """
    result = await db.execute(
        text(
            """
            SELECT vm.id::text AS id, vm.metadata, s.reason
            FROM verdict_memory vm
            JOIN signals s ON s.id = vm.signal_id
            WHERE COALESCE(vm.metadata->>'advanced_context_version','') <> 'v2'
            ORDER BY vm.observed_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    rows = result.mappings().all()
    updated = 0
    for row in rows:
        existing = dict(row.get("metadata") or {})
        existing.update(_extract(row.get("reason")))
        await db.execute(
            text(
                """
                UPDATE verdict_memory
                SET metadata = CAST(:metadata AS JSONB)
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": row["id"], "metadata": json.dumps(existing, separators=(",", ":"))},
        )
        updated += 1
    if updated:
        await db.commit()
    return {"seen": len(rows), "updated": updated}


async def advanced_context_stats(db: AsyncSession) -> dict[str, Any]:
    """Outcome cohorts for the newest contextual engines, still sample-gated."""
    result = await db.execute(
        text(
            """
            SELECT
              COALESCE(metadata->>'cascade_status','N/D') AS cascade_status,
              COALESCE(metadata->>'exchange_leadlag_status','N/D') AS exchange_status,
              CASE
                WHEN metadata->>'sequential_ready' = 'true' THEN 'READY'
                WHEN metadata->>'sequential_ready' = 'false' THEN 'WARMING_UP'
                ELSE 'N/D'
              END AS sequential_status,
              COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS decided,
              COUNT(*) FILTER (WHERE outcome='TP1_FIRST') AS wins,
              COUNT(*) FILTER (WHERE outcome='STOP_FIRST') AS losses,
              AVG(mfe_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mfe_pct,
              AVG(mae_pct) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) AS avg_mae_pct
            FROM verdict_memory
            WHERE metadata->>'advanced_context_version' = 'v2'
            GROUP BY cascade_status, exchange_status, sequential_status
            HAVING COUNT(*) FILTER (WHERE outcome IN ('TP1_FIRST','STOP_FIRST')) > 0
            ORDER BY decided DESC
            LIMIT 60
            """
        )
    )
    cohorts = []
    for source in result.mappings().all():
        row = dict(source)
        decided = int(row.get("decided") or 0)
        wins = int(row.get("wins") or 0)
        row["decided"] = decided
        row["wins"] = wins
        row["losses"] = int(row.get("losses") or 0)
        row["win_rate_pct"] = wins / decided * 100.0 if decided else None
        row["sample_status"] = "USABLE" if decided >= 30 else "CALIBRATING"
        row["can_influence_veto"] = decided >= 30
        cohorts.append(row)

    return {
        "mode": "SHADOW_LEARNING",
        "cohorts": cohorts,
        "usable_cohorts": sum(1 for row in cohorts if row["decided"] >= 30),
        "rule": "Advanced context is descriptive below 30 comparable decided outcomes and never creates an entry or raises leverage.",
    }
