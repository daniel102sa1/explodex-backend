from __future__ import annotations

from typing import Any

VERSION = "event_risk_engine_v2_pump_state_radar"


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _pump_state_radar(
    *,
    ch5: float,
    ch15: float,
    ch1h: float,
    atr: float,
    relative_volume: float,
    volume_acceleration: float,
    oi_change_pct: float,
    funding: float,
    futures_delta: float,
    spot_delta: float,
    book_imbalance: float,
) -> dict[str, Any]:
    """Experimental PAPER-only continuation/exhaustion diagnostic.

    This does not create a trade direction. It separates spot-supported expansion
    from leverage-led moves that are more vulnerable to exhaustion/reversal.
    Thresholds are deliberately interpretable so they can be calibrated later
    against ExplodeX's stored horizon outcomes.
    """
    atr = max(0.25, atr)
    up_move = ch15 >= max(1.2, atr * 1.35) or ch1h >= max(2.5, atr * 2.2)
    down_move = ch15 <= -max(1.2, atr * 1.35) or ch1h <= -max(2.5, atr * 2.2)
    perp_led_up = futures_delta >= 0.08 and spot_delta <= max(0.02, futures_delta - 0.08)
    perp_led_down = futures_delta <= -0.08 and spot_delta >= min(-0.02, futures_delta + 0.08)

    scores = {
        "UPSIDE_CONTINUATION": 0.0,
        "DOWNSIDE_CONTINUATION": 0.0,
        "UPSIDE_EXHAUSTION": 0.0,
        "DOWNSIDE_EXHAUSTION": 0.0,
    }
    evidence: dict[str, list[str]] = {key: [] for key in scores}

    def add(key: str, points: float, why: str) -> None:
        scores[key] += points
        evidence[key].append(why)

    if up_move:
        add("UPSIDE_CONTINUATION", 20, "abnormal_up_move")
        add("UPSIDE_EXHAUSTION", 20, "abnormal_up_move")
    if down_move:
        add("DOWNSIDE_CONTINUATION", 20, "abnormal_down_move")
        add("DOWNSIDE_EXHAUSTION", 20, "abnormal_down_move")
    if relative_volume >= 2.0:
        for key in scores:
            add(key, 10, "relative_volume>=2x")
    if volume_acceleration >= 1.5:
        add("UPSIDE_CONTINUATION", 8, "volume_accelerating")
        add("DOWNSIDE_CONTINUATION", 8, "volume_accelerating")
    if spot_delta >= 0.08:
        add("UPSIDE_CONTINUATION", 22, "spot_buying_confirms")
    if spot_delta <= -0.08:
        add("DOWNSIDE_CONTINUATION", 22, "spot_selling_confirms")
    if futures_delta >= 0.08:
        add("UPSIDE_CONTINUATION", 10, "futures_buying_confirms")
    if futures_delta <= -0.08:
        add("DOWNSIDE_CONTINUATION", 10, "futures_selling_confirms")
    if book_imbalance >= 0.08:
        add("UPSIDE_CONTINUATION", 10, "book_supports_upside")
    if book_imbalance <= -0.08:
        add("DOWNSIDE_CONTINUATION", 10, "book_supports_downside")
    if oi_change_pct >= 0.35:
        add("UPSIDE_CONTINUATION", 6, "new_open_interest")
        add("DOWNSIDE_CONTINUATION", 6, "new_open_interest")

    if up_move and perp_led_up:
        add("UPSIDE_EXHAUSTION", 20, "perp_led_spot_weak")
    if down_move and perp_led_down:
        add("DOWNSIDE_EXHAUSTION", 20, "perp_led_spot_weak")
    if up_move and oi_change_pct >= 0.80:
        add("UPSIDE_EXHAUSTION", 12, "oi_crowding")
    if down_move and oi_change_pct >= 0.80:
        add("DOWNSIDE_EXHAUSTION", 12, "oi_crowding")
    if up_move and funding >= 0.0005:
        add("UPSIDE_EXHAUSTION", 10, "positive_funding_crowding")
    if down_move and funding <= -0.0005:
        add("DOWNSIDE_EXHAUSTION", 10, "negative_funding_crowding")
    if up_move and book_imbalance <= 0.0:
        add("UPSIDE_EXHAUSTION", 10, "book_no_longer_supports_upside")
    if down_move and book_imbalance >= 0.0:
        add("DOWNSIDE_EXHAUSTION", 10, "book_no_longer_supports_downside")
    if up_move and ch5 < 0:
        add("UPSIDE_EXHAUSTION", 14, "short_term_reversal_after_pump")
    if down_move and ch5 > 0:
        add("DOWNSIDE_EXHAUSTION", 14, "short_term_reversal_after_dump")

    best_state, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score < 45:
        best_state = "NO_CLEAR_STATE"
    return {
        "version": "pump_state_radar_v1",
        "state": best_state,
        "confidence_score": round(_clip(best_score), 1),
        "scores": {key: round(_clip(value), 1) for key, value in scores.items()},
        "evidence": evidence.get(best_state, []),
        "up_move_active": up_move,
        "down_move_active": down_move,
        "perp_led_up": perp_led_up,
        "perp_led_down": perp_led_down,
        "experimental": True,
        "paper_only": True,
        "validated_out_of_sample": False,
        "creates_entry": False,
        "changes_direction": False,
    }


def build_event_risk(*, reason: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    prediction = _d(reason.get("prediction"))
    metrics = _d(reason.get("metrics")) or _d(score.get("metrics"))
    micro = _d(reason.get("microstructure"))
    cg = _d(reason.get("coinglass")) or _d(reason.get("coinglass_confirmation"))
    btc = _d(reason.get("btc_context")) or _d(prediction.get("btc_context"))

    ch5 = _f(metrics.get("change_5m_pct")); ch15 = _f(metrics.get("change_15m_pct")); ch1h = _f(metrics.get("change_1h_pct"))
    atr = max(0.25, _f(metrics.get("atr_pct"), 0.8))
    rv = max(0.0, _f(metrics.get("relative_volume"), _f(metrics.get("volume_ratio"), 1.0)))
    vacc = _f(metrics.get("volume_acceleration"))
    oi = _f(metrics.get("oi_change_pct"), _f(cg.get("oi_change_5m"))); oi15 = _f(cg.get("oi_change_15m"))
    funding = _f(metrics.get("funding_rate"), _f(cg.get("funding_median")))
    fd = _f(metrics.get("futures_delta_ratio"), _f(micro.get("futures_delta_ratio")))
    sd = _f(metrics.get("spot_delta_ratio"), _f(micro.get("spot_delta_ratio")))
    book = _f(metrics.get("order_book_imbalance"), _f(micro.get("order_book_imbalance")))
    long_liq = max(0.0, _f(cg.get("long_liquidations_1h"), _f(cg.get("long_liquidations"))))
    short_liq = max(0.0, _f(cg.get("short_liquidations_1h"), _f(cg.get("short_liquidations"))))
    liq_imb = _f(cg.get("liquidation_imbalance"))
    btc15 = _f(btc.get("change_15m_pct")); btc1h = _f(btc.get("change_1h_pct"))
    stablecoin_distance = abs(_f(reason.get("stablecoin_distance_pct"), _f(metrics.get("stablecoin_distance_pct"))))
    symbol = str(score.get("symbol") or reason.get("symbol") or "").upper()

    scores = {k: 0.0 for k in ("NORMAL", "STRESS", "LONG_SQUEEZE", "SHORT_SQUEEZE", "LIQUIDATION_CASCADE", "DEPEG_RISK", "NEWS_SHOCK_PROXY", "BLACK_SWAN_PROXY")}
    why: dict[str, list[str]] = {k: [] for k in scores}

    normalized_move = max(abs(ch5), abs(ch15), abs(ch1h) * 0.6) / atr
    if normalized_move >= 1.8: scores["STRESS"] += 24; why["STRESS"].append("move_vs_atr_extreme")
    if rv >= 2.0: scores["STRESS"] += 18; why["STRESS"].append("relative_volume>=2x")
    if rv >= 3.5: scores["STRESS"] += 16; why["STRESS"].append("relative_volume>=3.5x")
    if abs(oi15) >= 2.0 or abs(oi) >= 1.25: scores["STRESS"] += 12; why["STRESS"].append("oi_dislocation")
    if abs(fd) >= 0.20 or abs(sd) >= 0.16 or abs(book) >= 0.20: scores["STRESS"] += 12; why["STRESS"].append("flow_dislocation")
    if vacc >= 2.0: scores["STRESS"] += 10; why["STRESS"].append("volume_acceleration")

    if ch5 <= -max(0.7, atr * 0.85): scores["LONG_SQUEEZE"] += 24; why["LONG_SQUEEZE"].append("fast_down_move")
    if fd <= -0.12: scores["LONG_SQUEEZE"] += 15; why["LONG_SQUEEZE"].append("futures_selling")
    if oi <= -0.45 or oi15 <= -0.8: scores["LONG_SQUEEZE"] += 16; why["LONG_SQUEEZE"].append("oi_flush")
    if long_liq > short_liq * 1.8 and long_liq > 0: scores["LONG_SQUEEZE"] += 20; why["LONG_SQUEEZE"].append("long_liquidations_dominate")
    if liq_imb <= -0.25: scores["LONG_SQUEEZE"] += 12; why["LONG_SQUEEZE"].append("liquidation_imbalance_down")

    if ch5 >= max(0.7, atr * 0.85): scores["SHORT_SQUEEZE"] += 24; why["SHORT_SQUEEZE"].append("fast_up_move")
    if fd >= 0.12: scores["SHORT_SQUEEZE"] += 15; why["SHORT_SQUEEZE"].append("futures_buying")
    if oi <= -0.45 or oi15 <= -0.8: scores["SHORT_SQUEEZE"] += 16; why["SHORT_SQUEEZE"].append("oi_flush")
    if short_liq > long_liq * 1.8 and short_liq > 0: scores["SHORT_SQUEEZE"] += 20; why["SHORT_SQUEEZE"].append("short_liquidations_dominate")
    if liq_imb >= 0.25: scores["SHORT_SQUEEZE"] += 12; why["SHORT_SQUEEZE"].append("liquidation_imbalance_up")

    squeeze = max(scores["LONG_SQUEEZE"], scores["SHORT_SQUEEZE"])
    cascade_side = "SHORT" if scores["LONG_SQUEEZE"] >= scores["SHORT_SQUEEZE"] else "LONG"
    if squeeze >= 45: scores["LIQUIDATION_CASCADE"] += 30; why["LIQUIDATION_CASCADE"].append("squeeze_base")
    if rv >= 2.5: scores["LIQUIDATION_CASCADE"] += 18; why["LIQUIDATION_CASCADE"].append("volume_spike")
    if vacc >= 1.8: scores["LIQUIDATION_CASCADE"] += 12; why["LIQUIDATION_CASCADE"].append("volume_acceleration")
    if abs(liq_imb) >= 0.35 and (long_liq + short_liq) > 0: scores["LIQUIDATION_CASCADE"] += 18; why["LIQUIDATION_CASCADE"].append("one_sided_liquidations")

    stable_pair = symbol in {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "DAIUSDT", "USDEUSDT"}
    if stablecoin_distance >= 0.35 or (stable_pair and abs(ch15) >= 0.35): scores["DEPEG_RISK"] += 55; why["DEPEG_RISK"].append("stablecoin_off_peg")
    if stablecoin_distance >= 0.75 or (stable_pair and abs(ch1h) >= 0.75): scores["DEPEG_RISK"] += 30; why["DEPEG_RISK"].append("material_depeg")

    if normalized_move >= 2.0 and rv >= 2.5: scores["NEWS_SHOCK_PROXY"] += 36; why["NEWS_SHOCK_PROXY"].append("abrupt_move_and_volume")
    if abs(oi15) < 0.6 and abs(oi) < 0.45: scores["NEWS_SHOCK_PROXY"] += 12; why["NEWS_SHOCK_PROXY"].append("little_prior_oi_build")
    if bool(reason.get("external_event_flag") or prediction.get("external_event_flag")): scores["NEWS_SHOCK_PROXY"] += 35; why["NEWS_SHOCK_PROXY"].append("external_event_flag")

    if scores["STRESS"] >= 60: scores["BLACK_SWAN_PROXY"] += 22; why["BLACK_SWAN_PROXY"].append("extreme_stress")
    if abs(ch15) >= max(3.0, atr * 3.2): scores["BLACK_SWAN_PROXY"] += 22; why["BLACK_SWAN_PROXY"].append("very_large_15m_move")
    if rv >= 4.5: scores["BLACK_SWAN_PROXY"] += 18; why["BLACK_SWAN_PROXY"].append("very_high_volume")
    if abs(btc15) >= 2.0 or abs(btc1h) >= 3.5: scores["BLACK_SWAN_PROXY"] += 16; why["BLACK_SWAN_PROXY"].append("btc_systemic_shock")
    if stablecoin_distance >= 1.0: scores["BLACK_SWAN_PROXY"] += 25; why["BLACK_SWAN_PROXY"].append("stablecoin_systemic_risk")

    non_normal_max = max(v for k, v in scores.items() if k != "NORMAL")
    scores["NORMAL"] = 70.0 if non_normal_max < 25 else max(0.0, 45.0 - scores["STRESS"] * 0.4)
    event_type, raw_score = max(scores.items(), key=lambda item: item[1])
    event_score = _clip(raw_score)

    if event_type == "NORMAL": severity = "NORMAL"
    elif event_type in {"BLACK_SWAN_PROXY", "DEPEG_RISK"} and event_score >= 60: severity = "CRITICAL"
    elif event_score >= 65: severity = "HIGH"
    elif event_score >= 45: severity = "ELEVATED"
    else: severity = "WATCH"

    directional_bias = "NEUTRAL"
    if event_type == "LONG_SQUEEZE": directional_bias = "SHORT"
    elif event_type == "SHORT_SQUEEZE": directional_bias = "LONG"
    elif event_type == "LIQUIDATION_CASCADE": directional_bias = cascade_side

    risk_multiplier = {"NORMAL": 1.0, "WATCH": 0.85, "ELEVATED": 0.65, "HIGH": 0.40, "CRITICAL": 0.20}[severity]
    block = event_type in {"BLACK_SWAN_PROXY", "DEPEG_RISK"} and event_score >= 60
    pump_state = _pump_state_radar(
        ch5=ch5,
        ch15=ch15,
        ch1h=ch1h,
        atr=atr,
        relative_volume=rv,
        volume_acceleration=vacc,
        oi_change_pct=max(oi, oi15),
        funding=funding,
        futures_delta=fd,
        spot_delta=sd,
        book_imbalance=book,
    )

    return {
        "version": VERSION, "event_type": event_type, "event_score": round(event_score, 1), "severity": severity,
        "directional_bias": directional_bias, "block_new_entries": block,
        "require_extra_confirmation": severity in {"HIGH", "CRITICAL"} or event_type in {"NEWS_SHOCK_PROXY", "LIQUIDATION_CASCADE"},
        "risk_multiplier": risk_multiplier, "scores": {k: round(_clip(v), 1) for k, v in scores.items()},
        "reasons": why.get(event_type, []), "funding": funding, "pump_state": pump_state,
        "black_swan_is_proxy": True, "predicts_true_black_swan": False,
        "creates_entry": False, "changes_direction": False,
    }
