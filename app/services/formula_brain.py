from __future__ import annotations

import json
from math import log10, log2, sqrt
from statistics import mean, pstdev
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "formula_brain_shadow_v1"
MIN_SAMPLE = 30


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _rma(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    if len(values) < period:
        return [mean(values[: i + 1]) for i in range(len(values))]
    out = [mean(values[:period])]
    prev = out[0]
    for value in values[period:]:
        prev = ((period - 1.0) * prev + value) / period
        out.append(prev)
    return out


def _returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for a, b in zip(closes, closes[1:]):
        if a > 0:
            out.append((b - a) / a)
    return out


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(0.0, x) for x in changes]
    losses = [max(0.0, -x) for x in changes]
    avg_gain = _rma(gains, period)[-1]
    avg_loss = _rma(losses, period)[-1]
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(closes: list[float]) -> dict[str, float | None]:
    if len(closes) < 35:
        return {"macd": None, "signal": None, "histogram": None, "histogram_slope": None}
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    start = max(0, len(ema12) - len(ema26))
    line = [a - b for a, b in zip(ema12[start:], ema26)]
    signal = _ema_series(line, 9)
    hist = [a - b for a, b in zip(line[-len(signal):], signal)]
    return {
        "macd": line[-1] if line else None,
        "signal": signal[-1] if signal else None,
        "histogram": hist[-1] if hist else None,
        "histogram_slope": (hist[-1] - hist[-2]) if len(hist) >= 2 else None,
    }


def _true_ranges(rows: list[list[Any]]) -> list[float]:
    if len(rows) < 2:
        return []
    out: list[float] = []
    prev_close = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return out


def _atr_abs(rows: list[list[Any]], period: int = 14) -> float | None:
    trs = _true_ranges(rows)
    if len(trs) < period:
        return None
    return _rma(trs, period)[-1]


def _adx(rows: list[list[Any]], period: int = 14) -> float | None:
    if len(rows) < period * 2 + 2:
        return None
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for prev, cur in zip(rows, rows[1:]):
        prev_high, prev_low, prev_close = _f(prev[2]), _f(prev[3]), _f(prev[4])
        high, low = _f(cur[2]), _f(cur[3])
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    atr = _rma(trs, period)
    plus = _rma(plus_dm, period)
    minus = _rma(minus_dm, period)
    n = min(len(atr), len(plus), len(minus))
    if n <= 0:
        return None
    dx: list[float] = []
    for tr, pdm, mdm in zip(atr[-n:], plus[-n:], minus[-n:]):
        if tr <= 1e-12:
            dx.append(0.0)
            continue
        pdi = 100.0 * pdm / tr
        mdi = 100.0 * mdm / tr
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom > 1e-12 else 0.0)
    if len(dx) < period:
        return mean(dx) if dx else None
    return _rma(dx, period)[-1]


def _bollinger_z(closes: list[float], period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mu = mean(window)
    sigma = pstdev(window)
    if sigma <= 1e-12:
        return 0.0
    return (window[-1] - mu) / sigma


def _rolling_vwap(rows: list[list[Any]], period: int = 48) -> float | None:
    if not rows:
        return None
    window = rows[-period:]
    quote = sum(_f(row[7]) for row in window if len(row) > 7)
    base = sum(_f(row[5]) for row in window if len(row) > 5)
    if base > 1e-12 and quote > 0:
        return quote / base
    weighted = 0.0
    volume = 0.0
    for row in window:
        high, low, close, vol = _f(row[2]), _f(row[3]), _f(row[4]), _f(row[5])
        typical = (high + low + close) / 3.0
        weighted += typical * vol
        volume += vol
    return weighted / volume if volume > 1e-12 else None


def _donchian_position(rows: list[list[Any]], period: int = 20) -> float | None:
    if len(rows) < period:
        return None
    window = rows[-period:]
    high = max(_f(row[2]) for row in window)
    low = min(_f(row[3]) for row in window)
    close = _f(window[-1][4])
    width = high - low
    return (close - low) / width if width > 1e-12 else 0.5


def _efficiency_ratio(closes: list[float], period: int = 10) -> float | None:
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    change = abs(window[-1] - window[0])
    noise = sum(abs(b - a) for a, b in zip(window, window[1:]))
    return change / noise if noise > 1e-12 else 0.0


def _choppiness(rows: list[list[Any]], period: int = 14) -> float | None:
    if len(rows) < period + 1:
        return None
    window = rows[-(period + 1):]
    trs = _true_ranges(window)
    highs = [_f(row[2]) for row in window[-period:]]
    lows = [_f(row[3]) for row in window[-period:]]
    denom = max(highs) - min(lows)
    tr_sum = sum(trs[-period:])
    if denom <= 1e-12 or tr_sum <= 1e-12:
        return 50.0
    return 100.0 * log10(tr_sum / denom) / log10(period)


def _autocorrelation(values: list[float]) -> float | None:
    if len(values) < 8:
        return None
    x = values[:-1]
    y = values[1:]
    mx, my = mean(x), mean(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = sum((a - mx) ** 2 for a in x)
    dy = sum((b - my) ** 2 for b in y)
    denom = sqrt(dx * dy)
    return num / denom if denom > 1e-12 else 0.0


def _sign_entropy(values: list[float]) -> float | None:
    if len(values) < 8:
        return None
    up = sum(1 for x in values if x > 0)
    down = sum(1 for x in values if x < 0)
    total = up + down
    if total <= 0:
        return 1.0
    probs = [n / total for n in (up, down) if n > 0]
    entropy = -sum(p * log2(p) for p in probs)
    return entropy  # binary entropy already normalized to 0..1


def _obv_slope(rows: list[list[Any]], period: int = 20) -> float | None:
    if len(rows) < period + 1:
        return None
    window = rows[-(period + 1):]
    obv = 0.0
    total_volume = 0.0
    start_obv = 0.0
    for idx, (prev, cur) in enumerate(zip(window, window[1:])):
        prev_close, close = _f(prev[4]), _f(cur[4])
        vol = _f(cur[5])
        total_volume += vol
        if close > prev_close:
            obv += vol
        elif close < prev_close:
            obv -= vol
        if idx == 0:
            start_obv = obv
    return (obv - start_obv) / total_volume if total_volume > 1e-12 else 0.0


def _roc(closes: list[float], period: int = 12) -> float | None:
    if len(closes) < period + 1:
        return None
    base = closes[-period - 1]
    return ((closes[-1] - base) / base) * 100.0 if base > 0 else None


def _realized_vol(returns: list[float], period: int = 20) -> float | None:
    if len(returns) < period:
        return None
    window = returns[-period:]
    return pstdev(window) * sqrt(period) * 100.0


def formula_registry() -> dict[str, Any]:
    return {
        "version": VERSION,
        "research_only": True,
        "formulas": [
            {"name": "RSI_WILDER_14", "purpose": "momentum_balance", "equation": "100 - 100/(1 + avg_gain/avg_loss)"},
            {"name": "MACD_12_26_9", "purpose": "trend_momentum", "equation": "EMA12 - EMA26; signal=EMA9(MACD)"},
            {"name": "ADX_14", "purpose": "trend_strength", "equation": "Wilder-smoothed directional movement / true range"},
            {"name": "BOLLINGER_Z_20", "purpose": "distance_from_mean", "equation": "(close - mean20)/std20"},
            {"name": "VWAP_48", "purpose": "price_vs_volume_weighted_fair_value", "equation": "sum(quote_volume)/sum(base_volume)"},
            {"name": "DONCHIAN_POSITION_20", "purpose": "location_in_recent_range", "equation": "(close-low20)/(high20-low20)"},
            {"name": "KAUFMAN_ER_10", "purpose": "trend_efficiency", "equation": "abs(net_change)/sum(abs(bar_changes))"},
            {"name": "CHOPPINESS_14", "purpose": "trend_vs_range_regime", "equation": "100*log10(sum(TR)/range)/log10(14)"},
            {"name": "RETURN_AUTOCORR_1", "purpose": "persistence_vs_reversion", "equation": "corr(r_t,r_t-1)"},
            {"name": "SIGN_ENTROPY", "purpose": "directional_disorder", "equation": "-sum(p*log2(p)) for up/down returns"},
            {"name": "OBV_SLOPE_20", "purpose": "volume_confirmation", "equation": "normalized signed-volume change"},
            {"name": "ROC_12", "purpose": "rate_of_change", "equation": "(close/close_12_bars_ago - 1)*100"},
            {"name": "REALIZED_VOL_20", "purpose": "recent_path_volatility", "equation": "std(returns20)*sqrt(20)*100"},
        ],
        "policy": {
            "score_is_probability": False,
            "can_create_entry": False,
            "can_raise_leverage": False,
            "can_move_live_stop": False,
            "minimum_comparable_shadow_sample": MIN_SAMPLE,
            "promotion_requires_observed_outcomes": True,
        },
    }


def build_formula_brain(scored: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in (snapshot.get("klines") or []) if isinstance(row, list) and len(row) >= 8][-160:]
    if len(rows) < 35:
        return {
            "version": VERSION,
            "available": False,
            "research_only": True,
            "reason": "insufficient_klines",
            "registry": formula_registry(),
        }

    closes = [_f(row[4]) for row in rows]
    returns = _returns(closes)
    current = closes[-1]
    rsi = _rsi(closes)
    macd = _macd(closes)
    adx = _adx(rows)
    z = _bollinger_z(closes)
    vwap = _rolling_vwap(rows)
    donchian = _donchian_position(rows)
    er = _efficiency_ratio(closes)
    chop = _choppiness(rows)
    autocorr = _autocorrelation(returns[-40:])
    entropy = _sign_entropy(returns[-40:])
    obv = _obv_slope(rows)
    roc = _roc(closes)
    rv = _realized_vol(returns)
    atr = _atr_abs(rows)
    atr_pct = (atr / current * 100.0) if atr and current > 0 else _f((scored.get("metrics") or {}).get("atr_pct"))
    vwap_dist_pct = ((current - vwap) / vwap * 100.0) if vwap and vwap > 0 else None

    if adx is not None and er is not None and chop is not None and adx >= 25 and er >= 0.30 and chop <= 58:
        regime = "TREND_PERSISTENT"
    elif adx is not None and er is not None and chop is not None and adx <= 22 and er <= 0.25 and chop >= 55:
        regime = "RANGE_MEAN_REVERTING"
    else:
        regime = "MIXED"

    long_votes: list[dict[str, Any]] = []
    short_votes: list[dict[str, Any]] = []
    flags: list[str] = []

    def vote(side: str, name: str, weight: float, detail: str) -> None:
        target = long_votes if side == "LONG" else short_votes
        target.append({"formula": name, "weight": weight, "detail": detail})

    if rsi is not None:
        if 55 <= rsi <= 72:
            vote("LONG", "RSI", 1.0, f"rsi={rsi:.1f}")
        elif 28 <= rsi <= 45:
            vote("SHORT", "RSI", 1.0, f"rsi={rsi:.1f}")
        if rsi >= 76:
            flags.append("rsi_overextended_up")
        elif rsi <= 24:
            flags.append("rsi_overextended_down")

    hist = _f(macd.get("histogram"))
    hist_slope = _f(macd.get("histogram_slope"))
    if hist > 0 and hist_slope >= 0:
        vote("LONG", "MACD", 1.25, f"hist={hist:.6g};slope={hist_slope:.6g}")
    elif hist < 0 and hist_slope <= 0:
        vote("SHORT", "MACD", 1.25, f"hist={hist:.6g};slope={hist_slope:.6g}")

    ema20 = _ema_series(closes, 20)[-1]
    ema50 = _ema_series(closes, 50)[-1]
    if adx is not None and adx >= 20:
        if ema20 > ema50:
            vote("LONG", "ADX_EMA_TREND", 1.20, f"adx={adx:.1f};ema20>ema50")
        elif ema20 < ema50:
            vote("SHORT", "ADX_EMA_TREND", 1.20, f"adx={adx:.1f};ema20<ema50")
    elif adx is not None and adx < 18:
        flags.append("weak_trend_adx")

    if vwap_dist_pct is not None:
        if vwap_dist_pct >= 0.12:
            vote("LONG", "VWAP", 0.90, f"above_vwap={vwap_dist_pct:.3f}%")
        elif vwap_dist_pct <= -0.12:
            vote("SHORT", "VWAP", 0.90, f"below_vwap={vwap_dist_pct:.3f}%")
        if atr_pct > 0 and abs(vwap_dist_pct) > max(1.0, atr_pct * 2.5):
            flags.append("extended_from_vwap")

    if donchian is not None:
        if donchian >= 0.72:
            vote("LONG", "DONCHIAN", 0.85, f"position={donchian:.3f}")
        elif donchian <= 0.28:
            vote("SHORT", "DONCHIAN", 0.85, f"position={donchian:.3f}")

    if obv is not None:
        if obv >= 0.08:
            vote("LONG", "OBV", 0.80, f"normalized_slope={obv:.3f}")
        elif obv <= -0.08:
            vote("SHORT", "OBV", 0.80, f"normalized_slope={obv:.3f}")

    if roc is not None:
        if roc >= 0.30:
            vote("LONG", "ROC", 0.80, f"roc12={roc:.3f}%")
        elif roc <= -0.30:
            vote("SHORT", "ROC", 0.80, f"roc12={roc:.3f}%")

    if z is not None:
        if regime == "RANGE_MEAN_REVERTING":
            if z <= -1.50:
                vote("LONG", "BOLLINGER_MEAN_REVERSION", 1.10, f"z={z:.2f}")
            elif z >= 1.50:
                vote("SHORT", "BOLLINGER_MEAN_REVERSION", 1.10, f"z={z:.2f}")
        else:
            if z >= 0.50:
                vote("LONG", "BOLLINGER_TREND_LOCATION", 0.55, f"z={z:.2f}")
            elif z <= -0.50:
                vote("SHORT", "BOLLINGER_TREND_LOCATION", 0.55, f"z={z:.2f}")

    long_weight = sum(_f(x.get("weight")) for x in long_votes)
    short_weight = sum(_f(x.get("weight")) for x in short_votes)
    total_weight = long_weight + short_weight
    edge = long_weight - short_weight
    if edge >= 0.80:
        direction = "LONG"
    elif edge <= -0.80:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    directional_strength = abs(edge) / max(1.0, total_weight)
    consensus_score = _clip(50.0 + directional_strength * 50.0)

    persistence_score = 50.0
    if er is not None:
        persistence_score += (er - 0.25) * 80.0
    if autocorr is not None:
        persistence_score += autocorr * 20.0
    if entropy is not None:
        persistence_score += (0.85 - entropy) * 25.0
    if adx is not None:
        persistence_score += (adx - 20.0) * 0.60
    persistence_score = _clip(persistence_score)

    scanner_direction = str(scored.get("direction") or "").upper()
    agreement = direction in {"LONG", "SHORT"} and direction == scanner_direction
    conflict = direction in {"LONG", "SHORT"} and scanner_direction in {"LONG", "SHORT"} and direction != scanner_direction

    metrics = scored.get("metrics") if isinstance(scored.get("metrics"), dict) else {}
    spread_bps = _f(metrics.get("order_book_spread_bps"))
    if spread_bps > 12:
        flags.append("wide_spread")
    if entropy is not None and entropy >= 0.98:
        flags.append("high_directional_entropy")
    if chop is not None and chop >= 65:
        flags.append("high_choppiness")

    return {
        "version": VERSION,
        "available": True,
        "research_only": True,
        "direction": direction,
        "consensus_score": round(consensus_score, 2),
        "score_is_probability": False,
        "regime": regime,
        "persistence_score": round(persistence_score, 2),
        "scanner_direction": scanner_direction or None,
        "agrees_with_scanner": agreement,
        "conflicts_with_scanner": conflict,
        "long_weight": round(long_weight, 3),
        "short_weight": round(short_weight, 3),
        "long_votes": long_votes,
        "short_votes": short_votes,
        "risk_flags": sorted(set(flags)),
        "indicators": {
            "rsi_14": round(rsi, 4) if rsi is not None else None,
            "macd": round(_f(macd.get("macd")), 10) if macd.get("macd") is not None else None,
            "macd_signal": round(_f(macd.get("signal")), 10) if macd.get("signal") is not None else None,
            "macd_histogram": round(hist, 10) if macd.get("histogram") is not None else None,
            "macd_histogram_slope": round(hist_slope, 10) if macd.get("histogram_slope") is not None else None,
            "adx_14": round(adx, 4) if adx is not None else None,
            "bollinger_z_20": round(z, 4) if z is not None else None,
            "vwap_48": round(vwap, 12) if vwap is not None else None,
            "vwap_distance_pct": round(vwap_dist_pct, 4) if vwap_dist_pct is not None else None,
            "donchian_position_20": round(donchian, 4) if donchian is not None else None,
            "kaufman_efficiency_10": round(er, 4) if er is not None else None,
            "choppiness_14": round(chop, 4) if chop is not None else None,
            "return_autocorr_1": round(autocorr, 4) if autocorr is not None else None,
            "sign_entropy": round(entropy, 4) if entropy is not None else None,
            "obv_slope_20": round(obv, 4) if obv is not None else None,
            "roc_12_pct": round(roc, 4) if roc is not None else None,
            "realized_vol_20_pct": round(rv, 4) if rv is not None else None,
            "atr_pct": round(atr_pct, 4),
        },
        "policy": {
            "can_create_entry": False,
            "can_veto_entry": False,
            "can_raise_leverage": False,
            "can_move_live_stop": False,
            "minimum_sample_before_considering_policy": MIN_SAMPLE,
            "promotion_requires_shadow_outcomes": True,
        },
    }


def _formula_outcome(direction: str, return_pct: float, threshold_pct: float = 0.15) -> bool | None:
    if direction == "LONG":
        return return_pct >= threshold_pct
    if direction == "SHORT":
        return return_pct <= -threshold_pct
    return None


async def formula_brain_calibration_report(
    db: AsyncSession,
    *,
    limit: int = 3000,
) -> dict[str, Any]:
    table_exists = bool((await db.execute(text("SELECT to_regclass('public.heart_shadow_forecasts')"))).scalar_one())
    if not table_exists:
        return {
            "version": VERSION,
            "mode": "SHADOW_ONLY",
            "sample": 0,
            "status": "CALIBRATING",
            "minimum_sample": MIN_SAMPLE,
            "horizons": {},
            "registry": formula_registry(),
        }

    rows = [dict(row) for row in (await db.execute(text("""
        SELECT metadata, outcomes
        FROM heart_shadow_forecasts
        WHERE metadata ? 'formula_brain'
        ORDER BY observed_at DESC
        LIMIT :limit
    """), {"limit": max(1, min(limit, 10000))})).mappings().all()]

    horizon_values: dict[str, list[dict[str, Any]]] = {h: [] for h in ("15m", "1h", "4h", "6h", "24h")}
    regimes: dict[str, dict[str, list[bool]]] = {}

    for row in rows:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else json.loads(str(row.get("metadata") or "{}"))
        outcomes = row.get("outcomes") if isinstance(row.get("outcomes"), dict) else json.loads(str(row.get("outcomes") or "{}"))
        formula = metadata.get("formula_brain") if isinstance(metadata.get("formula_brain"), dict) else {}
        direction = str(formula.get("direction") or "").upper()
        regime = str(formula.get("regime") or "UNKNOWN")
        score = _f(formula.get("consensus_score"))
        if direction not in {"LONG", "SHORT"}:
            continue
        regimes.setdefault(regime, {})
        for horizon in horizon_values:
            outcome = outcomes.get(horizon) if isinstance(outcomes.get(horizon), dict) else {}
            if not outcome.get("mature"):
                continue
            ret = _f(outcome.get("return_pct"))
            correct = _formula_outcome(direction, ret)
            if correct is None:
                continue
            horizon_values[horizon].append({
                "correct": bool(correct),
                "score": score,
                "return_pct": ret,
            })
            regimes[regime].setdefault(horizon, []).append(bool(correct))

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        sample = len(items)
        correct = sum(1 for x in items if x.get("correct"))
        high = [x for x in items if _f(x.get("score")) >= 70]
        high_correct = sum(1 for x in high if x.get("correct"))
        return {
            "sample": sample,
            "correct": correct,
            "accuracy_pct": round(correct / sample * 100.0, 2) if sample else None,
            "high_consensus_sample": len(high),
            "high_consensus_accuracy_pct": round(high_correct / len(high) * 100.0, 2) if high else None,
            "avg_abs_return_pct": round(mean(abs(_f(x.get("return_pct"))) for x in items), 4) if items else None,
            "status": "USABLE" if sample >= MIN_SAMPLE else "CALIBRATING",
        }

    regime_rows: list[dict[str, Any]] = []
    for regime, by_horizon in regimes.items():
        for horizon, values in by_horizon.items():
            sample = len(values)
            correct = sum(1 for x in values if x)
            regime_rows.append({
                "regime": regime,
                "horizon": horizon,
                "sample": sample,
                "accuracy_pct": round(correct / sample * 100.0, 2) if sample else None,
                "status": "USABLE" if sample >= MIN_SAMPLE else "CALIBRATING",
            })
    regime_rows.sort(key=lambda x: (-int(x["sample"]), str(x["regime"]), str(x["horizon"])))

    total_mature = sum(len(v) for v in horizon_values.values())
    return {
        "version": VERSION,
        "mode": "SHADOW_ONLY",
        "research_only": True,
        "captured_formula_signals": len(rows),
        "mature_horizon_observations": total_mature,
        "minimum_sample": MIN_SAMPLE,
        "horizons": {h: summarize(items) for h, items in horizon_values.items()},
        "regime_cohorts": regime_rows[:60],
        "registry": formula_registry(),
        "policy": {
            "can_change_entries_now": False,
            "can_raise_leverage_now": False,
            "promotion_requires_at_least_30_comparable_cases": True,
            "accuracy_is_historical_not_next_trade_probability": True,
        },
    }
