from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def build_liquidation_cascade_context(
    scored: dict[str, Any],
    coinglass: dict[str, Any] | None,
    prediction: dict[str, Any],
) -> dict[str, Any]:
    """Estimate observed liquidation pressure without pretending to predict certainty.

    This engine uses only data already returned by CoinGlass: recent long/short
    liquidations, OI change and taker buy/sell flow. It can warn or support context,
    but never creates an entry by itself.
    """
    coinglass = coinglass or {}
    liquidations = coinglass.get("liquidations", {}) if isinstance(coinglass, dict) else {}
    oi = coinglass.get("open_interest", {}) if isinstance(coinglass, dict) else {}
    taker = coinglass.get("taker", {}) if isinstance(coinglass, dict) else {}
    metrics = dict(scored.get("metrics") or {})

    direction = str(prediction.get("direction") or scored.get("direction") or "LONG")
    long_1h = _f(liquidations.get("long_1h"))
    short_1h = _f(liquidations.get("short_1h"))
    total_1h = _f(liquidations.get("total_1h"), long_1h + short_1h)
    long_4h = _f(liquidations.get("long_4h"))
    short_4h = _f(liquidations.get("short_4h"))
    total_4h = _f(liquidations.get("total_4h"), long_4h + short_4h)
    imbalance = _f(liquidations.get("short_minus_long_imbalance_1h"))

    oi_5m = _f(oi.get("change_5m_pct"))
    oi_15m = _f(oi.get("change_15m_pct"))
    taker_buy = _f(taker.get("buy_ratio_pct"), 50.0)
    taker_sell = _f(taker.get("sell_ratio_pct"), 50.0)
    price_5m = _f(metrics.get("change_5m_pct"))
    price_15m = _f(metrics.get("change_15m_pct"))

    available = bool(liquidations.get("available"))
    if not available:
        return {
            "available": False,
            "status": "N/D",
            "direction": direction,
            "cascade_score": None,
            "cascade_bias": "NEUTRAL",
            "risk_to_direction": False,
            "supports_direction": False,
            "notes": ["CoinGlass liquidation data unavailable"],
        }

    # Relative burst: compare the last hour with the 4h average. >1 means current
    # liquidation intensity is above the recent four-hour baseline.
    hourly_baseline = total_4h / 4.0 if total_4h > 0 else 0.0
    burst_ratio = total_1h / hourly_baseline if hourly_baseline > 0 else 0.0

    # Imbalance is positive when short liquidations dominate, which is upward squeeze pressure.
    squeeze_side = "UP" if imbalance >= 0.12 else "DOWN" if imbalance <= -0.12 else "NEUTRAL"
    flow_side = "UP" if taker_buy - taker_sell >= 6 else "DOWN" if taker_sell - taker_buy >= 6 else "NEUTRAL"
    price_side = "UP" if price_5m >= 0.35 or price_15m >= 0.7 else "DOWN" if price_5m <= -0.35 or price_15m <= -0.7 else "NEUTRAL"

    evidence = 0.0
    notes: list[str] = []
    if burst_ratio >= 2.0:
        evidence += 28.0
        notes.append("liquidation burst >=2x recent 4h hourly baseline")
    elif burst_ratio >= 1.35:
        evidence += 16.0
        notes.append("liquidation intensity above recent baseline")

    if abs(imbalance) >= 0.45:
        evidence += 26.0
        notes.append("strong one-sided liquidation imbalance")
    elif abs(imbalance) >= 0.20:
        evidence += 14.0
        notes.append("moderate one-sided liquidation imbalance")

    if squeeze_side != "NEUTRAL" and squeeze_side == flow_side:
        evidence += 18.0
        notes.append("taker flow confirms liquidation pressure")
    if squeeze_side != "NEUTRAL" and squeeze_side == price_side:
        evidence += 16.0
        notes.append("price is already reacting in liquidation direction")

    # OI falling while liquidations accelerate suggests forced deleveraging; OI rising
    # warns that fresh leverage is still entering and the move may remain unstable.
    deleveraging = oi_5m <= -0.35 or oi_15m <= -0.65
    fresh_leverage = oi_5m >= 0.30 or oi_15m >= 0.60
    if deleveraging and squeeze_side != "NEUTRAL":
        evidence += 12.0
        notes.append("OI contraction supports forced deleveraging")
    elif fresh_leverage and squeeze_side != "NEUTRAL":
        notes.append("OI still expanding; squeeze can extend but instability remains")

    score = _clamp(evidence)
    cascade_bias = "LONG" if squeeze_side == "UP" else "SHORT" if squeeze_side == "DOWN" else "NEUTRAL"
    risk_to_direction = cascade_bias in {"LONG", "SHORT"} and cascade_bias != direction and score >= 48
    supports_direction = cascade_bias == direction and score >= 48

    if score >= 72:
        status = "CASCADE_ACTIVE"
    elif score >= 48:
        status = "CASCADE_BUILDING"
    elif score >= 24:
        status = "ELEVATED"
    else:
        status = "NORMAL"

    return {
        "available": True,
        "status": status,
        "direction": direction,
        "cascade_score": round(score, 1),
        "cascade_bias": cascade_bias,
        "risk_to_direction": risk_to_direction,
        "supports_direction": supports_direction,
        "burst_ratio_vs_4h_hourly": round(burst_ratio, 3),
        "short_minus_long_imbalance_1h": round(imbalance, 4),
        "long_liquidations_1h_usd": long_1h,
        "short_liquidations_1h_usd": short_1h,
        "total_liquidations_1h_usd": total_1h,
        "oi_change_5m_pct": oi_5m,
        "oi_change_15m_pct": oi_15m,
        "taker_buy_ratio_pct": taker_buy,
        "taker_sell_ratio_pct": taker_sell,
        "deleveraging": deleveraging,
        "fresh_leverage": fresh_leverage,
        "notes": notes[:10],
        "certainty_note": "Observed liquidation pressure is contextual evidence, not a probability or guarantee of continuation.",
    }


def apply_liquidation_cascade(
    scored: dict[str, Any],
    coinglass: dict[str, Any] | None,
    prediction: dict[str, Any],
) -> dict[str, Any]:
    if not prediction:
        return prediction
    result = dict(prediction)
    cascade = build_liquidation_cascade_context(scored, coinglass, result)
    context = dict(result.get("context_engine") or {})
    context["liquidation_cascade"] = cascade
    result["context_engine"] = context

    conflicts = list(result.get("conflicts") or [])
    confirmations = list(result.get("confirmations") or [])
    if cascade.get("risk_to_direction"):
        conflicts.append("liquidation cascade pressure opposes setup direction")
        if str(result.get("phase")) == "ACTIVADO" and float(cascade.get("cascade_score") or 0) >= 72:
            result["phase"] = "VIGILAR_CONFLICTOS"
    elif cascade.get("supports_direction"):
        confirmations.append("liquidation cascade pressure supports setup direction")

    result["conflicts"] = list(dict.fromkeys(conflicts))[:16]
    result["confirmations"] = list(dict.fromkeys(confirmations))[:14]
    return result
