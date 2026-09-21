from __future__ import annotations

from statistics import mean
from typing import Any

VERSION = "macro_cycle_engine_v1"
MIN_DAYS = 90


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _window(rows: list[list[Any]], days: int) -> list[list[Any]]:
    return rows[-min(days, len(rows)) :]


def _stats(rows: list[list[Any]]) -> dict[str, float]:
    if not rows:
        return {
            "range_pct": 0.0,
            "return_pct": 0.0,
            "position": 0.5,
            "volume_mean": 0.0,
        }
    highs = [_f(r[2]) for r in rows]
    lows = [_f(r[3]) for r in rows]
    closes = [_f(r[4]) for r in rows]
    volumes = [_f(r[7]) if len(r) > 7 else _f(r[5]) for r in rows]
    current = closes[-1]
    first = closes[0]
    hi = max(highs)
    lo = min(lows)
    span = hi - lo
    return {
        "range_pct": ((hi - lo) / current * 100.0) if current > 0 else 0.0,
        "return_pct": ((current - first) / first * 100.0) if first > 0 else 0.0,
        "position": ((current - lo) / span) if span > 1e-12 else 0.5,
        "volume_mean": mean(volumes) if volumes else 0.0,
        "high": hi,
        "low": lo,
    }


def _atr_pct(rows: list[list[Any]], period: int) -> float:
    valid = [r for r in rows if isinstance(r, list) and len(r) >= 5]
    if len(valid) < 3:
        return 0.0
    trs: list[float] = []
    prev = _f(valid[0][4])
    for row in valid[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        if prev <= 0:
            prev = close
            continue
        tr = max(high - low, abs(high - prev), abs(low - prev))
        trs.append(tr / prev * 100.0)
        prev = close
    return mean(trs[-min(period, len(trs)) :]) if trs else 0.0


def _block_extrema(values: list[float], *, blocks: int = 4, mode: str = "min") -> list[float]:
    if len(values) < blocks * 4:
        return []
    block_size = max(4, len(values) // blocks)
    data = values[-block_size * blocks :]
    out: list[float] = []
    for i in range(blocks):
        part = data[i * block_size : (i + 1) * block_size]
        out.append(min(part) if mode == "min" else max(part))
    return out


def _rising(values: list[float], tolerance: float = 0.02) -> bool:
    if len(values) < 4:
        return False
    return all(values[i] >= values[i - 1] * (1 - tolerance) for i in range(1, len(values))) and values[-1] > values[0]


def _falling(values: list[float], tolerance: float = 0.02) -> bool:
    if len(values) < 4:
        return False
    return all(values[i] <= values[i - 1] * (1 + tolerance) for i in range(1, len(values))) and values[-1] < values[0]


def _obv_bias(rows: list[list[Any]], days: int = 120) -> float:
    sample = _window(rows, days)
    if len(sample) < 10:
        return 0.0
    obv = 0.0
    series: list[float] = [0.0]
    prev = _f(sample[0][4])
    total_volume = 0.0
    for row in sample[1:]:
        close = _f(row[4])
        volume = _f(row[7]) if len(row) > 7 else _f(row[5])
        total_volume += abs(volume)
        if close > prev:
            obv += volume
        elif close < prev:
            obv -= volume
        series.append(obv)
        prev = close
    if total_volume <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, (series[-1] - series[0]) / total_volume))


def _relative_strength(rows: list[list[Any]], btc_rows: list[list[Any]] | None, days: int) -> float | None:
    if not btc_rows:
        return None
    asset = _stats(_window(rows, days))["return_pct"]
    btc = _stats(_window(btc_rows, days))["return_pct"]
    return asset - btc


def build_macro_cycle(
    rows: list[list[Any]],
    *,
    btc_rows: list[list[Any]] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    valid = [r for r in (rows or []) if isinstance(r, list) and len(r) >= 8 and _f(r[4]) > 0]
    if len(valid) < MIN_DAYS:
        return {
            "version": VERSION,
            "available": False,
            "history_days": len(valid),
            "reason": "insufficient_daily_history",
            "can_create_entry": False,
        }

    current = _f(valid[-1][4])
    closes = [_f(r[4]) for r in valid]
    highs = [_f(r[2]) for r in valid]
    lows = [_f(r[3]) for r in valid]

    windows: dict[str, dict[str, float]] = {}
    for days in (90, 180, 365, 730, 1095):
        if len(valid) >= min(days, 90):
            s = _stats(_window(valid, days))
            windows[f"{days}d"] = {k: round(v, 5) for k, v in s.items()}

    s90 = _stats(_window(valid, 90))
    s180 = _stats(_window(valid, 180))
    s365 = _stats(_window(valid, 365))
    slong = _stats(_window(valid, min(1095, len(valid))))

    atr20 = _atr_pct(valid, 20)
    atr90 = _atr_pct(valid, 90)
    volatility_contraction = atr20 / atr90 if atr90 > 1e-12 else 1.0

    recent30 = _stats(_window(valid, 30))
    prior120 = valid[-150:-30] if len(valid) >= 150 else valid[:-30]
    prior_volume = _stats(prior120)["volume_mean"] if prior120 else 0.0
    volume_ratio = recent30["volume_mean"] / prior_volume if prior_volume > 1e-12 else 1.0

    ema50 = _ema(closes[-min(260, len(closes)) :], 50)
    ema200 = _ema(closes[-min(500, len(closes)) :], 200) if len(closes) >= 200 else 0.0
    obv_bias = _obv_bias(valid, 120)

    low_blocks = _block_extrema(lows[-120:], mode="min")
    high_blocks = _block_extrema(highs[-120:], mode="max")
    higher_lows = _rising(low_blocks)
    lower_highs = _falling(high_blocks)

    prior180 = valid[-240:-60] if len(valid) >= 240 else valid[:-60]
    prior_low = min((_f(r[3]) for r in prior180), default=s180["low"])
    prior_high = max((_f(r[2]) for r in prior180), default=s180["high"])
    last60 = valid[-60:]
    last60_low = min((_f(r[3]) for r in last60), default=current)
    last60_high = max((_f(r[2]) for r in last60), default=current)
    failed_breakdown_reclaim = bool(prior_low > 0 and last60_low < prior_low * 0.995 and current > prior_low * 1.01)
    failed_breakout_reject = bool(prior_high > 0 and last60_high > prior_high * 1.005 and current < prior_high * 0.99)

    range_contraction_180_365 = s180["range_pct"] / s365["range_pct"] if s365["range_pct"] > 1e-12 else 1.0
    range_contraction_180_long = s180["range_pct"] / slong["range_pct"] if slong["range_pct"] > 1e-12 else 1.0

    rs90 = _relative_strength(valid, btc_rows, 90)
    rs365 = _relative_strength(valid, btc_rows, 365)

    evidence_acc: list[str] = []
    evidence_dist: list[str] = []
    accumulation = 10.0
    distribution = 10.0

    if len(valid) >= 365:
        accumulation += 8; distribution += 8
    if len(valid) >= 730:
        accumulation += 6; distribution += 6
    if len(valid) >= 1000:
        accumulation += 4; distribution += 4

    if range_contraction_180_365 <= 0.60:
        accumulation += 12; distribution += 10
        evidence_acc.append("180d_range_contracting_vs_1y")
        evidence_dist.append("180d_range_contracting_vs_1y")
    if len(valid) >= 730 and range_contraction_180_long <= 0.42:
        accumulation += 10; distribution += 8
        evidence_acc.append("long_base_compression")
        evidence_dist.append("long_top_compression")

    if volatility_contraction <= 0.82:
        accumulation += 10; distribution += 10
        evidence_acc.append("daily_volatility_contraction")
        evidence_dist.append("daily_volatility_contraction")
    if volume_ratio <= 0.82:
        accumulation += 8; distribution += 8
        evidence_acc.append("volume_dry_up")
        evidence_dist.append("volume_dry_up")

    if higher_lows:
        accumulation += 12
        evidence_acc.append("multi_month_higher_lows")
    if lower_highs:
        distribution += 12
        evidence_dist.append("multi_month_lower_highs")
    if failed_breakdown_reclaim:
        accumulation += 14
        evidence_acc.append("failed_breakdown_reclaimed")
    if failed_breakout_reject:
        distribution += 14
        evidence_dist.append("failed_breakout_rejected")

    if obv_bias >= 0.08 and abs(s90["return_pct"]) <= 25:
        accumulation += 10
        evidence_acc.append("obv_rising_while_price_basing")
    if obv_bias <= -0.08 and abs(s90["return_pct"]) <= 25:
        distribution += 10
        evidence_dist.append("obv_falling_while_price_flat")

    if 0.45 <= s180["position"] <= 0.90:
        accumulation += 7
    if 0.10 <= s180["position"] <= 0.55:
        distribution += 7
    if s180["position"] >= 0.72 and higher_lows:
        accumulation += 8
        evidence_acc.append("pressure_near_long_range_resistance")
    if s180["position"] <= 0.28 and lower_highs:
        distribution += 8
        evidence_dist.append("pressure_near_long_range_support")

    if rs90 is not None and rs90 >= 8:
        accumulation += 8
        evidence_acc.append("relative_strength_vs_btc_improving")
    elif rs90 is not None and rs90 <= -8:
        distribution += 6
        evidence_dist.append("relative_weakness_vs_btc")

    accumulation = _clip(accumulation)
    distribution = _clip(distribution)

    markup = 0.0
    markdown = 0.0
    if ema50 > 0 and current > ema50:
        markup += 20
    if ema200 > 0 and ema50 > ema200 and current > ema200:
        markup += 25
    if s90["return_pct"] >= 15:
        markup += min(30.0, s90["return_pct"] * 0.6)
    if rs90 is not None and rs90 > 0:
        markup += min(20.0, rs90 * 0.5)

    if ema50 > 0 and current < ema50:
        markdown += 20
    if ema200 > 0 and ema50 < ema200 and current < ema200:
        markdown += 25
    if s90["return_pct"] <= -15:
        markdown += min(30.0, abs(s90["return_pct"]) * 0.6)
    if rs90 is not None and rs90 < 0:
        markdown += min(20.0, abs(rs90) * 0.5)

    markup = _clip(markup)
    markdown = _clip(markdown)

    if accumulation >= 70 and s180["position"] >= 0.68:
        state = "ACCUMULATION_LATE"
        bias = "LONG"
        confidence = accumulation
    elif accumulation >= 62 and accumulation >= distribution + 8:
        state = "ACCUMULATION"
        bias = "LONG"
        confidence = accumulation
    elif distribution >= 70 and s180["position"] <= 0.32:
        state = "DISTRIBUTION_LATE"
        bias = "SHORT"
        confidence = distribution
    elif distribution >= 62 and distribution >= accumulation + 8:
        state = "DISTRIBUTION"
        bias = "SHORT"
        confidence = distribution
    elif markup >= 62 and markup >= markdown + 10:
        state = "MARKUP"
        bias = "LONG"
        confidence = markup
    elif markdown >= 62 and markdown >= markup + 10:
        state = "MARKDOWN"
        bias = "SHORT"
        confidence = markdown
    else:
        state = "RANGE_OR_TRANSITION"
        if accumulation - distribution >= 15:
            bias = "LONG"
            confidence = accumulation
        elif distribution - accumulation >= 15:
            bias = "SHORT"
            confidence = distribution
        else:
            bias = "NEUTRAL"
            confidence = max(accumulation, distribution, markup, markdown)

    long_base_candidate = bool(
        len(valid) >= 730
        and range_contraction_180_long <= 0.50
        and volatility_contraction <= 0.90
        and accumulation >= 58
    )

    return {
        "version": VERSION,
        "available": True,
        "source": source,
        "history_days": len(valid),
        "history_years_approx": round(len(valid) / 365.25, 2),
        "complete_3y": len(valid) >= 1000,
        "state": state,
        "bias": bias,
        "confidence_score": round(_clip(confidence), 1),
        "score_is_probability": False,
        "accumulation_score": round(accumulation, 1),
        "distribution_score": round(distribution, 1),
        "markup_score": round(markup, 1),
        "markdown_score": round(markdown, 1),
        "long_base_candidate": long_base_candidate,
        "windows": windows,
        "features": {
            "atr20_pct": round(atr20, 4),
            "atr90_pct": round(atr90, 4),
            "volatility_contraction_ratio": round(volatility_contraction, 4),
            "volume_30d_vs_prior_ratio": round(volume_ratio, 4),
            "range_180d_vs_365d_ratio": round(range_contraction_180_365, 4),
            "range_180d_vs_long_ratio": round(range_contraction_180_long, 4),
            "position_in_180d_range": round(s180["position"], 4),
            "ema50": round(ema50, 12),
            "ema200": round(ema200, 12) if ema200 else None,
            "obv_bias": round(obv_bias, 4),
            "higher_lows": higher_lows,
            "lower_highs": lower_highs,
            "failed_breakdown_reclaim": failed_breakdown_reclaim,
            "failed_breakout_reject": failed_breakout_reject,
            "relative_strength_90d_vs_btc_pct": round(rs90, 3) if rs90 is not None else None,
            "relative_strength_365d_vs_btc_pct": round(rs365, 3) if rs365 is not None else None,
        },
        "accumulation_evidence": evidence_acc,
        "distribution_evidence": evidence_dist,
        "suggested_watch_horizon": "3D_14D" if long_base_candidate or state in {"ACCUMULATION_LATE", "DISTRIBUTION_LATE"} else "1D_7D",
        "can_create_entry": False,
        "can_override_hard_safety": False,
        "role": "slow macro context and long-base discovery; timing still comes from lower timeframes",
    }
