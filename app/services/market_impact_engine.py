from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _sentiment_value(payload: dict[str, Any]) -> float:
    sentiment = str(payload.get("sentiment") or "NEUTRAL").upper()
    raw = _f(payload.get("raw_sentiment_score"), 0.0)
    adjustment = _f(payload.get("score_adjustment"), 0.0)
    base = 0.0
    if sentiment == "POSITIVE":
        base = 18.0
    elif sentiment == "NEGATIVE":
        base = -18.0
    return max(-30.0, min(30.0, base + raw * 2.0 + adjustment * 1.5))


def build_market_impact(
    scored: dict[str, Any],
    prediction: dict[str, Any],
    coinglass: dict[str, Any] | None,
    symbol_news: dict[str, Any] | None,
    global_news: dict[str, Any] | None,
    broad_market: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fuse external catalysts with observable market reaction.

    The engine estimates whether the environment supports the current predicted
    direction. It never creates a trade by itself and its score is not a
    calibrated probability.
    """

    cg = dict(coinglass or {})
    symbol_news = dict(symbol_news or {})
    global_news = dict(global_news or {})
    broad = dict(broad_market or {})
    fusion = dict(prediction.get("verdict_fusion") or {})
    path = dict(prediction.get("path_forecast") or {})

    direction = str(prediction.get("direction") or scored.get("direction") or "LONG").upper()
    side = 1.0 if direction == "LONG" else -1.0

    symbol_news_raw = _sentiment_value(symbol_news)
    global_news_raw = _sentiment_value(global_news)
    symbol_news_aligned = symbol_news_raw * side
    global_news_aligned = global_news_raw * side

    regime = str(broad.get("regime") or "MIXED").upper()
    regime_raw = 0.0
    if regime == "RISK_ON":
        regime_raw = 18.0
    elif regime == "RISK_OFF":
        regime_raw = -18.0
    regime_aligned = regime_raw * side

    btc = dict(broad.get("btc") or {})
    eth = dict(broad.get("eth") or {})
    btc_trend = str(btc.get("trend") or "NEUTRAL").upper()
    eth_trend = str(eth.get("trend") or "NEUTRAL").upper()
    majors_raw = 0.0
    majors_raw += 12.0 if btc_trend == "BULLISH" else -12.0 if btc_trend == "BEARISH" else 0.0
    majors_raw += 6.0 if eth_trend == "BULLISH" else -6.0 if eth_trend == "BEARISH" else 0.0
    majors_aligned = majors_raw * side

    breadth_raw = _f(broad.get("net_breadth_pct"), 0.0)
    breadth_aligned = max(-24.0, min(24.0, breadth_raw * 0.35 * side))

    oi = dict(cg.get("open_interest") or {})
    taker = dict(cg.get("taker") or {})
    funding = dict(cg.get("funding") or {})
    liq = dict(cg.get("liquidations") or {})

    oi_15m = _f(oi.get("change_15m_pct"), 0.0)
    oi_1h = _f(oi.get("change_1h_pct"), 0.0)
    taker_ratio = _f(taker.get("buy_sell_ratio"), 1.0)
    funding_rate = _f(funding.get("median_rate_pct"), 0.0)
    liq_imbalance = _f(liq.get("short_minus_long_imbalance_1h"), 0.0)

    derivatives = 0.0
    if direction == "LONG":
        derivatives += 8.0 if taker_ratio >= 1.04 else -8.0 if taker_ratio <= 0.96 else 0.0
        derivatives += 7.0 if oi_15m > 0.15 or oi_1h > 0.35 else -5.0 if oi_15m < -0.35 else 0.0
        derivatives += 5.0 if liq_imbalance > 0 else -5.0 if liq_imbalance < 0 else 0.0
        if funding_rate > 0.08:
            derivatives -= 7.0
    else:
        derivatives += 8.0 if taker_ratio <= 0.96 else -8.0 if taker_ratio >= 1.04 else 0.0
        derivatives += 7.0 if oi_15m > 0.15 or oi_1h > 0.35 else -5.0 if oi_15m < -0.35 else 0.0
        derivatives += 5.0 if liq_imbalance < 0 else -5.0 if liq_imbalance > 0 else 0.0
        if funding_rate < -0.08:
            derivatives -= 7.0
    derivatives = max(-25.0, min(25.0, derivatives))

    flow = _f(fusion.get("flow_strength"), 50.0)
    mtf = _f(fusion.get("mtf_strength"), 50.0)
    acceleration = _f(fusion.get("acceleration_score"), 50.0)
    trap_safety = 100.0 - _f(fusion.get("trap_risk"), 50.0)
    technical_reaction = (
        (flow - 50.0) * 0.18
        + (mtf - 50.0) * 0.14
        + (acceleration - 50.0) * 0.10
        + (trap_safety - 50.0) * 0.08
    )
    technical_reaction = max(-25.0, min(25.0, technical_reaction))

    path_bias = str(path.get("final_bias") or "").upper()
    path_alignment = 10.0 if path_bias == direction else -10.0 if path_bias in {"LONG", "SHORT"} else 0.0

    weighted_delta = (
        symbol_news_aligned * 0.18
        + global_news_aligned * 0.12
        + regime_aligned * 0.12
        + majors_aligned * 0.10
        + breadth_aligned * 0.08
        + derivatives * 0.18
        + technical_reaction * 0.17
        + path_alignment * 0.05
    )
    support_score = _clamp(50.0 + weighted_delta)

    symbol_negative_shock = symbol_news_raw <= -24.0
    global_negative_shock = global_news_raw <= -24.0
    opposite_shock = (symbol_negative_shock or global_negative_shock) if direction == "LONG" else (
        symbol_news_raw >= 24.0 or global_news_raw >= 24.0
    )
    reaction_conflict = technical_reaction <= -10.0
    shock_risk = bool(opposite_shock and reaction_conflict)

    if shock_risk:
        state = "SHOCK_RISK"
        label = "RIESGO DE CATALIZADOR"
    elif support_score >= 66:
        state = "SUPPORTIVE"
        label = "ENTORNO APOYA"
    elif support_score <= 38:
        state = "CONFLICT"
        label = "ENTORNO EN CONTRA"
    else:
        state = "NEUTRAL"
        label = "ENTORNO MIXTO"

    factors = {
        "symbol_news": round(symbol_news_aligned, 1),
        "global_news": round(global_news_aligned, 1),
        "market_regime": round(regime_aligned, 1),
        "btc_eth": round(majors_aligned, 1),
        "market_breadth": round(breadth_aligned, 1),
        "derivatives": round(derivatives, 1),
        "technical_reaction": round(technical_reaction, 1),
        "path_alignment": round(path_alignment, 1),
    }

    reasons: list[str] = []
    if symbol_news.get("sentiment") not in {None, "NEUTRAL", "UNAVAILABLE"}:
        reasons.append(f"Noticias del activo: {symbol_news.get('sentiment')}")
    if global_news.get("sentiment") not in {None, "NEUTRAL", "UNAVAILABLE"}:
        reasons.append(f"Noticias globales/macro: {global_news.get('sentiment')}")
    reasons.append(f"Régimen de mercado: {regime}")
    reasons.append(f"BTC {btc_trend} · ETH {eth_trend}")
    reasons.append(f"Breadth neto {breadth_raw:.1f}%")
    if taker.get("available"):
        reasons.append(f"Taker buy/sell {taker_ratio:.2f}x")
    if oi.get("available"):
        reasons.append(f"OI 15m {oi_15m:+.2f}% · 1h {oi_1h:+.2f}%")

    return {
        "version": "market_impact_v1",
        "direction": direction,
        "state": state,
        "label": label,
        "support_score": round(support_score, 1),
        "score_is_probability": False,
        "shock_risk": shock_risk,
        "factors": factors,
        "reasons": reasons[:8],
        "symbol_news": symbol_news,
        "global_news": global_news,
        "broad_market": {
            "regime": regime,
            "net_breadth_pct": round(breadth_raw, 2),
            "btc": btc,
            "eth": eth,
        },
        "derivatives": {
            "oi_15m_pct": round(oi_15m, 3),
            "oi_1h_pct": round(oi_1h, 3),
            "taker_buy_sell_ratio": round(taker_ratio, 3),
            "funding_median_pct": round(funding_rate, 5),
            "liquidation_imbalance_1h": round(liq_imbalance, 2),
        },
        "safety": {
            "creates_entry": False,
            "news_alone_can_trigger_trade": False,
            "can_veto_or_warn": True,
        },
        "note": (
            "Catalyst/Market Impact combines news and market reaction. It is a technical context score, "
            "not a probability or guarantee. News alone cannot create TRADE NOW."
        ),
    }


def apply_market_impact_gate(prediction: dict[str, Any], impact: dict[str, Any]) -> dict[str, Any]:
    """Attach Catalyst context and conservatively gate the current trade classification.

    The active classification lives in ``premove_fingerprint``. Market Impact may
    demote an existing TRADE_NOW / TRADE_SOON, but it never upgrades WATCHLIST or
    NO_TRADE into an entry. That preserves technical-entry ownership while making
    news/macro risk part of the final decision path.
    """
    result = dict(prediction)
    result["market_impact"] = dict(impact or {})

    fingerprint = dict(result.get("premove_fingerprint") or {})
    if not fingerprint:
        return result

    original_class = str(fingerprint.get("trade_class") or "NO_TRADE").upper()
    original_label = str(fingerprint.get("trade_label") or original_class)
    original_grade = str(fingerprint.get("grade") or "C")
    state = str((impact or {}).get("state") or "NEUTRAL").upper()
    support_score = _f((impact or {}).get("support_score"), 50.0)

    trade_class = original_class
    trade_label = original_label
    grade = original_grade
    catalyst_block = False

    if state == "SHOCK_RISK":
        catalyst_block = original_class in {"TRADE_NOW", "TRADE_SOON"}
        if original_class == "TRADE_NOW":
            trade_class = "TRADE_SOON"
            trade_label = "WAIT CATALYST"
            grade = "B"
        elif original_class == "TRADE_SOON":
            trade_class = "WATCHLIST"
            trade_label = "WATCH · CATALYST RISK"
            grade = "C"
    elif state == "CONFLICT":
        if original_class == "TRADE_NOW":
            trade_class = "TRADE_SOON"
            trade_label = "WAIT MARKET ALIGNMENT"
            grade = "A" if original_grade == "A+" else original_grade
        elif original_class == "TRADE_SOON" and support_score <= 30:
            trade_class = "WATCHLIST"
            trade_label = "WATCH · MARKET CONFLICT"
            grade = "C"

    fingerprint["trade_class"] = trade_class
    fingerprint["trade_label"] = trade_label
    fingerprint["grade"] = grade
    fingerprint["market_impact_state"] = state
    fingerprint["market_impact_score"] = round(support_score, 1)
    fingerprint["market_impact_gate"] = {
        "state": state,
        "support_score": round(support_score, 1),
        "original_trade_class": original_class,
        "original_trade_label": original_label,
        "original_grade": original_grade,
        "demoted": trade_class != original_class,
        "catalyst_block": catalyst_block,
        "news_can_create_trade": False,
    }
    result["premove_fingerprint"] = fingerprint
    return result
