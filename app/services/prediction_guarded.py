from __future__ import annotations

from typing import Any

from app.services.prediction_engine import build_pre_move_prediction as build_raw_pre_move_prediction
from app.services.prediction_safety import apply_prediction_safety


def build_pre_move_prediction(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = build_raw_pre_move_prediction(scored, snapshot, coinglass)
    return apply_prediction_safety(scored, raw)
