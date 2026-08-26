from __future__ import annotations

from typing import Any

from app.services.context_engine import apply_context_engine
from app.services.prediction_engine import build_pre_move_prediction as build_raw_pre_move_prediction
from app.services.prediction_safety import apply_prediction_safety
from app.services.sequential_context import apply_sequential_context


def build_pre_move_prediction(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = build_raw_pre_move_prediction(scored, snapshot, coinglass)
    safe = apply_prediction_safety(scored, raw)
    contextual = apply_context_engine(scored, snapshot, coinglass, safe)
    return apply_sequential_context(scored, snapshot, contextual)
