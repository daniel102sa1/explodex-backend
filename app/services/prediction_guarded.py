from __future__ import annotations

from typing import Any

from app.services.confidence_progression import observe_confidence_progression
from app.services.context_engine import apply_context_engine
from app.services.entry_zone_engine import build_entry_zone_engine
from app.services.exchange_lead_lag import apply_exchange_lead_lag
from app.services.forced_path_forecast import build_forced_path_forecast
from app.services.liquidation_cascade import apply_liquidation_cascade
from app.services.premove_fingerprint import build_premove_fingerprint
from app.services.prediction_engine import build_pre_move_prediction as build_raw_pre_move_prediction
from app.services.prediction_safety import apply_prediction_safety
from app.services.prediction_stack_v5 import build_prediction_stack_v5
from app.services.sequential_context import apply_sequential_context
from app.services.verdict_entry_zone_guard import build_guarded_verdict_fusion


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

    entry_zone = build_entry_zone_engine(scored, result, snapshot)
    result["entry_zone_engine"] = entry_zone
    result["verdict_fusion"] = build_guarded_verdict_fusion(scored, snapshot, result, entry_zone)
    result["path_forecast"] = build_forced_path_forecast(scored, snapshot, result)
    result["premove_fingerprint"] = build_premove_fingerprint(scored, snapshot, result)
    result["prediction_stack_v5"] = build_prediction_stack_v5(scored, snapshot, result, coinglass)

    symbol = str(snapshot.get("symbol") or scored.get("symbol") or "UNKNOWN")
    result["confidence_progression"] = observe_confidence_progression(symbol, result)
    return result
