from __future__ import annotations

from typing import Any

from app.services.confidence_progression import observe_confidence_progression
from app.services.context_engine import apply_context_engine
from app.services.entry_zone_engine import build_entry_zone_engine
from app.services.exchange_lead_lag import apply_exchange_lead_lag
from app.services.liquidation_cascade import apply_liquidation_cascade
from app.services.prediction_engine import build_pre_move_prediction as build_raw_pre_move_prediction
from app.services.prediction_safety import apply_prediction_safety
from app.services.sequential_context import apply_sequential_context
from app.services.server_verdict_fusion import build_server_verdict_fusion


def build_pre_move_prediction(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = build_raw_pre_move_prediction(scored, snapshot, coinglass)
    safe = apply_prediction_safety(scored, raw)
    contextual = apply_context_engine(scored, snapshot, coinglass, safe)
    sequential = apply_sequential_context(scored, snapshot, contextual)
    cascade = apply_liquidation_cascade(scored, coinglass, sequential)
    final = apply_exchange_lead_lag(coinglass, cascade)
    result = dict(final)
    result["verdict_fusion"] = build_server_verdict_fusion(scored, snapshot, result)
    result["entry_zone_engine"] = build_entry_zone_engine(scored, result)
    symbol = str(snapshot.get("symbol") or scored.get("symbol") or "UNKNOWN")
    result["confidence_progression"] = observe_confidence_progression(symbol, result)
    return result
