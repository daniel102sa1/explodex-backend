from __future__ import annotations

from typing import Any

VERSION = "liquidity_target_engine_v1"


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
    return max(0.0, min(100.0, value))


def build_liquidity_targets(
    score: dict[str, Any],
    prediction: dict[str, Any],
    thesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe plausible price magnets without pretending to know future liquidations.

    Automatic scanning does not rely on CoinGlass heatmap because it may not be
    available on the configured plan. This engine therefore combines the frozen
    trade geometry, nearby structural highs/lows, OI/taker/funding and observed
    liquidation imbalance into a *liquidity attraction proxy*. It is advisory;
    it does not move the frozen stop or targets by itself.
    """
    metrics = _d(score.get("metrics"))
    cg = _d(score.get("coinglass"))
    if not cg:
        cg = _d(metrics.get("coinglass"))
    cg_oi = _d(cg.get("open_interest"))
    cg_taker = _d(cg.get("taker"))
    cg_funding = _d(cg.get("funding"))
    cg_liq = _d(cg.get("liquidations"))

    plan = thesis if isinstance(thesis, dict) and thesis.get("frozen_plan") else prediction
    direction = str((plan or {}).get("direction") or prediction.get("direction") or score.get("direction") or "").upper()
    current = _f(score.get("current_price"))
    stop = _f((plan or {}).get("stop_loss"), _f(score.get("stop_loss")))
    tp1 = _f((plan or {}).get("tp1"), _f(score.get("tp1")))
    tp2 = _f((plan or {}).get("tp2"), _f(score.get("tp2")))
    tp3 = _f((plan or {}).get("tp3"), _f(score.get("tp3")))
    risk = abs(current - stop) if current > 0 and stop > 0 else 0.0

    dist_high = max(0.0, _f(metrics.get("distance_to_high_pct")))
    dist_low = max(0.0, _f(metrics.get("distance_to_low_pct")))
    structure_high = current * (1.0 + dist_high / 100.0) if current > 0 and dist_high > 0 else 0.0
    structure_low = current * (1.0 - dist_low / 100.0) if current > 0 and dist_low > 0 else 0.0

    taker_ratio = _f(cg_taker.get("buy_sell_ratio"), _f(metrics.get("futures_buy_sell_ratio"), 1.0))
    oi5 = _f(cg_oi.get("change_5m_pct"), _f(metrics.get("oi_change_pct")))
    oi15 = _f(cg_oi.get("change_15m_pct"))
    funding = _f(cg_funding.get("median_rate_pct"), _f(metrics.get("funding_rate")) * 100.0)
    liq_imbalance = _f(cg_liq.get("short_minus_long_imbalance_1h"))
    long_liq = _f(cg_liq.get("long_1h"))
    short_liq = _f(cg_liq.get("short_1h"))

    upward = 50.0
    downward = 50.0

    if taker_ratio >= 1.08:
        upward += min(14.0, (taker_ratio - 1.0) * 45.0)
        downward -= min(8.0, (taker_ratio - 1.0) * 25.0)
    elif taker_ratio <= 0.92 and taker_ratio > 0:
        downward += min(14.0, (1.0 - taker_ratio) * 45.0)
        upward -= min(8.0, (1.0 - taker_ratio) * 25.0)

    # Rising OI adds fuel but direction comes from aligned taker/price context.
    if max(oi5, oi15) >= 0.25:
        if direction == "LONG":
            upward += 7.0
        elif direction == "SHORT":
            downward += 7.0

    # Observed liquidation imbalance is used as context only. Heavy short
    # liquidations can mean squeeze already happened, so the boost is capped.
    if liq_imbalance >= 0.20:
        upward += 5.0
    elif liq_imbalance <= -0.20:
        downward += 5.0

    # Crowded funding raises opposite squeeze risk.
    if funding >= 0.05:
        downward += 5.0
        upward -= 3.0
    elif funding <= -0.05:
        upward += 5.0
        downward -= 3.0

    futures_delta = _f(metrics.get("futures_delta_ratio"))
    spot_delta = _f(metrics.get("spot_delta_ratio"))
    book = _f(metrics.get("order_book_imbalance"))
    upward += max(0.0, futures_delta) * 25.0 + max(0.0, spot_delta) * 25.0 + max(0.0, book) * 18.0
    downward += max(0.0, -futures_delta) * 25.0 + max(0.0, -spot_delta) * 25.0 + max(0.0, -book) * 18.0

    upward = _clip(upward)
    downward = _clip(downward)
    attraction_direction = "UP" if upward >= downward + 8 else "DOWN" if downward >= upward + 8 else "BALANCED"
    aligned = (direction == "LONG" and attraction_direction == "UP") or (direction == "SHORT" and attraction_direction == "DOWN")

    candidates: list[dict[str, Any]] = []
    for name, price, kind in (
        ("TP1", tp1, "frozen_target"),
        ("TP2", tp2, "frozen_target"),
        ("TP3", tp3, "frozen_target"),
        ("RECENT_HIGH", structure_high, "structure"),
        ("RECENT_LOW", structure_low, "structure"),
    ):
        if price <= 0 or current <= 0:
            continue
        distance_pct = (price - current) / current * 100.0
        favorable = distance_pct > 0 if direction == "LONG" else distance_pct < 0
        r_distance = abs(price - current) / risk if risk > 0 else None
        candidates.append({
            "name": name,
            "price": round(price, 12),
            "kind": kind,
            "distance_pct": round(distance_pct, 4),
            "favorable_for_thesis": favorable,
            "distance_r": round(r_distance, 3) if r_distance is not None else None,
        })

    favorable_candidates = [x for x in candidates if x["favorable_for_thesis"]]
    favorable_candidates.sort(key=lambda x: abs(_f(x.get("distance_pct"))))
    nearest = favorable_candidates[0] if favorable_candidates else None

    return {
        "version": VERSION,
        "direction": direction,
        "attraction_direction": attraction_direction,
        "upward_attraction_score": round(upward, 1),
        "downward_attraction_score": round(downward, 1),
        "aligned_with_thesis": aligned,
        "nearest_favorable_magnet": nearest,
        "candidates": candidates[:8],
        "inputs": {
            "taker_buy_sell_ratio": round(taker_ratio, 4),
            "oi_change_5m_pct": round(oi5, 4),
            "oi_change_15m_pct": round(oi15, 4),
            "funding_median_pct": round(funding, 5),
            "liquidation_imbalance_1h": round(liq_imbalance, 4),
            "long_liquidated_1h_usd": round(long_liq, 2),
            "short_liquidated_1h_usd": round(short_liq, 2),
        },
        "target_is_forecast_not_guarantee": True,
        "rule": "Liquidity intelligence is advisory and never widens the frozen stop or moves TP after entry.",
    }
