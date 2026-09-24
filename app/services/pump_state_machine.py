from __future__ import annotations

from typing import Any

VERSION = "pump_state_machine_v1_shadow"


def _d(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def classify_pump_state(
    *,
    score: dict[str, Any],
    prediction: dict[str, Any] | None = None,
    fundamental: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shadow state machine for abnormal altcoin moves.

    States are descriptive, not standalone trading instructions:
    NORMAL -> COMPRESSION -> PRE_IGNITION -> IGNITION ->
    CONTINUATION / DERIVATIVE_SQUEEZE -> EXHAUSTION ->
    REVERSAL_CONFIRMED.
    """
    prediction = _d(prediction)
    fundamental = _d(fundamental)
    metrics = _d(score.get("metrics"))
    pro = _d(prediction.get("professional_arsenal"))
    flow = _d(pro.get("flow"))
    fcvd = _d(flow.get("futures_cvd_proxy"))
    scvd = _d(flow.get("spot_cvd_proxy"))
    derivatives = _d(pro.get("derivatives"))
    absorption = _d(pro.get("absorption_exhaustion"))
    patterns = _d(pro.get("patterns"))
    pattern_rows = patterns.get("patterns") if isinstance(patterns.get("patterns"), list) else []

    ch5 = _f(metrics.get("change_5m_pct"))
    ch15 = _f(metrics.get("change_15m_pct"))
    ch1h = _f(metrics.get("change_1h_pct"))
    atr = max(0.25, _f(metrics.get("atr_pct"), 0.8))
    rvol = max(0.0, _f(metrics.get("relative_volume"), 1.0))
    vacc = max(0.0, _f(metrics.get("volume_acceleration"), 1.0))
    oi = _f(metrics.get("oi_change_pct"))
    funding = _f(metrics.get("funding_rate"))
    book = _f(metrics.get("order_book_imbalance"))
    spread = _f(metrics.get("order_book_spread_bps"))
    fdelta = _f(fcvd.get("normalized_delta"), _f(metrics.get("futures_delta_ratio")))
    sdelta = _f(scvd.get("normalized_delta"), _f(metrics.get("spot_delta_ratio")))
    compression = _f(metrics.get("compression_ratio"), 1.0)
    exhaustion = str(absorption.get("exhaustion") or "NONE")
    squeeze = str(derivatives.get("liquidation_squeeze") or "NONE")
    crowding = str(derivatives.get("crowding") or "NONE")

    pattern_names = {str(item.get("name") or "") for item in pattern_rows if isinstance(item, dict)}
    failed_up = "FAILED_BREAKOUT_RANGE_DEVIATION" in pattern_names
    failed_down = "FAILED_BREAKDOWN_RANGE_DEVIATION" in pattern_names

    scores = {
        "NORMAL": 25.0,
        "COMPRESSION": 0.0,
        "PRE_IGNITION": 0.0,
        "IGNITION": 0.0,
        "CONTINUATION": 0.0,
        "DERIVATIVE_SQUEEZE": 0.0,
        "EXHAUSTION": 0.0,
        "REVERSAL_CONFIRMED": 0.0,
    }
    evidence: dict[str, list[str]] = {key: [] for key in scores}

    def add(state: str, points: float, reason: str) -> None:
        scores[state] += points
        evidence[state].append(reason)

    if compression <= 0.65:
        add("COMPRESSION", 34, "range_compression")
    if rvol <= 1.25 and abs(ch15) <= atr:
        add("COMPRESSION", 12, "quiet_volume_and_price")

    anomaly_count = 0
    if rvol >= 1.5:
        anomaly_count += 1
        add("PRE_IGNITION", 14, "relative_volume_rising")
    if vacc >= 1.35:
        anomaly_count += 1
        add("PRE_IGNITION", 14, "volume_acceleration")
    if abs(oi) >= 0.35:
        anomaly_count += 1
        add("PRE_IGNITION", 12, "open_interest_moving")
    if abs(book) >= 0.08:
        anomaly_count += 1
        add("PRE_IGNITION", 10, "book_imbalance")
    if compression <= 0.75:
        add("PRE_IGNITION", 12, "compression_base")
    if anomaly_count >= 3:
        add("PRE_IGNITION", 18, "multi_domain_anomaly")

    up_ignition = ch5 >= max(0.6, atr * 0.65) and rvol >= 1.8 and (fdelta >= 0.06 or sdelta >= 0.05)
    down_ignition = ch5 <= -max(0.6, atr * 0.65) and rvol >= 1.8 and (fdelta <= -0.06 or sdelta <= -0.05)
    if up_ignition:
        add("IGNITION", 55, "upside_break_with_flow")
    if down_ignition:
        add("IGNITION", 55, "downside_break_with_flow")

    up_cont = ch15 > 0 and sdelta >= 0.05 and fdelta >= 0.03 and book >= 0.03
    down_cont = ch15 < 0 and sdelta <= -0.05 and fdelta <= -0.03 and book <= -0.03
    if up_cont:
        add("CONTINUATION", 62, "spot_perp_book_align_up")
    if down_cont:
        add("CONTINUATION", 62, "spot_perp_book_align_down")
    if rvol >= 2.0 and vacc >= 1.2:
        add("CONTINUATION", 10, "volume_supports_continuation")
    if abs(oi) >= 0.25 and abs(funding) < 0.001:
        add("CONTINUATION", 8, "oi_without_extreme_funding")

    if squeeze in {"SHORT_SQUEEZE_PRESSURE", "LONG_SQUEEZE_PRESSURE"}:
        add("DERIVATIVE_SQUEEZE", 58, squeeze.lower())
    if abs(oi) >= 0.8 and abs(fdelta) >= 0.10:
        add("DERIVATIVE_SQUEEZE", 15, "leveraged_flow_expansion")
    if crowding != "NONE":
        add("DERIVATIVE_SQUEEZE", 12, crowding.lower())

    if exhaustion in {"UPSIDE_EXHAUSTION", "DOWNSIDE_EXHAUSTION"}:
        add("EXHAUSTION", 58, exhaustion.lower())
    if ch15 > max(1.5, atr * 1.5) and sdelta <= 0.0 and fdelta > 0:
        add("EXHAUSTION", 18, "up_move_perp_led_spot_weak")
    if ch15 < -max(1.5, atr * 1.5) and sdelta >= 0.0 and fdelta < 0:
        add("EXHAUSTION", 18, "down_move_perp_led_spot_weak")
    if spread >= 10:
        add("EXHAUSTION", 7, "spread_deterioration")

    upside_reversal = (
        failed_up
        and ch5 < -max(0.35, atr * 0.35)
        and (sdelta <= -0.03 or book <= -0.05)
    )
    downside_reversal = (
        failed_down
        and ch5 > max(0.35, atr * 0.35)
        and (sdelta >= 0.03 or book >= 0.05)
    )
    if upside_reversal:
        add("REVERSAL_CONFIRMED", 76, "failed_breakout_plus_flow_reversal")
    if downside_reversal:
        add("REVERSAL_CONFIRMED", 76, "failed_breakdown_plus_flow_reversal")

    # Tokenomics risk never creates the state, but it raises caution around pumps
    # where low float/high dilution can amplify both continuation and reversal.
    fund_risk = _d(fundamental.get("risk"))
    if _f(fund_risk.get("risk_score")) >= 60:
        add("PRE_IGNITION", 5, "high_tokenomics_manipulability_context")
        add("EXHAUSTION", 5, "high_tokenomics_distribution_risk")

    state, raw = max(scores.items(), key=lambda item: item[1])
    if raw < 35:
        state = "NORMAL"

    dominant_direction = "NEUTRAL"
    if state in {"IGNITION", "CONTINUATION", "DERIVATIVE_SQUEEZE"}:
        if ch15 > 0 or (ch15 == 0 and ch5 > 0):
            dominant_direction = "LONG"
        elif ch15 < 0 or (ch15 == 0 and ch5 < 0):
            dominant_direction = "SHORT"
    elif state == "EXHAUSTION":
        dominant_direction = "SHORT_BIAS" if ch15 > 0 else "LONG_BIAS" if ch15 < 0 else "NEUTRAL"
    elif state == "REVERSAL_CONFIRMED":
        dominant_direction = "SHORT" if upside_reversal else "LONG" if downside_reversal else "NEUTRAL"

    next_confirmation = {
        "COMPRESSION": "Wait for multi-domain anomalies: RVOL/OI/book/flow.",
        "PRE_IGNITION": "Require breakout/impulse plus aggressive flow; do not pre-chase.",
        "IGNITION": "Require spot participation and structure acceptance/retest.",
        "CONTINUATION": "Monitor spot support, funding crowding and structure.",
        "DERIVATIVE_SQUEEZE": "Do not fade solely because move is large; wait for exhaustion and structure failure.",
        "EXHAUSTION": "Require structure break + failed retest before treating as reversal.",
        "REVERSAL_CONFIRMED": "Use structural invalidation and liquidity-aware execution; no automatic leverage increase.",
        "NORMAL": "No abnormal pump/dump state identified.",
    }.get(state, "Observe.")

    return {
        "version": VERSION,
        "state": state,
        "state_score": round(_clip(raw), 1),
        "dominant_direction": dominant_direction,
        "evidence": evidence.get(state, []),
        "all_scores": {key: round(_clip(value), 1) for key, value in scores.items()},
        "next_required_confirmation": next_confirmation,
        "paper_only": True,
        "shadow_only": True,
        "validated_out_of_sample": False,
        "can_create_entry": False,
        "can_change_direction": False,
        "can_raise_leverage": False,
        "note": "Descriptive pump-state classifier. Thresholds are research hypotheses until validated out of sample.",
    }
