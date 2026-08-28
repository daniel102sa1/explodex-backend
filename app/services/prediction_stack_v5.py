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


def _state_score(value: str, positive: set[str], negative: set[str]) -> float:
    value = str(value or "").upper()
    if value in positive:
        return 75.0
    if value in negative:
        return 25.0
    return 50.0


def build_prediction_stack_v5(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    prediction: dict[str, Any],
    coinglass: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Organize ExplodeX prediction engines into an auditable decision stack.

    This layer does not invent a new entry. It reorganizes existing evidence into
    three independent questions:
      1) Direction: which side currently has the stronger thesis?
      2) Timing: is this a good moment to enter that thesis?
      3) Risk/Veto: is there a reason to wait or avoid the trade?

    All numeric scores are technical quality/context scores, not probabilities.
    """

    cg = dict(coinglass or {})
    fusion = dict(prediction.get("verdict_fusion") or {})
    fingerprint = dict(prediction.get("premove_fingerprint") or {})
    zone = dict(prediction.get("entry_zone_engine") or {})
    path = dict(prediction.get("path_forecast") or {})
    sequence = dict(prediction.get("sequence") or {})
    context = dict(prediction.get("context_engine") or {})
    market_impact = dict(prediction.get("market_impact") or {})

    direction = str(prediction.get("direction") or scored.get("direction") or "LONG").upper()
    path_bias = str(path.get("final_bias") or "").upper()
    path_clarity = str(path.get("clarity") or "TIGHT_RACE").upper()
    path_aligned = path_bias == direction if path_bias in {"LONG", "SHORT"} else True

    mtf = _f(fusion.get("mtf_strength"), 50.0)
    flow = _f(fusion.get("flow_strength"), 50.0)
    acceleration = _f(fusion.get("acceleration_score"), 50.0)
    trap_risk = _f(fusion.get("trap_risk"), 50.0)
    decay_risk = _f(fusion.get("decay_risk"), 50.0)
    technical = _f(fusion.get("technical_confidence"), _f(prediction.get("preactivation_score"), 50.0))
    pass_count = int(_f(fusion.get("pass_count"), 0.0))

    metrics = dict(scored.get("metrics") or {})
    atr_pct = _f(metrics.get("atr_pct"), 0.0)
    compression_ratio = _f(metrics.get("compression_ratio"), 1.0)
    compressed = bool(metrics.get("compressed")) or bool(sequence.get("compressed"))
    change_5m = _f(metrics.get("change_5m_pct"), 0.0)
    change_15m = _f(metrics.get("change_15m_pct"), 0.0)

    # 1. Regime layer: simple and explicit. It helps determine how much trust to
    # place in breakout vs pullback logic without pretending to infer a hidden state.
    trending = mtf >= 62 and abs(change_15m) >= max(0.20, atr_pct * 0.35)
    high_vol = atr_pct >= 2.2
    low_vol = compressed or compression_ratio <= 0.65
    if high_vol:
        regime = "HIGH_VOLATILITY"
        regime_label = "ALTA VOLATILIDAD"
    elif low_vol:
        regime = "COMPRESSION"
        regime_label = "COMPRESIÓN"
    elif trending:
        regime = "TREND"
        regime_label = "TENDENCIA"
    else:
        regime = "MIXED_RANGE"
        regime_label = "MIXTO / RANGO"

    # 2. Direction layer. Path can support or conflict, but it cannot override all
    # technical evidence by itself.
    path_component = 70.0 if path_aligned and path_clarity in {"CLEAR", "USABLE"} else 58.0 if path_aligned else 30.0
    direction_score = _clamp(
        technical * 0.30
        + mtf * 0.25
        + flow * 0.20
        + acceleration * 0.10
        + path_component * 0.15
    )
    if path_bias in {"LONG", "SHORT"} and path_bias != direction and direction_score < 72:
        direction_state = "CONFLICT"
    elif direction_score >= 70:
        direction_state = "STRONG"
    elif direction_score >= 58:
        direction_state = "FAVORED"
    else:
        direction_state = "UNCLEAR"

    # 3. Derivatives layer. This keeps OI/taker/funding/liquidations visible as an
    # independent block instead of hiding them inside one composite score.
    oi = dict(cg.get("open_interest") or {})
    taker = dict(cg.get("taker") or {})
    funding = dict(cg.get("funding") or {})
    liquidations = dict(cg.get("liquidations") or {})
    oi_15m = _f(oi.get("change_15m_pct"), 0.0)
    oi_1h = _f(oi.get("change_1h_pct"), 0.0)
    taker_ratio = _f(taker.get("buy_sell_ratio"), 1.0)
    funding_rate = _f(funding.get("median_rate_pct"), 0.0)
    liquidation_imbalance = _f(liquidations.get("short_minus_long_imbalance_1h"), 0.0)

    derivatives_score = 50.0
    if direction == "LONG":
        derivatives_score += 10 if taker_ratio >= 1.04 else -10 if taker_ratio <= 0.96 else 0
        derivatives_score += 8 if (oi_15m >= 0.15 or oi_1h >= 0.35) else -5 if oi_15m <= -0.35 else 0
        derivatives_score += 5 if liquidation_imbalance > 0 else -5 if liquidation_imbalance < 0 else 0
        if funding_rate >= 0.08:
            derivatives_score -= 8
    else:
        derivatives_score += 10 if taker_ratio <= 0.96 else -10 if taker_ratio >= 1.04 else 0
        derivatives_score += 8 if (oi_15m >= 0.15 or oi_1h >= 0.35) else -5 if oi_15m <= -0.35 else 0
        derivatives_score += 5 if liquidation_imbalance < 0 else -5 if liquidation_imbalance > 0 else 0
        if funding_rate <= -0.08:
            derivatives_score -= 8
    derivatives_score = _clamp(derivatives_score)

    # 4. Microstructure / trap / absorption layer.
    anti_trap = _clamp(100.0 - trap_risk)
    freshness = _clamp(100.0 - decay_risk)
    micro_score = _clamp(flow * 0.45 + acceleration * 0.25 + anti_trap * 0.20 + freshness * 0.10)

    # 5. Zone and trigger layers stay separate. Correct direction is not enough to
    # authorize a late/chasing entry.
    zone_state = str(zone.get("state") or "N/D").upper()
    zone_action = str(zone.get("action") or "N/D").upper()
    zone_quality = _f(zone.get("quality_score"), _f(fusion.get("entry_quality"), 0.0))
    trigger_hit = bool(prediction.get("trigger_hit"))
    chase = bool(sequence.get("chase_risk")) or zone_state == "CHASE"
    invalidated = bool(fusion.get("invalidated"))
    hard_block = bool(fusion.get("hard_block"))

    fingerprint_score = _f(fingerprint.get("fingerprint_score"), 0.0)
    trade_class = str(fingerprint.get("trade_class") or "NO_TRADE").upper()
    trade_label = str(fingerprint.get("trade_label") or trade_class)
    steps_to_yes = int(_f(fingerprint.get("steps_to_yes"), len(fingerprint.get("yes_missing") or [])))
    yes_missing = list(fingerprint.get("yes_missing") or [])

    if trade_class == "TRADE_NOW":
        timing_state = "ENTER_NOW"
        timing_label = "SÍ · ENTRADA OPERABLE"
    elif trade_class == "TRADE_SOON":
        timing_state = "WAIT_NEAR"
        timing_label = "ESPERA · CASI LISTA"
    elif trade_class == "WATCHLIST":
        timing_state = "WATCH"
        timing_label = "TODAVÍA NO · VIGILAR"
    else:
        timing_state = "AVOID_OR_COLD"
        timing_label = "NO · NO ENTRAR"

    # 6. Veto/risk layer. These are explicit reasons that may block a technically
    # attractive direction from being a good entry now.
    vetoes: list[str] = []
    warnings: list[str] = []
    if invalidated:
        vetoes.append("tesis invalidada")
    if hard_block:
        vetoes.append("bloqueo duro del guard")
    if chase:
        vetoes.append("precio en chase / fuera de zona")
    if trap_risk >= 72:
        vetoes.append("riesgo de trampa extremo")
    elif trap_risk >= 58:
        warnings.append("riesgo de trampa elevado")
    if decay_risk >= 78:
        warnings.append("momentum deteriorado")
    if path_bias in {"LONG", "SHORT"} and path_bias != direction:
        warnings.append("Path Forecast en conflicto")
    if regime == "HIGH_VOLATILITY" and zone_quality < 72:
        warnings.append("alta volatilidad exige mejor zona")

    catalyst_state = str(market_impact.get("state") or "UNAVAILABLE").upper()
    catalyst_score = _f(market_impact.get("support_score"), 50.0)
    if catalyst_state == "SHOCK_RISK":
        vetoes.append("riesgo de catalizador/noticia")
    elif catalyst_state == "CONFLICT":
        warnings.append("contexto externo en contra")

    # 7. Master decision. It never upgrades NO_TRADE/WAIT to ENTER by itself;
    # timing authorization remains owned by the fingerprint/entry engines.
    if vetoes:
        master_state = "NO"
        master_label = "NO · VETO ACTIVO"
    elif timing_state == "ENTER_NOW":
        master_state = "YES"
        master_label = "SÍ · TRADEAR AHORA"
    elif timing_state == "WAIT_NEAR":
        master_state = "WAIT"
        master_label = "ESPERA · CERCA DEL SÍ"
    elif timing_state == "WATCH":
        master_state = "WATCH"
        master_label = "VIGILAR · AÚN NO"
    else:
        master_state = "NO"
        master_label = "NO · SIN ENTRADA"

    layers = [
        {
            "key": "regime",
            "label": "Régimen",
            "state": regime,
            "display": regime_label,
            "score": None,
        },
        {
            "key": "catalyst",
            "label": "Noticias / Macro",
            "state": catalyst_state,
            "display": catalyst_state if catalyst_state != "UNAVAILABLE" else "VER MARKET IMPACT",
            "score": round(catalyst_score, 1) if catalyst_state != "UNAVAILABLE" else None,
        },
        {
            "key": "direction",
            "label": "Dirección",
            "state": direction_state,
            "display": f"{direction} · {direction_state}",
            "score": round(direction_score, 1),
        },
        {
            "key": "derivatives",
            "label": "Derivados",
            "state": "SUPPORT" if derivatives_score >= 60 else "CONFLICT" if derivatives_score <= 40 else "MIXED",
            "display": f"Derivados {derivatives_score:.0f}/100",
            "score": round(derivatives_score, 1),
        },
        {
            "key": "flow",
            "label": "Flow / Microestructura",
            "state": "SUPPORT" if micro_score >= 62 else "CONFLICT" if micro_score <= 40 else "MIXED",
            "display": f"Flow {micro_score:.0f}/100",
            "score": round(micro_score, 1),
        },
        {
            "key": "trap",
            "label": "Trampa / Absorción",
            "state": "SAFE" if anti_trap >= 62 else "RISK" if anti_trap <= 40 else "MIXED",
            "display": f"Seguridad anti-trap {anti_trap:.0f}/100",
            "score": round(anti_trap, 1),
        },
        {
            "key": "zone",
            "label": "Zona",
            "state": zone_state,
            "display": f"{zone_state} · {zone_quality:.0f}/100",
            "score": round(zone_quality, 1),
        },
        {
            "key": "trigger",
            "label": "Trigger",
            "state": "HIT" if trigger_hit else "WAIT",
            "display": "TRIGGER CONFIRMADO" if trigger_hit else "ESPERANDO TRIGGER",
            "score": None,
        },
        {
            "key": "veto",
            "label": "Veto",
            "state": "BLOCKED" if vetoes else "CLEAR",
            "display": " · ".join(vetoes) if vetoes else "SIN VETO DURO",
            "score": None,
        },
        {
            "key": "decision",
            "label": "¿Tradear?",
            "state": master_state,
            "display": master_label,
            "score": round(fingerprint_score, 1),
        },
    ]

    return {
        "version": "prediction_stack_v5",
        "direction": {
            "side": direction,
            "state": direction_state,
            "score": round(direction_score, 1),
            "score_is_probability": False,
            "path_bias": path_bias or None,
            "path_clarity": path_clarity,
        },
        "entry_timing": {
            "state": timing_state,
            "label": timing_label,
            "trade_class": trade_class,
            "trade_label": trade_label,
            "fingerprint_score": round(fingerprint_score, 1),
            "steps_to_yes": steps_to_yes,
            "yes_missing": yes_missing,
            "zone_state": zone_state,
            "zone_action": zone_action,
            "zone_quality": round(zone_quality, 1),
            "trigger_hit": trigger_hit,
            "locks_passed": pass_count,
        },
        "risk_veto": {
            "blocked": bool(vetoes),
            "vetoes": vetoes,
            "warnings": warnings,
            "trap_risk": round(trap_risk, 1),
            "momentum_decay_risk": round(decay_risk, 1),
            "chase": chase,
            "invalidated": invalidated,
            "hard_block": hard_block,
        },
        "regime": {
            "state": regime,
            "label": regime_label,
            "atr_pct": round(atr_pct, 3),
            "compressed": low_vol,
        },
        "derivatives": {
            "score": round(derivatives_score, 1),
            "score_is_probability": False,
            "oi_15m_pct": round(oi_15m, 3),
            "oi_1h_pct": round(oi_1h, 3),
            "taker_buy_sell_ratio": round(taker_ratio, 3),
            "funding_median_pct": round(funding_rate, 5),
            "liquidation_imbalance_1h": round(liquidation_imbalance, 2),
        },
        "microstructure": {
            "score": round(micro_score, 1),
            "flow": round(flow, 1),
            "acceleration": round(acceleration, 1),
            "anti_trap": round(anti_trap, 1),
            "momentum_freshness": round(freshness, 1),
        },
        "catalyst": {
            "state": catalyst_state,
            "score": round(catalyst_score, 1) if catalyst_state != "UNAVAILABLE" else None,
            "available": catalyst_state != "UNAVAILABLE",
            "note": "External news/macro is supplied by Market Impact when attached to this prediction.",
        },
        "master_decision": {
            "state": master_state,
            "label": master_label,
            "can_trade_now": master_state == "YES",
            "does_not_guarantee_profit": True,
        },
        "layers": layers,
        "safety": {
            "creates_orders": False,
            "can_upgrade_entry": False,
            "paper_research_first": True,
            "scores_are_probabilities": False,
        },
        "note": (
            "Prediction Stack v5 reorganizes existing ExplodeX evidence. Direction, entry timing and veto are intentionally separate. "
            "A strong directional thesis can coexist with ESPERA/NO if the entry geometry or risk is poor."
        ),
    }
