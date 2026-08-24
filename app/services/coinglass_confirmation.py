from __future__ import annotations

from typing import Any

from app.config import settings


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def apply_coinglass_confirmation(score: dict[str, Any], cg: dict[str, Any]) -> dict[str, Any]:
    """Apply a bounded multi-exchange confirmation layer.

    CoinGlass cannot create a trade by itself. It can confirm, downgrade or veto a
    locally detected setup. Adjustments are deliberately small compared with the
    local structure/order-flow model to reduce overfitting.
    """
    result = dict(score)
    metrics = dict(result.get("metrics") or {})
    components = dict(result.get("components") or {})
    direction = str(result.get("direction", ""))
    base_state = str(result.get("state", "NO_TRADE"))
    base_score = _f(result.get("setup_score"))
    base_risk = _f(result.get("risk_score"))
    price_change_15m = _f(metrics.get("change_15m_pct"))
    local_confirmations = int(metrics.get("confirmations") or 0)

    available = bool(cg.get("available"))
    critical_complete = bool(cg.get("critical_complete"))
    reasons: list[str] = []
    confirmations: list[str] = []
    conflicts: list[str] = []
    score_adjustment = 0.0
    risk_adjustment = 0.0

    oi = cg.get("open_interest") or {}
    oi15 = _f(oi.get("change_15m_pct"))
    oi5 = _f(oi.get("change_5m_pct"))
    oi_available = bool(oi.get("available"))

    taker = cg.get("taker") or {}
    taker_ratio = _f(taker.get("buy_sell_ratio"), 1.0)
    taker_available = bool(taker.get("available"))

    funding = cg.get("funding") or {}
    funding_pct = _f(funding.get("median_rate_pct"))
    funding_available = bool(funding.get("available"))

    liquidations = cg.get("liquidations") or {}
    liq_imbalance = _f(liquidations.get("short_minus_long_imbalance_1h"))
    liquidation_available = bool(liquidations.get("available"))

    # Aggregated OI is useful only when interpreted jointly with price direction.
    if oi_available:
        if direction == "LONG":
            if oi15 >= 0.35 and price_change_15m >= 0.10:
                score_adjustment += 4.0
                confirmations.append("cg_oi_confirms_long")
            elif oi15 >= 0.35 and price_change_15m <= -0.10:
                score_adjustment -= 5.0
                risk_adjustment += 7.0
                conflicts.append("cg_oi_price_conflict")
            elif oi15 <= -0.60:
                score_adjustment -= 3.0
                risk_adjustment += 4.0
                conflicts.append("cg_oi_contracting")
        elif direction == "SHORT":
            if oi15 >= 0.35 and price_change_15m <= -0.10:
                score_adjustment += 4.0
                confirmations.append("cg_oi_confirms_short")
            elif oi15 >= 0.35 and price_change_15m >= 0.10:
                score_adjustment -= 5.0
                risk_adjustment += 7.0
                conflicts.append("cg_oi_price_conflict")
            elif oi15 <= -0.60:
                score_adjustment -= 3.0
                risk_adjustment += 4.0
                conflicts.append("cg_oi_contracting")

    # Aggregated aggressive flow across exchanges.
    if taker_available:
        if direction == "LONG":
            if taker_ratio >= 1.12:
                score_adjustment += 4.0
                confirmations.append("cg_taker_confirms_long")
            elif taker_ratio <= 0.90:
                score_adjustment -= 7.0
                risk_adjustment += 8.0
                conflicts.append("cg_taker_against_long")
        elif direction == "SHORT":
            if taker_ratio <= 0.90:
                score_adjustment += 4.0
                confirmations.append("cg_taker_confirms_short")
            elif taker_ratio >= 1.12:
                score_adjustment -= 7.0
                risk_adjustment += 8.0
                conflicts.append("cg_taker_against_short")

    # Funding is a crowding/risk filter, not a directional trigger.
    if funding_available:
        if direction == "LONG" and funding_pct >= 0.05:
            score_adjustment -= 3.0
            risk_adjustment += 5.0
            conflicts.append("cg_long_crowding")
        elif direction == "SHORT" and funding_pct <= -0.05:
            score_adjustment -= 3.0
            risk_adjustment += 5.0
            conflicts.append("cg_short_crowding")
        elif abs(funding_pct) <= 0.02:
            confirmations.append("cg_funding_not_crowded")

    # Liquidations are context only. Never use them as a stand-alone prediction.
    if liquidation_available:
        if direction == "LONG" and liq_imbalance >= 0.25:
            score_adjustment += 1.5
            confirmations.append("cg_recent_short_liquidations")
        elif direction == "SHORT" and liq_imbalance <= -0.25:
            score_adjustment += 1.5
            confirmations.append("cg_recent_long_liquidations")

    hard_conflict = False
    if direction == "LONG" and oi_available and taker_available:
        hard_conflict = (oi15 >= 0.35 and price_change_15m <= -0.10 and taker_ratio <= 0.90)
    elif direction == "SHORT" and oi_available and taker_available:
        hard_conflict = (oi15 >= 0.35 and price_change_15m >= 0.10 and taker_ratio >= 1.12)
    if hard_conflict:
        conflicts.append("cg_multi_exchange_hard_conflict")
        risk_adjustment += 12.0
        score_adjustment -= 10.0

    # Clamp CoinGlass influence. This is confirmation, not a second independent strategy.
    score_adjustment = max(-15.0, min(10.0, score_adjustment))
    risk_adjustment = max(0.0, min(25.0, risk_adjustment))
    adjusted_score = max(0.0, min(100.0, base_score + score_adjustment))
    adjusted_risk = max(0.0, min(100.0, base_risk + risk_adjustment))

    cg_directional_confirmations = len([
        x for x in confirmations if x.startswith("cg_oi_") or x.startswith("cg_taker_")
    ])

    # CoinGlass is not allowed to upgrade WATCH/NO_TRADE into READY. It may only
    # validate an already strong local PREPARING/READY setup.
    state = base_state
    if hard_conflict:
        state = "NO_TRADE"
        reasons.append("coinglass_hard_conflict")
    elif base_state == "READY":
        if settings.coinglass_require_for_ready and (not critical_complete or cg_directional_confirmations < 2):
            state = "PREPARING"
            reasons.append("coinglass_confirmation_required")
        elif adjusted_score < 86 or adjusted_risk > 32:
            state = "PREPARING" if adjusted_score >= 75 and adjusted_risk <= 48 else "NO_TRADE"
            reasons.append("coinglass_downgrade")
    elif base_state == "PREPARING":
        if (
            critical_complete
            and cg_directional_confirmations >= 2
            and adjusted_score >= 86
            and adjusted_risk <= 32
            and local_confirmations >= 3
        ):
            state = "READY"
            reasons.append("coinglass_confirmed_ready")
        elif adjusted_score < 75 or adjusted_risk > 48:
            state = "WATCH" if adjusted_score >= 64 and adjusted_risk <= 65 else "NO_TRADE"
            reasons.append("coinglass_downgrade")

    if settings.coinglass_require_for_ready and state == "READY" and not available:
        state = "PREPARING"
        reasons.append("coinglass_unavailable_for_ready")

    metrics.update({
        "coinglass_available": available,
        "coinglass_critical_complete": critical_complete,
        "coinglass_score_adjustment": round(score_adjustment, 3),
        "coinglass_risk_adjustment": round(risk_adjustment, 3),
        "coinglass_confirmations": confirmations,
        "coinglass_conflicts": conflicts,
        "coinglass_reasons": reasons,
        "coinglass_oi_change_5m_pct": round(oi5, 4),
        "coinglass_oi_change_15m_pct": round(oi15, 4),
        "coinglass_taker_buy_sell_ratio": round(taker_ratio, 4),
        "coinglass_funding_median_pct": round(funding_pct, 6),
        "coinglass_liquidation_imbalance_1h": round(liq_imbalance, 4),
        "coinglass_raw": cg,
    })
    existing_rejects = list(metrics.get("reject_reasons") or [])
    for reason in conflicts + reasons:
        if reason not in existing_rejects:
            existing_rejects.append(reason)
    metrics["reject_reasons"] = existing_rejects

    components["coinglass"] = round(max(0.0, min(20.0, 10.0 + score_adjustment)), 2)
    result["setup_score"] = round(adjusted_score, 2)
    result["risk_score"] = round(adjusted_risk, 2)
    result["state"] = state
    result["metrics"] = metrics
    result["components"] = components
    result["coinglass"] = cg
    return result
