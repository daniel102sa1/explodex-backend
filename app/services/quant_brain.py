from __future__ import annotations

import math
import random
from statistics import mean, median, pstdev
from typing import Any

VERSION = "unified_quant_brain_v1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _bars(rows: list[list[Any]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for row in rows:
        if len(row) < 6:
            continue
        o, h, l, c = (_f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]))
        volume = max(0.0, _f(row[5]))
        quote_volume = max(0.0, _f(row[7])) if len(row) > 7 else volume * c
        if min(o, h, l, c) <= 0:
            continue
        out.append({
            "time": _f(row[0]),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": volume,
            "quote_volume": quote_volume,
        })
    return out


def _simple_returns(closes: list[float]) -> list[float]:
    return [(b / a - 1.0) for a, b in zip(closes[:-1], closes[1:]) if a > 0 and b > 0]


def _log_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes[:-1], closes[1:]) if a > 0 and b > 0]


def _std(values: list[float]) -> float:
    return pstdev(values) if len(values) >= 2 else 0.0


def _quantile(values: list[float], q: float) -> float:
    clean = sorted(v for v in values if math.isfinite(v))
    if not clean:
        return 0.0
    q = _clip(q, 0.0, 1.0)
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return clean[lo]
    w = pos - lo
    return clean[lo] * (1.0 - w) + clean[hi] * w


def _percentile_rank(history: list[float], current: float) -> float:
    clean = [v for v in history if math.isfinite(v)]
    if not clean:
        return 0.0
    below = sum(1 for v in clean if v < current)
    equal = sum(1 for v in clean if abs(v - current) <= 1e-12)
    return (below + 0.5 * equal) / len(clean) * 100.0


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _ema(values: list[float], period: int) -> float:
    series = _ema_series(values, period)
    return series[-1] if series else 0.0


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    changes = [b - a for a, b in zip(closes[-(period + 1):-1], closes[-period:])]
    gains = [max(0.0, x) for x in changes]
    losses = [max(0.0, -x) for x in changes]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _macd(closes: list[float]) -> dict[str, float | None]:
    if len(closes) < 35:
        return {"macd": None, "signal": None, "histogram": None}
    fast = _ema_series(closes, 12)
    slow = _ema_series(closes, 26)
    offset = len(fast) - len(slow)
    line = [fast[i + offset] - slow[i] for i in range(len(slow))]
    signal = _ema_series(line, 9)
    if not line or not signal:
        return {"macd": None, "signal": None, "histogram": None}
    return {
        "macd": line[-1],
        "signal": signal[-1],
        "histogram": line[-1] - signal[-1],
    }


def _atr_pct(bars: list[dict[str, float]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    previous = bars[0]["close"]
    for bar in bars[1:]:
        tr = max(
            bar["high"] - bar["low"],
            abs(bar["high"] - previous),
            abs(bar["low"] - previous),
        )
        if previous > 0:
            trs.append(tr / previous)
        previous = bar["close"]
    sample = trs[-period:]
    return mean(sample) * 100.0 if sample else 0.0


def _rolling_volatility(returns: list[float], period: int = 20) -> list[float]:
    if len(returns) < period:
        return []
    return [_std(returns[i - period:i]) for i in range(period, len(returns) + 1)]


def _zscore(value: float, history: list[float]) -> float:
    if len(history) < 5:
        return 0.0
    mu = mean(history)
    sigma = _std(history)
    return (value - mu) / sigma if sigma > 1e-12 else 0.0


def _vwap(bars: list[dict[str, float]], period: int = 20) -> float:
    sample = bars[-period:]
    numerator = 0.0
    denominator = 0.0
    for bar in sample:
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        weight = bar["volume"]
        numerator += typical * weight
        denominator += weight
    return numerator / denominator if denominator > 1e-12 else (sample[-1]["close"] if sample else 0.0)


def _covariance(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    aa, bb = a[-n:], b[-n:]
    ma, mb = mean(aa), mean(bb)
    return sum((x - ma) * (y - mb) for x, y in zip(aa, bb)) / n


def _correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    aa, bb = a[-n:], b[-n:]
    sa, sb = _std(aa), _std(bb)
    if sa <= 1e-12 or sb <= 1e-12:
        return 0.0
    return _clip(_covariance(aa, bb) / (sa * sb), -1.0, 1.0)


def _beta(asset_returns: list[float], btc_returns: list[float]) -> float:
    n = min(len(asset_returns), len(btc_returns))
    if n < 3:
        return 0.0
    asset, btc = asset_returns[-n:], btc_returns[-n:]
    variance_btc = _std(btc) ** 2
    return _covariance(asset, btc) / variance_btc if variance_btc > 1e-12 else 0.0


def _linear_regression(x: list[float], y: list[float]) -> tuple[float, float]:
    n = min(len(x), len(y))
    if n < 3:
        return 0.0, 0.0
    xx, yy = x[-n:], y[-n:]
    mx, my = mean(xx), mean(yy)
    varx = sum((v - mx) ** 2 for v in xx)
    if varx <= 1e-12:
        return my, 0.0
    slope = sum((a - mx) * (b - my) for a, b in zip(xx, yy)) / varx
    intercept = my - slope * mx
    return intercept, slope


def _hurst_rs(returns: list[float]) -> float | None:
    values = returns[-160:]
    points: list[tuple[float, float]] = []
    for window in (8, 16, 32, 64):
        if len(values) < window:
            continue
        sample = values[-window:]
        mu = mean(sample)
        cumulative: list[float] = []
        total = 0.0
        for value in sample:
            total += value - mu
            cumulative.append(total)
        r = max(cumulative) - min(cumulative)
        s = _std(sample)
        if r > 1e-12 and s > 1e-12:
            points.append((math.log(window), math.log(r / s)))
    if len(points) < 2:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    _, slope = _linear_regression(xs, ys)
    return _clip(slope, 0.0, 1.0)


def _entropy(returns: list[float]) -> float | None:
    sample = returns[-120:]
    if len(sample) < 20:
        return None
    sigma = _std(sample)
    if sigma <= 1e-12:
        return 0.0
    counts = [0, 0, 0, 0, 0]
    for value in sample:
        z = value / sigma
        idx = 0 if z < -1.0 else 1 if z < -0.25 else 2 if z <= 0.25 else 3 if z <= 1.0 else 4
        counts[idx] += 1
    total = sum(counts)
    h = 0.0
    for count in counts:
        if count <= 0:
            continue
        p = count / total
        h -= p * math.log(p)
    return _clip(h / math.log(len(counts)), 0.0, 1.0)


def _efficiency_ratio(closes: list[float], period: int = 20) -> float:
    if len(closes) < 3:
        return 0.0
    recent = closes[-min(period + 1, len(closes)):]
    net = abs(recent[-1] - recent[0])
    path = sum(abs(b - a) for a, b in zip(recent[:-1], recent[1:]))
    return net / path if path > 1e-12 else 0.0


def _kalman_level(closes: list[float], returns: list[float]) -> dict[str, float | None]:
    if len(closes) < 10:
        return {"filtered_price": None, "slope_pct_5": None, "innovation_z": None}
    price = closes[0]
    p = max(1e-8, (closes[0] * 0.002) ** 2)
    sigma_r = max(_std(returns[-80:]), 1e-6)
    q = max(1e-10, (mean(closes[-20:]) * sigma_r * 0.10) ** 2)
    r = max(1e-10, (mean(closes[-20:]) * sigma_r * 0.70) ** 2)
    filtered: list[float] = []
    innovations: list[float] = []
    innovation_scales: list[float] = []
    for observation in closes:
        p = p + q
        s = p + r
        k = p / s
        innovation = observation - price
        price = price + k * innovation
        p = (1.0 - k) * p
        filtered.append(price)
        innovations.append(innovation)
        innovation_scales.append(math.sqrt(max(s, 1e-12)))
    slope = (filtered[-1] / filtered[-6] - 1.0) * 100.0 if len(filtered) >= 6 and filtered[-6] > 0 else 0.0
    innovation_z = innovations[-1] / innovation_scales[-1] if innovation_scales[-1] > 1e-12 else 0.0
    return {
        "filtered_price": filtered[-1],
        "slope_pct_5": slope,
        "innovation_z": _clip(innovation_z, -8.0, 8.0),
    }


def _cointegration_proxy(asset_closes: list[float], btc_closes: list[float]) -> dict[str, Any]:
    n = min(len(asset_closes), len(btc_closes), 160)
    if n < 40:
        return {
            "available": False,
            "method": "OLS residual AR(1) proxy; not an ADF test",
        }
    y = [math.log(v) for v in asset_closes[-n:] if v > 0]
    x = [math.log(v) for v in btc_closes[-n:] if v > 0]
    n = min(len(x), len(y))
    x, y = x[-n:], y[-n:]
    intercept, hedge_beta = _linear_regression(x, y)
    residuals = [yy - (intercept + hedge_beta * xx) for xx, yy in zip(x, y)]
    sigma = _std(residuals)
    residual_z = (residuals[-1] - mean(residuals)) / sigma if sigma > 1e-12 else 0.0

    lagged = residuals[:-1]
    current = residuals[1:]
    ar_intercept, phi = _linear_regression(lagged, current)
    del ar_intercept
    phi = _clip(phi, -1.5, 1.5)
    stationary_proxy = _clip(1.0 - abs(phi), 0.0, 1.0)
    half_life = None
    if 0.0 < phi < 0.999999:
        half_life = -math.log(2.0) / math.log(phi)
    return {
        "available": True,
        "method": "OLS residual AR(1) proxy; not an ADF test",
        "hedge_beta": round(hedge_beta, 4),
        "residual_z": round(residual_z, 4),
        "ar1_phi": round(phi, 4),
        "stationarity_proxy": round(stationary_proxy, 4),
        "half_life_bars": round(half_life, 2) if half_life is not None and math.isfinite(half_life) else None,
    }


def _markov_regime(closes: list[float], returns: list[float]) -> dict[str, Any]:
    if len(closes) < 60 or len(returns) < 50:
        return {
            "state": "UNKNOWN",
            "transition_sample": 0,
            "next_state_frequencies": {},
            "hidden_markov_model": False,
            "method": "empirical Markov regime proxy",
        }

    rolling_vols = _rolling_volatility(returns, 12)
    vol_p75 = _quantile(rolling_vols, 0.75) if rolling_vols else 0.0
    labels: list[str] = []
    for i in range(24, len(closes)):
        c = closes[: i + 1]
        r = returns[:i]
        recent_r = r[-12:]
        vol = _std(recent_r)
        efficiency = _efficiency_ratio(c, 16)
        net = c[-1] / c[-17] - 1.0 if c[-17] > 0 else 0.0
        if vol_p75 > 0 and vol >= vol_p75 * 1.15:
            label = "VOLATILE"
        elif efficiency >= 0.52:
            label = "TREND_UP" if net >= 0 else "TREND_DOWN"
        elif efficiency <= 0.27:
            label = "RANGE"
        else:
            label = "TRANSITION"
        labels.append(label)

    if not labels:
        return {
            "state": "UNKNOWN",
            "transition_sample": 0,
            "next_state_frequencies": {},
            "hidden_markov_model": False,
            "method": "empirical Markov regime proxy",
        }

    transitions: dict[str, dict[str, int]] = {}
    for a, b in zip(labels[:-1], labels[1:]):
        transitions.setdefault(a, {})
        transitions[a][b] = transitions[a].get(b, 0) + 1
    current = labels[-1]
    counts = transitions.get(current, {})
    total = sum(counts.values())
    next_freq = {k: round(v / total * 100.0, 2) for k, v in counts.items()} if total else {}
    return {
        "state": current,
        "transition_sample": total,
        "next_state_frequencies": next_freq,
        "hidden_markov_model": False,
        "method": "empirical Markov regime proxy; state is observed, not a trained HMM",
    }


def _historical_var_cvar(returns: list[float]) -> dict[str, Any]:
    sample = returns[-180:]
    if len(sample) < 20:
        return {"sample": len(sample), "var95_return_pct": None, "cvar95_return_pct": None}
    q05 = _quantile(sample, 0.05)
    tail = [v for v in sample if v <= q05]
    cvar = mean(tail) if tail else q05
    return {
        "sample": len(sample),
        "var95_return_pct": round(q05 * 100.0, 4),
        "cvar95_return_pct": round(cvar * 100.0, 4),
        "method": "historical 5m return distribution",
    }


def _market_monte_carlo(returns: list[float], *, paths: int = 1000, horizon_bars: int = 12) -> dict[str, Any]:
    sample = returns[-160:]
    if len(sample) < 30:
        return {
            "available": False,
            "sample": len(sample),
            "paths": 0,
            "note": "Insufficient returns for bootstrap simulation.",
        }
    seed = 17 + len(sample) * 97 + int(sum(abs(v) for v in sample) * 1e8) % 1_000_003
    rng = random.Random(seed)
    ending: list[float] = []
    drawdowns: list[float] = []
    for _ in range(paths):
        equity = 1.0
        peak = 1.0
        worst = 0.0
        for _bar in range(horizon_bars):
            ret = sample[rng.randrange(len(sample))]
            equity *= math.exp(ret)
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak)
        ending.append((equity - 1.0) * 100.0)
        drawdowns.append(worst * 100.0)
    return {
        "available": True,
        "sample": len(sample),
        "paths": paths,
        "horizon_bars": horizon_bars,
        "median_return_pct": round(_quantile(ending, 0.50), 4),
        "p10_return_pct": round(_quantile(ending, 0.10), 4),
        "p90_return_pct": round(_quantile(ending, 0.90), 4),
        "simulated_up_frequency_pct": round(sum(1 for v in ending if v > 0) / len(ending) * 100.0, 2),
        "simulated_drawdown_p90_pct": round(_quantile(drawdowns, 0.90), 4),
        "is_calibrated_probability": False,
        "method": "deterministic bootstrap of recent 5m log returns",
    }


def _flow_evidence(metrics: dict[str, Any], direction: str) -> dict[str, Any]:
    sign = -1.0 if direction == "SHORT" else 1.0
    futures = sign * _f(metrics.get("futures_delta_ratio"))
    spot = sign * _f(metrics.get("spot_delta_ratio"))
    book = sign * _f(metrics.get("order_book_imbalance"))
    oi = _f(metrics.get("oi_change_pct"))
    funding = _f(metrics.get("funding_rate"))
    taker = _f(metrics.get("taker_avg_3"), 1.0)

    futures_score = math.tanh(futures * 5.0)
    spot_score = math.tanh(spot * 5.0)
    book_score = math.tanh(book * 6.0)
    oi_score = math.tanh(oi / 1.5)
    taker_directional = sign * (taker - 1.0)
    taker_score = math.tanh(taker_directional * 2.5)
    funding_penalty = min(1.0, abs(funding) / 0.0010)

    score = (
        futures_score * 0.27
        + spot_score * 0.25
        + book_score * 0.18
        + oi_score * 0.15
        + taker_score * 0.15
    )
    score *= 1.0 - funding_penalty * 0.20
    return {
        "aligned_score": round(_clip(score, -1.0, 1.0), 4),
        "futures_delta": futures,
        "spot_delta": spot,
        "orderbook": book,
        "oi_change_pct": oi,
        "taker_ratio": taker,
        "funding_rate": funding,
    }


def _strategy_family(regime: str, hurst: float | None, entropy: float | None) -> dict[str, Any]:
    if regime == "VOLATILE" or (entropy is not None and entropy >= 0.90):
        return {
            "preferred": "DEFENSIVE_CONFIRMATION",
            "allowed": ["BREAKOUT_RETEST", "TREND_CONTINUATION"],
            "avoid": ["EARLY_AGGRESSIVE", "TIGHT_MEAN_REVERSION"],
        }
    if regime in {"TREND_UP", "TREND_DOWN"} or (hurst is not None and hurst >= 0.56):
        return {
            "preferred": "TREND_CONTINUATION",
            "allowed": ["BREAKOUT_RETEST", "SWING_TREND", "TACTICAL"],
            "avoid": ["COUNTERTREND_MEAN_REVERSION"],
        }
    if regime == "RANGE" or (hurst is not None and hurst <= 0.44):
        return {
            "preferred": "MEAN_REVERSION_RETEST",
            "allowed": ["STRUCTURE_RETEST", "RANGE_MEAN_REVERSION"],
            "avoid": ["LATE_BREAKOUT_CHASE"],
        }
    return {
        "preferred": "WAIT_FOR_CONFIRMATION",
        "allowed": ["TACTICAL", "STRUCTURE_RETEST"],
        "avoid": ["EARLY_AGGRESSIVE"],
    }


def build_quant_brain(
    *,
    symbol: str,
    direction: str,
    klines_5m: list[list[Any]],
    klines_15m: list[list[Any]] | None,
    btc_5m: list[list[Any]],
    metrics: dict[str, Any] | None = None,
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    direction = str(direction or "").upper()
    metrics = metrics if isinstance(metrics, dict) else {}
    calibration = calibration if isinstance(calibration, dict) else {}

    bars = _bars(klines_5m)
    bars15 = _bars(klines_15m or [])
    btc_bars = _bars(btc_5m)
    closes = [b["close"] for b in bars]
    btc_closes = [b["close"] for b in btc_bars]
    returns = _log_returns(closes)
    simple_returns = _simple_returns(closes)
    btc_returns = _log_returns(btc_closes)

    if direction not in {"LONG", "SHORT"} or len(closes) < 60 or len(btc_closes) < 60:
        return {
            "version": VERSION,
            "symbol": symbol,
            "direction": direction,
            "available": False,
            "reason": "insufficient_quant_history",
            "block_new_entry": False,
            "risk_multiplier": 0.70,
            "score_is_probability": False,
        }

    current = closes[-1]
    log_return_z = _zscore(returns[-1], returns[-61:-1]) if len(returns) >= 61 else 0.0
    price_window = closes[-40:]
    price_z = _zscore(current, price_window[:-1]) if len(price_window) >= 20 else 0.0
    vol = _std(returns[-20:])
    rolling_vol = _rolling_volatility(returns[-180:], 20)
    vol_percentile = _percentile_rank(rolling_vol[:-1], rolling_vol[-1]) if len(rolling_vol) >= 2 else 0.0
    realized_vol_pct_per_5m = vol * 100.0

    vwap20 = _vwap(bars, 20)
    vwap_distance_pct = (current / vwap20 - 1.0) * 100.0 if vwap20 > 0 else 0.0
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema_gap_pct = (ema20 / ema50 - 1.0) * 100.0 if ema50 > 0 else 0.0
    rsi14 = _rsi(closes, 14)
    macd = _macd(closes)
    atr5 = _atr_pct(bars, 14)
    atr15 = _atr_pct(bars15, 14) if bars15 else None

    corr = _correlation(returns[-120:], btc_returns[-120:])
    beta = _beta(returns[-120:], btc_returns[-120:])
    btc_ret_15m = (btc_closes[-1] / btc_closes[-4] - 1.0) * 100.0 if len(btc_closes) >= 4 else 0.0
    btc_ret_60m = (btc_closes[-1] / btc_closes[-13] - 1.0) * 100.0 if len(btc_closes) >= 13 else 0.0

    hurst = _hurst_rs(returns)
    entropy = _entropy(returns)
    efficiency = _efficiency_ratio(closes, 20)
    kalman = _kalman_level(closes, returns)
    cointegration = _cointegration_proxy(closes, btc_closes)
    markov = _markov_regime(closes, returns)
    var_cvar = _historical_var_cvar(simple_returns)
    monte_carlo = _market_monte_carlo(returns)

    flow = _flow_evidence(metrics, direction)
    sign = -1.0 if direction == "SHORT" else 1.0
    vol_norm = max(realized_vol_pct_per_5m, 0.03)
    trend_component = math.tanh(sign * _f(kalman.get("slope_pct_5")) / max(vol_norm * 2.0, 0.10))
    momentum_component = math.tanh(sign * log_return_z / 2.0)
    vwap_component = math.tanh(sign * vwap_distance_pct / max(atr5, 0.10))
    macd_hist = _f(macd.get("histogram"))
    macd_component = math.tanh(sign * (macd_hist / current) * 8000.0) if current > 0 else 0.0
    btc_directional = sign * beta * (btc_ret_15m * 0.65 + btc_ret_60m * 0.35)
    btc_component = math.tanh(btc_directional / max(atr5, 0.25))

    mean_reversion_component = math.tanh(sign * (-price_z) / 2.0)
    coint_component = 0.0
    if cointegration.get("available") and _f(cointegration.get("stationarity_proxy")) >= 0.10 and abs(corr) >= 0.35:
        coint_component = math.tanh(sign * (-_f(cointegration.get("residual_z"))) / 2.0)

    regime = str(markov.get("state") or "TRANSITION")
    if regime in {"TREND_UP", "TREND_DOWN"}:
        weights = {
            "trend": 0.22, "momentum": 0.16, "vwap": 0.08, "macd": 0.10,
            "flow": 0.22, "btc_beta_context": 0.14, "mean_reversion": 0.03, "cointegration": 0.05,
        }
    elif regime == "RANGE":
        weights = {
            "trend": 0.08, "momentum": 0.08, "vwap": 0.10, "macd": 0.08,
            "flow": 0.20, "btc_beta_context": 0.10, "mean_reversion": 0.22, "cointegration": 0.14,
        }
    elif regime == "VOLATILE":
        weights = {
            "trend": 0.12, "momentum": 0.14, "vwap": 0.07, "macd": 0.07,
            "flow": 0.27, "btc_beta_context": 0.23, "mean_reversion": 0.04, "cointegration": 0.06,
        }
    else:
        weights = {
            "trend": 0.15, "momentum": 0.12, "vwap": 0.10, "macd": 0.09,
            "flow": 0.23, "btc_beta_context": 0.15, "mean_reversion": 0.08, "cointegration": 0.08,
        }

    components = {
        "trend": trend_component,
        "momentum": momentum_component,
        "vwap": vwap_component,
        "macd": macd_component,
        "flow": _f(flow.get("aligned_score")),
        "btc_beta_context": btc_component,
        "mean_reversion": mean_reversion_component,
        "cointegration": coint_component,
    }
    raw_edge = sum(components[name] * weight for name, weight in weights.items()) * 100.0

    calibration_adjustment = 0.0
    if str(calibration.get("status") or "") == "USABLE":
        calibration_adjustment = _clip(_f(calibration.get("bounded_conviction_adjustment")), -5.0, 5.0)
    edge = _clip(raw_edge + calibration_adjustment, -100.0, 100.0)

    data_strength = min(100.0, len(returns) / 120.0 * 100.0)
    structure_strength = min(100.0, efficiency * 130.0 + abs(_f(kalman.get("slope_pct_5"))) / max(atr5, 0.1) * 20.0)
    flow_strength = min(100.0, abs(_f(flow.get("aligned_score"))) * 100.0 + 15.0)
    evidence_strength = _clip(data_strength * 0.40 + structure_strength * 0.25 + flow_strength * 0.25 + abs(corr) * 10.0, 0.0, 100.0)

    risk_multiplier = 1.0
    risk_reasons: list[str] = []
    if vol_percentile >= 95:
        risk_multiplier *= 0.45
        risk_reasons.append("volatility_percentile_ge_95")
    elif vol_percentile >= 85:
        risk_multiplier *= 0.65
        risk_reasons.append("volatility_percentile_ge_85")
    elif vol_percentile >= 70:
        risk_multiplier *= 0.82
        risk_reasons.append("volatility_percentile_ge_70")

    if entropy is not None and entropy >= 0.92:
        risk_multiplier *= 0.70
        risk_reasons.append("high_entropy")
    elif entropy is not None and entropy >= 0.85:
        risk_multiplier *= 0.85
        risk_reasons.append("elevated_entropy")

    cvar = var_cvar.get("cvar95_return_pct")
    if cvar is not None and _f(cvar) <= -2.5:
        risk_multiplier *= 0.70
        risk_reasons.append("fat_left_tail")
    elif cvar is not None and _f(cvar) <= -1.5:
        risk_multiplier *= 0.85
        risk_reasons.append("left_tail_risk")

    if regime == "VOLATILE":
        risk_multiplier *= 0.70
        risk_reasons.append("markov_volatile_regime")
    elif regime == "TRANSITION":
        risk_multiplier *= 0.88
        risk_reasons.append("transition_regime")

    if abs(beta) >= 1.6 and abs(corr) >= 0.65:
        risk_multiplier *= 0.82
        risk_reasons.append("high_btc_beta_correlation")

    risk_multiplier = _clip(risk_multiplier, 0.20, 1.0)

    strong_conflict = edge <= -30.0 and evidence_strength >= 55.0
    hard_block = (
        (edge <= -52.0 and evidence_strength >= 65.0)
        or (regime == "VOLATILE" and entropy is not None and entropy >= 0.93 and edge < -10.0)
    )
    support = edge >= 22.0 and evidence_strength >= 50.0

    if hard_block:
        stance = "BLOCK"
    elif strong_conflict:
        stance = "CONFLICT"
    elif support:
        stance = "SUPPORT"
    else:
        stance = "NEUTRAL"

    preferred = _strategy_family(regime, hurst, entropy)
    if regime == "VOLATILE" and vol_percentile >= 95:
        preferred["preferred"] = "DEFENSIVE_WAIT_OR_CONFIRMED_BREAKOUT"

    return {
        "version": VERSION,
        "symbol": symbol,
        "direction": direction,
        "available": True,
        "stance": stance,
        "directional_edge": round(edge, 2),
        "raw_directional_edge": round(raw_edge, 2),
        "evidence_strength": round(evidence_strength, 2),
        "score_is_probability": False,
        "block_new_entry": hard_block,
        "strong_conflict": strong_conflict,
        "supports_direction": support,
        "risk_multiplier": round(risk_multiplier, 4),
        "risk_reasons": risk_reasons,
        "strategy_selector": preferred,
        "regime": {
            **markov,
            "hurst_exponent": round(hurst, 4) if hurst is not None else None,
            "entropy_normalized": round(entropy, 4) if entropy is not None else None,
            "efficiency_ratio": round(efficiency, 4),
        },
        "statistics": {
            "last_log_return_z": round(log_return_z, 4),
            "price_z_40": round(price_z, 4),
            "realized_vol_pct_per_5m": round(realized_vol_pct_per_5m, 5),
            "volatility_percentile_recent": round(vol_percentile, 2),
            "atr_5m_pct": round(atr5, 4),
            "atr_15m_pct": round(atr15, 4) if atr15 is not None else None,
            "vwap20": round(vwap20, 12),
            "distance_to_vwap_pct": round(vwap_distance_pct, 4),
            "ema20": round(ema20, 12),
            "ema50": round(ema50, 12),
            "ema_gap_pct": round(ema_gap_pct, 4),
            "rsi14": round(rsi14, 2) if rsi14 is not None else None,
            "macd": round(_f(macd.get("macd")), 12) if macd.get("macd") is not None else None,
            "macd_signal": round(_f(macd.get("signal")), 12) if macd.get("signal") is not None else None,
            "macd_histogram": round(_f(macd.get("histogram")), 12) if macd.get("histogram") is not None else None,
            "kalman": {
                "filtered_price": round(_f(kalman.get("filtered_price")), 12) if kalman.get("filtered_price") is not None else None,
                "slope_pct_5": round(_f(kalman.get("slope_pct_5")), 5) if kalman.get("slope_pct_5") is not None else None,
                "innovation_z": round(_f(kalman.get("innovation_z")), 4) if kalman.get("innovation_z") is not None else None,
            },
        },
        "btc_relationship": {
            "correlation_5m": round(corr, 4),
            "beta_5m": round(beta, 4),
            "btc_return_15m_pct": round(btc_ret_15m, 4),
            "btc_return_60m_pct": round(btc_ret_60m, 4),
        },
        "cointegration": cointegration,
        "orderflow": flow,
        "var_cvar": var_cvar,
        "monte_carlo_market": monte_carlo,
        "components": {k: round(v, 4) for k, v in components.items()},
        "weights": weights,
        "calibration": {
            "status": calibration.get("status") or "CALIBRATING",
            "sample": int(calibration.get("sample") or 0),
            "historical_accuracy_pct": calibration.get("accuracy_pct"),
            "bounded_edge_adjustment": calibration_adjustment,
            "is_next_trade_probability": False,
            "note": "Historical shadow-forecast calibration may adjust conviction only after enough mature cases.",
        },
        "rules": {
            "may_upgrade_wait_to_entry": False,
            "may_block_or_reduce_entry": True,
            "may_change_primary_direction": False,
            "may_widen_stop_after_entry": False,
            "risk_multiplier_applies_after_structural_stop": True,
        },
    }
