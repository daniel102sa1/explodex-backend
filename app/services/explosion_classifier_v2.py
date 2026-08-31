from __future__ import annotations

from typing import Any

VERSION = "explosion_classifier_v2"


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def classify_horizons(direction: str, outcomes: dict[str, Any]) -> dict[str, Any] | None:
    if not outcomes:
        return None
    ordered = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "6h", "24h"]
    available = [(label, _d(outcomes.get(label))) for label in ordered if isinstance(outcomes.get(label), dict)]
    if not available:
        return None

    short_labels = {"1m", "3m", "5m", "10m", "15m", "30m", "1h"}
    long_labels = {"4h", "6h", "24h"}
    short = [(label, outcome) for label, outcome in available if label in short_labels]
    long = [(label, outcome) for label, outcome in available if label in long_labels]

    max_fav = max((_f(o.get("favorable_r")) for _, o in available), default=0.0)
    max_adv = max((_f(o.get("adverse_r")) for _, o in available), default=0.0)
    short_fav = max((_f(o.get("favorable_r")) for _, o in short), default=0.0)
    short_adv = max((_f(o.get("adverse_r")) for _, o in short), default=0.0)
    long_fav = max((_f(o.get("favorable_r")) for _, o in long), default=0.0)
    long_adv = max((_f(o.get("adverse_r")) for _, o in long), default=0.0)

    explosion_horizon = next(
        (label for label, outcome in available if _f(outcome.get("favorable_r")) >= 2.0),
        None,
    )

    early_positive = any(
        _f(o.get("directional_return_pct")) > 0 and _f(o.get("favorable_r")) >= 0.45
        for _, o in short[:5]
    )
    later_negative = any(
        _f(o.get("directional_return_pct")) < 0 and _f(o.get("adverse_r")) >= 0.9
        for _, o in short[3:]
    )

    # Priority matters: if the thesis never generated meaningful favorable
    # excursion within 1h but finally expanded at 4h/6h/24h, that is a delayed
    # directional call, not a good entry and not automatically a liquidity sweep.
    delayed = short_fav < 0.8 and long_fav >= 2.0

    # A sweep/reversal needs a meaningful early adverse excursion *and* a
    # meaningful recovery that starts within the timing window. This avoids
    # calling a six-hour delayed move a sweep just because MAE briefly touched .8R.
    recovered_within_short_window = short_fav >= 1.0
    sweep_reversal = short_adv >= 0.8 and recovered_within_short_window and max_fav > max_adv

    if short_fav >= 2.0 and short_adv < 1.0:
        label = f"EXPLOSION_{str(direction).upper()}"
        timing = "GOOD"
    elif delayed:
        label = "DELAYED_EXPLOSION"
        timing = "TOO_EARLY"
        explosion_horizon = next(
            (label for label, outcome in long if _f(outcome.get("favorable_r")) >= 2.0),
            explosion_horizon,
        )
    elif sweep_reversal:
        label = "SWEEP_AND_REVERSE_TO_THESIS"
        timing = "EARLY_RISKY"
    elif early_positive and later_negative:
        label = "FAKE_BREAKOUT"
        timing = "FALSE_START"
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
        "version": VERSION,
        "label": label,
        "timing_quality": timing,
        "explosion_horizon": explosion_horizon,
        "max_favorable_r": round(max_fav, 4),
        "max_adverse_r": round(max_adv, 4),
        "short_horizon_favorable_r": round(short_fav, 4),
        "short_horizon_adverse_r": round(short_adv, 4),
        "long_horizon_favorable_r": round(long_fav, 4),
        "long_horizon_adverse_r": round(long_adv, 4),
    }
