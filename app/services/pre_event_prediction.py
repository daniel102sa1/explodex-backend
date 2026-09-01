from __future__ import annotations

from typing import Any

VERSION = "pre_event_prediction_v1"

PRE_EVENTS = {
    "NONE",
    "PRE_SHORT_SQUEEZE",
    "PRE_LONG_SQUEEZE",
    "PRE_UPSIDE_BREAKOUT",
    "PRE_DOWNSIDE_BREAKDOWN",
    "PRE_VOLATILITY_EXPANSION",
}


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def build_pre_event_prediction(*, reason: dict[str, Any], score: dict[str, Any], event_risk: dict[str, Any] | None = None) -> dict[str, Any]:
    """Detect precursor combinations before an abnormal move is fully active.

    This engine is intentionally probabilistic-looking without claiming calibrated
    probability. It produces a preparation score and explicit missing triggers.
    It never creates a real-money entry and never changes canonical direction.
    """
    prediction = _d(reason.get("prediction"))
    metrics = _d(reason.get("metrics")) or _d(score.get("metrics"))
    micro = _d(reason.get("microstructure"))
    cg = _d(reason.get("coinglass")) or _d(reason.get("coinglass_confirmation"))
    sequence = _d(prediction.get("sequence"))
    event = _d(event_risk)

    ch5 = _f(metrics.get("change_5m_pct"))
    ch15 = _f(metrics.get("change_15m_pct"))
    ch1h = _f(metrics.get("change_1h_pct"))
    atr = max(0.25, _f(metrics.get("atr_pct"), 0.8))
    rv = max(0.0, _f(metrics.get("relative_volume"), _f(metrics.get("volume_ratio"), 1.0)))
    compression = _f(metrics.get("compression_score"), _f(metrics.get("compression")))
    oi5 = _f(metrics.get("oi_change_pct"), _f(cg.get("oi_change_5m")))
    oi15 = _f(cg.get("oi_change_15m"))
    funding = _f(metrics.get("funding_rate"), _f(cg.get("funding_median")))
    futures_delta = _f(metrics.get("futures_delta_ratio"), _f(micro.get("futures_delta_ratio")))
    spot_delta = _f(metrics.get("spot_delta_ratio"), _f(micro.get("spot_delta_ratio")))
    book = _f(metrics.get("order_book_imbalance"), _f(micro.get("order_book_imbalance")))
    taker = _f(cg.get("taker_buy_sell_ratio"), 1.0)
    liq_imb = _f(cg.get("liquidation_imbalance"))
    chase = bool(sequence.get("chase_risk"))

    scores = {name: 0.0 for name in PRE_EVENTS}
    evidence: dict[str, list[str]] = {name: [] for name in PRE_EVENTS}
    missing: dict[str, list[str]] = {name: [] for name in PRE_EVENTS}

    # PRE_SHORT_SQUEEZE: shorts crowded, price refuses to fall, buyers/spot begin absorbing.
    if funding <= -0.0002:
        scores["PRE_SHORT_SQUEEZE"] += 14; evidence["PRE_SHORT_SQUEEZE"].append("negative_funding")
    else:
        missing["PRE_SHORT_SQUEEZE"].append("funding_not_negative")
    if oi5 >= 0.30 or oi15 >= 0.60:
        scores["PRE_SHORT_SQUEEZE"] += 14; evidence["PRE_SHORT_SQUEEZE"].append("oi_building")
    else:
        missing["PRE_SHORT_SQUEEZE"].append("oi_not_building")
    if ch15 > -atr * 0.35 and ch5 >= -atr * 0.20:
        scores["PRE_SHORT_SQUEEZE"] += 12; evidence["PRE_SHORT_SQUEEZE"].append("price_resists_downside")
    if spot_delta >= 0.05:
        scores["PRE_SHORT_SQUEEZE"] += 16; evidence["PRE_SHORT_SQUEEZE"].append("spot_buying")
    if futures_delta >= 0.04 or taker >= 1.05:
        scores["PRE_SHORT_SQUEEZE"] += 12; evidence["PRE_SHORT_SQUEEZE"].append("taker_turning_up")
    if book >= 0.06:
        scores["PRE_SHORT_SQUEEZE"] += 10; evidence["PRE_SHORT_SQUEEZE"].append("bid_imbalance")
    if liq_imb >= 0.05:
        scores["PRE_SHORT_SQUEEZE"] += 6; evidence["PRE_SHORT_SQUEEZE"].append("short_liq_pressure_starting")

    # PRE_LONG_SQUEEZE: longs crowded, price refuses to advance, sellers start winning.
    if funding >= 0.0002:
        scores["PRE_LONG_SQUEEZE"] += 14; evidence["PRE_LONG_SQUEEZE"].append("positive_funding")
    else:
        missing["PRE_LONG_SQUEEZE"].append("funding_not_positive")
    if oi5 >= 0.30 or oi15 >= 0.60:
        scores["PRE_LONG_SQUEEZE"] += 14; evidence["PRE_LONG_SQUEEZE"].append("oi_building")
    else:
        missing["PRE_LONG_SQUEEZE"].append("oi_not_building")
    if ch15 < atr * 0.35 and ch5 <= atr * 0.20:
        scores["PRE_LONG_SQUEEZE"] += 12; evidence["PRE_LONG_SQUEEZE"].append("price_resists_upside")
    if spot_delta <= -0.05:
        scores["PRE_LONG_SQUEEZE"] += 16; evidence["PRE_LONG_SQUEEZE"].append("spot_selling")
    if futures_delta <= -0.04 or taker <= 0.95:
        scores["PRE_LONG_SQUEEZE"] += 12; evidence["PRE_LONG_SQUEEZE"].append("taker_turning_down")
    if book <= -0.06:
        scores["PRE_LONG_SQUEEZE"] += 10; evidence["PRE_LONG_SQUEEZE"].append("ask_imbalance")
    if liq_imb <= -0.05:
        scores["PRE_LONG_SQUEEZE"] += 6; evidence["PRE_LONG_SQUEEZE"].append("long_liq_pressure_starting")

    # Generic expansion precursor: compression + OI + volume wake-up before displacement.
    if compression >= 60:
        scores["PRE_VOLATILITY_EXPANSION"] += 22; evidence["PRE_VOLATILITY_EXPANSION"].append("compression_high")
    if oi5 >= 0.25 or oi15 >= 0.50:
        scores["PRE_VOLATILITY_EXPANSION"] += 18; evidence["PRE_VOLATILITY_EXPANSION"].append("oi_building")
    if 1.15 <= rv <= 2.50:
        scores["PRE_VOLATILITY_EXPANSION"] += 14; evidence["PRE_VOLATILITY_EXPANSION"].append("volume_waking_up")
    if abs(ch15) <= atr * 0.75:
        scores["PRE_VOLATILITY_EXPANSION"] += 10; evidence["PRE_VOLATILITY_EXPANSION"].append("move_not_expanded_yet")

    # Directional breakout/breakdown preconditions from flow balance.
    upside_flow = 0
    downside_flow = 0
    if futures_delta >= 0.06: upside_flow += 1
    if spot_delta >= 0.05: upside_flow += 1
    if book >= 0.06: upside_flow += 1
    if taker >= 1.05: upside_flow += 1
    if futures_delta <= -0.06: downside_flow += 1
    if spot_delta <= -0.05: downside_flow += 1
    if book <= -0.06: downside_flow += 1
    if taker <= 0.95: downside_flow += 1

    if upside_flow >= 2:
        scores["PRE_UPSIDE_BREAKOUT"] += 18 + (upside_flow - 2) * 8
        evidence["PRE_UPSIDE_BREAKOUT"].append(f"upside_flow_components={upside_flow}")
    if oi5 >= 0.25 or oi15 >= 0.50:
        scores["PRE_UPSIDE_BREAKOUT"] += 16; evidence["PRE_UPSIDE_BREAKOUT"].append("oi_support")
    if compression >= 50:
        scores["PRE_UPSIDE_BREAKOUT"] += 12; evidence["PRE_UPSIDE_BREAKOUT"].append("compressed_structure")
    if ch5 >= 0 and ch15 < atr * 1.2:
        scores["PRE_UPSIDE_BREAKOUT"] += 10; evidence["PRE_UPSIDE_BREAKOUT"].append("not_extended_yet")

    if downside_flow >= 2:
        scores["PRE_DOWNSIDE_BREAKDOWN"] += 18 + (downside_flow - 2) * 8
        evidence["PRE_DOWNSIDE_BREAKDOWN"].append(f"downside_flow_components={downside_flow}")
    if oi5 >= 0.25 or oi15 >= 0.50:
        scores["PRE_DOWNSIDE_BREAKDOWN"] += 16; evidence["PRE_DOWNSIDE_BREAKDOWN"].append("oi_support")
    if compression >= 50:
        scores["PRE_DOWNSIDE_BREAKDOWN"] += 12; evidence["PRE_DOWNSIDE_BREAKDOWN"].append("compressed_structure")
    if ch5 <= 0 and ch15 > -atr * 1.2:
        scores["PRE_DOWNSIDE_BREAKDOWN"] += 10; evidence["PRE_DOWNSIDE_BREAKDOWN"].append("not_extended_yet")

    # If the event is already active, precursor status is no longer the primary label.
    active_event = str(event.get("event_type") or "NORMAL")
    active_severity = str(event.get("severity") or "NORMAL")
    if active_event != "NORMAL" and active_severity in {"HIGH", "CRITICAL"}:
        scores = {k: (0.0 if k != "NONE" else 70.0) for k in scores}
        evidence["NONE"] = ["event_already_active"]

    non_none = [(k, _clip(v)) for k, v in scores.items() if k != "NONE"]
    event_type, preparation = max(non_none, key=lambda item: item[1]) if non_none else ("NONE", 0.0)
    if preparation < 48:
        event_type = "NONE"
        preparation = max(0.0, preparation)
        scores["NONE"] = 70.0

    direction = "NEUTRAL"
    if event_type in {"PRE_SHORT_SQUEEZE", "PRE_UPSIDE_BREAKOUT"}:
        direction = "LONG"
    elif event_type in {"PRE_LONG_SQUEEZE", "PRE_DOWNSIDE_BREAKDOWN"}:
        direction = "SHORT"
    elif event_type == "PRE_VOLATILITY_EXPANSION":
        if upside_flow > downside_flow:
            direction = "LONG"
        elif downside_flow > upside_flow:
            direction = "SHORT"

    if preparation >= 78:
        phase = "TRIGGERING"
    elif preparation >= 62:
        phase = "PRE_EVENT_STRONG"
    elif preparation >= 48:
        phase = "PRE_EVENT"
    else:
        phase = "NONE"

    supporting = len(evidence.get(event_type, []))
    # PAPER-only early lane requires multiple independent precursors and no chase.
    paper_candidate = (
        event_type != "NONE"
        and direction in {"LONG", "SHORT"}
        and preparation >= 68
        and supporting >= 4
        and not chase
        and not bool(event.get("block_new_entries"))
    )

    return {
        "version": VERSION,
        "pre_event_type": event_type,
        "phase": phase,
        "preparation_score": round(preparation, 1),
        "score_is_probability": False,
        "direction": direction,
        "supporting_signals": supporting,
        "evidence": evidence.get(event_type, []),
        "missing": missing.get(event_type, []),
        "paper_candidate": paper_candidate,
        "suggested_horizon": "15m-4h" if event_type in {"PRE_SHORT_SQUEEZE", "PRE_LONG_SQUEEZE"} else "15m-6h",
        "event_already_active": active_event != "NORMAL" and active_severity in {"HIGH", "CRITICAL"},
        "creates_real_entry": False,
        "changes_direction": False,
        "rule": "Pre-event is precursor evidence. It may authorize tiny PAPER research only when the unified Heart safety rules are clear; it never places or recommends a real-money trade by itself.",
    }
