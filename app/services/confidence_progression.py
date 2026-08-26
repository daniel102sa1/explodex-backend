from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

_HISTORY: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=8))


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def observe_confidence_progression(symbol: str, prediction: dict[str, Any]) -> dict[str, Any]:
    fusion = prediction.get("verdict_fusion") if isinstance(prediction.get("verdict_fusion"), dict) else {}
    score = _f(fusion.get("technical_confidence"))
    direction = str(prediction.get("direction") or "")
    phase = str(prediction.get("phase") or "")
    now = datetime.now(timezone.utc).isoformat()

    rows = _HISTORY[symbol]
    last = rows[-1] if rows else None
    # Avoid duplicating identical scanner reads while still preserving meaningful
    # score/phase changes.
    if not last or last.get("score") != round(score, 2) or last.get("phase") != phase or last.get("direction") != direction:
        rows.append({"at": now, "score": round(score, 2), "direction": direction, "phase": phase})

    scores = [float(row.get("score") or 0) for row in rows if row.get("direction") == direction]
    delta_last = scores[-1] - scores[-2] if len(scores) >= 2 else 0.0
    delta_window = scores[-1] - scores[0] if len(scores) >= 2 else 0.0

    if len(scores) < 2:
        trend = "WARMING_UP"
    elif delta_last >= 3 and delta_window >= 5:
        trend = "STRENGTHENING_FAST"
    elif delta_last > 0.5 and delta_window > 0:
        trend = "STRENGTHENING"
    elif delta_last <= -3 and delta_window <= -5:
        trend = "WEAKENING_FAST"
    elif delta_last < -0.5:
        trend = "WEAKENING"
    else:
        trend = "STABLE"

    crossed_90 = len(scores) >= 2 and scores[-2] < 90 <= scores[-1]
    sustained_90 = len(scores) >= 2 and scores[-1] >= 90 and scores[-2] >= 90

    return {
        "version": "confidence_progression_v1",
        "symbol": symbol,
        "direction": direction,
        "trend": trend,
        "samples": len(scores),
        "scores": [round(value, 2) for value in scores],
        "latest_score": round(scores[-1], 2) if scores else round(score, 2),
        "delta_last": round(delta_last, 2),
        "delta_window": round(delta_window, 2),
        "crossed_90_score": crossed_90,
        "sustained_90_score": sustained_90,
        "score_is_probability": False,
        "note": "Progression tracks technical score evolution only; 90/100 is not a guaranteed 90% win probability.",
    }
