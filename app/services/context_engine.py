from __future__ import annotations

from statistics import mean
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _pct(a: float, b: float) -> float:
    return ((b - a) / a * 100.0) if a else 0.0


def _closes(rows: list[list[Any]]) -> list[float]:
    return [_f(row[4]) for row in rows if len(row) > 4 and _f(row[4]) > 0]


def _trend_score(rows: list[list[Any]]) -> float:
    closes = _closes(rows)
    if len(closes) < 12:
        return 0.0
    short = mean(closes[-4:])
    long = mean(closes[-12:])
    move = _pct(closes[-12], closes[-1])
    slope = _pct(long, short)
    return _clamp((move * 18.0) + (slope * 22.0), -100.0, 100.0)


def _trade_delta(trades: list[dict[str, Any]]) -> dict[str, float | None]:
    buy = 0.0
    sell = 0.0
    for trade in trades:
        price = _f(trade.get("p") or trade.get("price"))
        qty = _f(trade.get("q") or trade.get("qty"))
        notional = price * qty
        if notional <= 0:
            continue
        # Binance m=True means buyer is maker -> aggressive sell. OKX fallback is normalized the same way.
        if bool(trade.get("m", False)):
            sell += notional
        else:
            buy += notional
    total = buy + sell
    if total <= 0:
        return {"buy_notional": 0.0, "sell_notional": 0.0, "delta_ratio": None}
    return {
        "buy_notional": buy,
        "sell_notional": sell,
        "delta_ratio": (buy - sell) / total,
    }


def _book_features(book: dict[str, Any]) -> dict[str, float | None]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return {
            "imbalance": None,
            "microprice": None,
            "mid": None,
            "spread_bps": None,
            "top_bid_size": None,
            "top_ask_size": None,
        }

    bid_price = _f(bids[0][0])
    ask_price = _f(asks[0][0])
    bid_size = _f(bids[0][1])
    ask_size = _f(asks[0][1])
    bid_depth = sum(_f(row[1]) for row in bids[:10] if len(row) >= 2)
    ask_depth = sum(_f(row[1]) for row in asks[:10] if len(row) >= 2)
    depth = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / depth if depth > 0 else None
    mid = (bid_price + ask_price) / 2 if bid_price > 0 and ask_price > 0 else None
    microprice = (
        (ask_price * bid_size + bid_price * ask_size) / (bid_size + ask_size)
        if bid_price > 0 and ask_price > 0 and bid_size + ask_size > 0
        else None
    )
    spread_bps = ((ask_price - bid_price) / mid * 10000.0) if mid else None
    return {
        "imbalance": imbalance,
        "microprice": microprice,
        "mid": mid,
        "spread_bps": spread_bps,
        "top_bid_size": bid_size,
        "top_ask_size": ask_size,
    }


def classify_regime(scored: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(scored.get("metrics") or {})
    k5 = snapshot.get("klines") or []
    k15 = snapshot.get("klines_15m") or []
    k1h = snapshot.get("klines_1h") or []

    t5 = _trend_score(k5)
    t15 = _trend_score(k15)
    t1h = _trend_score(k1h)
    atr_pct = max(0.0, _f(metrics.get("atr_pct")))
    compression_ratio = _f(metrics.get("compression_ratio"), 1.0)
    relative_volume = _f(metrics.get("relative_volume"), 1.0)
    change_15m = _f(metrics.get("change_15m_pct"))
    change_5m = _f(metrics.get("change_5m_pct"))

    aligned_up = t5 >= 16 and t15 >= 12 and t1h >= 8
    aligned_down = t5 <= -16 and t15 <= -12 and t1h <= -8
    disagreement = (t5 > 12 and t15 < -8) or (t5 < -12 and t15 > 8) or (t15 > 14 and t1h < -8) or (t15 < -14 and t1h > 8)

    notes: list[str] = []
    confidence = 55.0
    if atr_pct >= 2.2 or abs(change_15m) >= 4.0 or (relative_volume >= 2.8 and abs(change_5m) >= 1.2):
        regime = "EXTREME_VOLATILITY"
        confidence = 86.0
        notes.append("volatilidad/velocidad extrema; exigir retest y evitar persecución")
    elif abs(change_15m) >= 2.8 and relative_volume >= 1.8 and not (aligned_up or aligned_down):
        regime = "POST_IMPULSE"
        confidence = 78.0
        notes.append("movimiento reciente extendido sin alineación completa; riesgo de agotamiento")
    elif aligned_up or aligned_down:
        regime = "TREND"
        confidence = 76.0 + min(18.0, (abs(t5) + abs(t15) + abs(t1h)) / 15.0)
        notes.append("5m/15m/1h alineados")
    elif compression_ratio <= 0.72 and abs(t15) < 18 and abs(t1h) < 18:
        regime = "RANGE_COMPRESSION"
        confidence = 74.0
        notes.append("compresión compatible con preparación de expansión, todavía requiere trigger")
    elif disagreement:
        regime = "TRANSITION"
        confidence = 72.0
        notes.append("marcos en transición o conflicto; reducir confianza direccional")
    else:
        regime = "RANGE"
        confidence = 62.0
        notes.append("sin tendencia multimarco suficientemente clara")

    directional_bias = "NEUTRAL"
    composite = t5 * 0.45 + t15 * 0.35 + t1h * 0.20
    if composite >= 12:
        directional_bias = "LONG"
    elif composite <= -12:
        directional_bias = "SHORT"

    return {
        "regime": regime,
        "confidence": round(_clamp(confidence), 1),
        "directional_bias": directional_bias,
        "trend_scores": {"5m": round(t5, 1), "15m": round(t15, 1), "1h": round(t1h, 1)},
        "atr_pct": round(atr_pct, 3),
        "compression_ratio": round(compression_ratio, 3),
        "relative_volume": round(relative_volume, 3),
        "notes": notes,
        "source_is_complete": bool(k5 and k15 and k1h),
    }


def microstructure_context(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None,
    direction: str,
) -> dict[str, Any]:
    metrics = dict(scored.get("metrics") or {})
    coinglass = coinglass or {}
    book = _book_features(snapshot.get("order_book") or {})
    futures = _trade_delta(snapshot.get("agg_trades") or [])
    spot = _trade_delta(snapshot.get("spot_agg_trades") or [])

    book_imbalance = book.get("imbalance")
    futures_delta = futures.get("delta_ratio")
    spot_delta = spot.get("delta_ratio")
    current = _f(scored.get("current_price"))
    change_5m = _f(metrics.get("change_5m_pct"))
    oi_change = _f(metrics.get("oi_change_pct"))

    microprice_bias = None
    if book.get("microprice") and book.get("mid"):
        microprice_bias = _pct(_f(book.get("mid")), _f(book.get("microprice")))

    cg_oi = coinglass.get("open_interest", {}) if isinstance(coinglass, dict) else {}
    cg_oi_15m = _f(cg_oi.get("change_15m_pct")) if cg_oi.get("available") else None

    side = 1.0 if direction == "LONG" else -1.0
    evidence = 0.0
    available = 0
    conflicts: list[str] = []
    confirmations: list[str] = []

    for name, value, weight, threshold in (
        ("orderbook", book_imbalance, 22.0, 0.035),
        ("futures_flow", futures_delta, 24.0, 0.035),
        ("spot_flow", spot_delta, 28.0, 0.025),
    ):
        if value is None:
            continue
        available += 1
        aligned = _f(value) * side
        if aligned >= threshold:
            evidence += weight
            confirmations.append(f"{name} alineado")
        elif aligned <= -threshold:
            evidence -= weight
            conflicts.append(f"{name} contrario")

    if microprice_bias is not None:
        available += 1
        if microprice_bias * side >= 0.004:
            evidence += 12.0
            confirmations.append("microprice inclinado a favor")
        elif microprice_bias * side <= -0.004:
            evidence -= 12.0
            conflicts.append("microprice contrario")

    oi_value = cg_oi_15m if cg_oi_15m is not None else oi_change
    if oi_value:
        available += 1
        if oi_value >= 0.18:
            evidence += 8.0
            confirmations.append("OI creciendo durante la preparación")
        elif oi_value <= -0.45:
            evidence -= 5.0
            conflicts.append("OI contrayéndose")

    # Absorption proxy: aggressive flow points one way but price fails to progress.
    absorption = "NONE"
    if futures_delta is not None:
        if futures_delta <= -0.10 and change_5m > -0.10:
            absorption = "SELLS_ABSORBED"
        elif futures_delta >= 0.10 and change_5m < 0.10:
            absorption = "BUYS_ABSORBED"

    score = _clamp(50.0 + evidence)
    aligned = score >= 58.0
    strong_conflict = score <= 32.0 and available >= 2

    return {
        "score": round(score, 1),
        "aligned": aligned,
        "strong_conflict": strong_conflict,
        "available_inputs": available,
        "order_book_imbalance": round(_f(book_imbalance), 4) if book_imbalance is not None else None,
        "microprice": round(_f(book.get("microprice")), 12) if book.get("microprice") else None,
        "microprice_bias_pct": round(_f(microprice_bias), 5) if microprice_bias is not None else None,
        "spread_bps": round(_f(book.get("spread_bps")), 3) if book.get("spread_bps") is not None else None,
        "futures_delta_ratio": round(_f(futures_delta), 4) if futures_delta is not None else None,
        "spot_delta_ratio": round(_f(spot_delta), 4) if spot_delta is not None else None,
        "oi_change_15m_pct": round(_f(oi_value), 4) if oi_value is not None else None,
        "absorption_proxy": absorption,
        "confirmations": confirmations,
        "conflicts": conflicts,
        # These require sequential L2 snapshots; explicitly N/D instead of fabricated values.
        "ofi": None,
        "replenishment": None,
        "liquidity_speed": None,
        "sequential_absorption": None,
        "data_note": "OFI/replenishment/liquidity_speed require sequential order-book snapshots and remain N/D in v1.",
        "current_price": current,
    }


def apply_context_engine(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None,
    prediction: dict[str, Any],
) -> dict[str, Any]:
    if not prediction:
        return prediction

    result = dict(prediction)
    direction = str(result.get("direction") or scored.get("direction") or "LONG")
    regime = classify_regime(scored, snapshot)
    micro = microstructure_context(scored, snapshot, coinglass, direction)
    sequence = dict(result.get("sequence") or {})
    conflicts = list(result.get("conflicts") or [])
    confirmations = list(result.get("confirmations") or [])

    bias = regime.get("directional_bias")
    regime_conflict = bias in {"LONG", "SHORT"} and bias != direction and regime.get("confidence", 0) >= 70
    if regime_conflict:
        conflicts.append("régimen multimarco contrario a la dirección")
    elif bias == direction:
        confirmations.append("régimen multimarco acompaña la dirección")

    if micro.get("strong_conflict"):
        conflicts.append("microestructura agregada fuertemente contraria")
    elif micro.get("aligned") and micro.get("available_inputs", 0) >= 2:
        confirmations.append("microestructura agregada acompaña")

    regime_name = str(regime.get("regime"))
    severe_regime = regime_name in {"EXTREME_VOLATILITY", "POST_IMPULSE"}
    context_guard_pass = not regime_conflict and not micro.get("strong_conflict") and not severe_regime

    # Context is allowed to downgrade a risky activation, never to promote a weaker phase to ACTIVADO.
    if not context_guard_pass and str(result.get("phase")) == "ACTIVADO":
        result["phase"] = "VIGILAR_CONFLICTOS" if not severe_regime else "ESPERAR_RETEST"

    early_context_score = _clamp(
        0.45 * _f(result.get("preactivation_score"))
        + 0.25 * _f(micro.get("score"), 50.0)
        + 0.20 * (100.0 if bias == direction else 50.0 if bias == "NEUTRAL" else 0.0)
        + 0.10 * (100.0 if regime_name == "RANGE_COMPRESSION" else 70.0 if regime_name == "TREND" else 40.0)
    )

    sequence.update({
        "market_regime": regime_name,
        "regime_directional_bias": bias,
        "regime_confidence": regime.get("confidence"),
        "microstructure_score": micro.get("score"),
        "microstructure_inputs": micro.get("available_inputs"),
        "microstructure_conflict": micro.get("strong_conflict"),
        "context_guard_pass": context_guard_pass,
        "early_context_score": round(early_context_score, 1),
    })

    result["sequence"] = sequence
    result["context_engine"] = {
        "version": "regime-micro-v1",
        "regime": regime,
        "microstructure": micro,
        "context_guard_pass": context_guard_pass,
        "early_context_score": round(early_context_score, 1),
        "certainty_note": "El contexto mejora filtrado y anticipación; no representa probabilidad matemática ni garantiza el siguiente movimiento.",
    }
    result["conflicts"] = list(dict.fromkeys(conflicts))[:16]
    result["confirmations"] = list(dict.fromkeys(confirmations))[:14]
    return result
