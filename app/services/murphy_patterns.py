from __future__ import annotations

from statistics import mean
from typing import Any

VERSION = "murphy_pattern_library_v1"
MIN_SHADOW_SAMPLE = 30


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
    out = values[0]
    for value in values[1:]:
        out = alpha * value + (1.0 - alpha) * out
    return out


def _atr(rows: list[list[Any]], period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    sample = trs[-period:]
    return mean(sample) if sample else 0.0


def _slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_bar = (n - 1) / 2.0
    y_bar = mean(values)
    denom = sum((i - x_bar) ** 2 for i in range(n))
    if denom <= 1e-12:
        return 0.0
    return sum((i - x_bar) * (y - y_bar) for i, y in enumerate(values)) / denom


def _normalized_slope(values: list[float], scale: float) -> float:
    if scale <= 1e-12:
        return 0.0
    return _slope(values) / scale


def _pivots(values: list[float], *, mode: str, left: int = 2, right: int = 2) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for i in range(left, len(values) - right):
        window = values[i - left : i + right + 1]
        value = values[i]
        if mode == "high" and value >= max(window):
            out.append((i, value))
        elif mode == "low" and value <= min(window):
            out.append((i, value))
    return out


def _similar(a: float, b: float, tolerance_pct: float) -> bool:
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base * 100.0 <= tolerance_pct


def _volume_ratio(rows: list[list[Any]], recent: int = 4, baseline: int = 20) -> float:
    volumes = [_f(r[7] if len(r) > 7 else r[5]) for r in rows]
    if len(volumes) < recent + 2:
        return 1.0
    recent_avg = mean(volumes[-recent:])
    base_slice = volumes[-(baseline + recent):-recent] if len(volumes) >= baseline + recent else volumes[:-recent]
    base_avg = mean(base_slice) if base_slice else recent_avg
    return recent_avg / base_avg if base_avg > 1e-12 else 1.0


def murphy_pattern_registry() -> dict[str, Any]:
    return {
        "version": VERSION,
        "source_family": "JOHN_J_MURPHY_TECHNICAL_STRUCTURE",
        "implementation": "TRANSPARENT_HEURISTICS_NOT_VERBATIM_BOOK_RULES",
        "research_only": True,
        "patterns": [
            {
                "name": "ASCENDING_TRIANGLE",
                "family": "CONTINUATION_OR_BREAKOUT",
                "logic": "relatively flat resistance + rising lows + contraction",
            },
            {
                "name": "DESCENDING_TRIANGLE",
                "family": "CONTINUATION_OR_BREAKDOWN",
                "logic": "relatively flat support + falling highs + contraction",
            },
            {
                "name": "SYMMETRICAL_TRIANGLE",
                "family": "NEUTRAL_UNTIL_BREAK",
                "logic": "falling highs + rising lows + converging range",
            },
            {
                "name": "RECTANGLE",
                "family": "CONSOLIDATION",
                "logic": "flat support + flat resistance + repeated containment",
            },
            {
                "name": "BULL_FLAG_OR_PENNANT",
                "family": "CONTINUATION",
                "logic": "sharp prior rise + tight/contracting consolidation",
            },
            {
                "name": "BEAR_FLAG_OR_PENNANT",
                "family": "CONTINUATION",
                "logic": "sharp prior fall + tight/contracting consolidation",
            },
            {
                "name": "DOUBLE_TOP",
                "family": "REVERSAL",
                "logic": "two similar swing highs with intervening trough",
            },
            {
                "name": "DOUBLE_BOTTOM",
                "family": "REVERSAL",
                "logic": "two similar swing lows with intervening peak",
            },
            {
                "name": "HEAD_AND_SHOULDERS",
                "family": "REVERSAL",
                "logic": "three swing highs with higher central head and similar shoulders",
            },
            {
                "name": "INVERSE_HEAD_AND_SHOULDERS",
                "family": "REVERSAL",
                "logic": "three swing lows with lower central head and similar shoulders",
            },
            {
                "name": "SUPPORT_RESISTANCE_ROLE_REVERSAL",
                "family": "RETEST",
                "logic": "broken level later acts as support/resistance",
            },
        ],
        "confirmation_principles": {
            "trend_context_matters": True,
            "support_resistance_context_matters": True,
            "trendline_context_matters": True,
            "volume_is_confirmation_not_standalone_entry": True,
            "moving_average_context_is_confirmation": True,
            "multi_timeframe_context_is_preferred": True,
        },
        "policy": {
            "can_create_entry": False,
            "can_veto_entry": False,
            "can_raise_leverage": False,
            "can_move_live_stop": False,
            "minimum_shadow_sample_before_promotion": MIN_SHADOW_SAMPLE,
        },
    }


def build_murphy_pattern_context(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    prediction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [r for r in (snapshot.get("klines") or []) if isinstance(r, list) and len(r) >= 8][-120:]
    if len(rows) < 40:
        return {
            "version": VERSION,
            "available": False,
            "research_only": True,
            "reason": "insufficient_klines",
            "registry": murphy_pattern_registry(),
        }

    prediction = dict(prediction or {})
    closes = [_f(r[4]) for r in rows]
    highs = [_f(r[2]) for r in rows]
    lows = [_f(r[3]) for r in rows]
    current = closes[-1]
    atr = _atr(rows)
    atr_pct = atr / current * 100.0 if current > 0 and atr > 0 else _f((scored.get("metrics") or {}).get("atr_pct"), 0.5)
    scale = max(atr, current * 0.001)

    window = 30
    wh = highs[-window:]
    wl = lows[-window:]
    wc = closes[-window:]
    high_slope_atr = _normalized_slope(wh, scale)
    low_slope_atr = _normalized_slope(wl, scale)
    close_slope_atr = _normalized_slope(wc, scale)

    high_band = max(wh) - min(wh)
    low_band = max(wl) - min(wl)
    range_width = max(wh) - min(wl)
    flat_threshold = max(scale * 1.10, current * 0.0035)
    resistance_flat = high_band <= flat_threshold
    support_flat = low_band <= flat_threshold
    rising_lows = low_slope_atr >= 0.035
    falling_highs = high_slope_atr <= -0.035

    first_half_range = max(highs[-30:-15]) - min(lows[-30:-15])
    second_half_range = max(highs[-15:]) - min(lows[-15:])
    range_contraction = second_half_range / first_half_range if first_half_range > 1e-12 else 1.0
    contracting = range_contraction <= 0.78

    ema20 = _ema(closes[-60:], 20)
    ema50 = _ema(closes[-90:], 50)
    trend_context = "UP" if ema20 > ema50 and close_slope_atr > 0 else "DOWN" if ema20 < ema50 and close_slope_atr < 0 else "MIXED"

    recent_volume_ratio = _volume_ratio(rows, recent=4, baseline=20)
    consolidation_volume_ratio = _volume_ratio(rows[-24:], recent=6, baseline=12)
    volume_dry = consolidation_volume_ratio <= 0.82

    patterns: list[dict[str, Any]] = []

    def add(
        name: str,
        *,
        bias: str,
        confidence: float,
        state: str,
        evidence: list[str],
        level: float | None = None,
        confirmed: bool = False,
    ) -> None:
        patterns.append({
            "name": name,
            "bias": bias,
            "confidence_score": round(_clip(confidence), 2),
            "score_is_probability": False,
            "state": state,
            "confirmed": bool(confirmed),
            "level": round(level, 12) if level and level > 0 else None,
            "evidence": evidence,
        })

    # Confirmation must break a level that existed *before* the current bar.
    # Including the current high/low in the boundary made "near the high" look
    # like a confirmed breakout. Keep FORMING separate from real close-through.
    prior_resistance = max(highs[-25:-1])
    prior_support = min(lows[-25:-1])
    breakout_up = current > prior_resistance + scale * 0.08
    breakout_down = current < prior_support - scale * 0.08

    if resistance_flat and rising_lows and contracting:
        score = 60 + min(18, low_slope_atr * 80) + (8 if volume_dry else 0)
        add(
            "ASCENDING_TRIANGLE",
            bias="LONG",
            confidence=score,
            state="BREAKOUT_CONFIRMED" if breakout_up else "FORMING",
            confirmed=breakout_up and recent_volume_ratio >= 1.15,
            level=prior_resistance,
            evidence=[
                "flat_resistance",
                "rising_lows",
                "range_contracting",
                *(["volume_dry_up"] if volume_dry else []),
                *(["breakout_volume_expanding"] if breakout_up and recent_volume_ratio >= 1.15 else []),
            ],
        )

    if support_flat and falling_highs and contracting:
        score = 60 + min(18, abs(high_slope_atr) * 80) + (8 if volume_dry else 0)
        add(
            "DESCENDING_TRIANGLE",
            bias="SHORT",
            confidence=score,
            state="BREAKDOWN_CONFIRMED" if breakout_down else "FORMING",
            confirmed=breakout_down and recent_volume_ratio >= 1.15,
            level=prior_support,
            evidence=[
                "flat_support",
                "falling_highs",
                "range_contracting",
                *(["volume_dry_up"] if volume_dry else []),
                *(["breakdown_volume_expanding"] if breakout_down and recent_volume_ratio >= 1.15 else []),
            ],
        )

    if falling_highs and rising_lows and contracting and not resistance_flat and not support_flat:
        bias = "LONG" if str(prediction.get("direction") or "").upper() == "LONG" else "SHORT" if str(prediction.get("direction") or "").upper() == "SHORT" else "NEUTRAL"
        add(
            "SYMMETRICAL_TRIANGLE",
            bias=bias,
            confidence=62 + (8 if volume_dry else 0),
            state="FORMING",
            confirmed=False,
            evidence=[
                "falling_highs",
                "rising_lows",
                "range_contracting",
                "direction_unresolved_until_break",
                *(["volume_dry_up"] if volume_dry else []),
            ],
        )

    if resistance_flat and support_flat and range_width <= max(scale * 7.0, current * 0.025):
        add(
            "RECTANGLE",
            bias="NEUTRAL",
            confidence=58 + (8 if volume_dry else 0),
            state="FORMING",
            evidence=[
                "flat_resistance",
                "flat_support",
                "range_containment",
                *(["volume_dry_up"] if volume_dry else []),
            ],
        )

    # Flag / pennant heuristic: compare the impulse immediately before the final
    # 12-bar consolidation to that consolidation's width and slope.
    pre = rows[-32:-12]
    cons = rows[-12:]
    if len(pre) >= 12 and len(cons) >= 10:
        pre_open = _f(pre[0][1])
        pre_close = _f(pre[-1][4])
        impulse_pct = (pre_close - pre_open) / pre_open * 100.0 if pre_open > 0 else 0.0
        cons_highs = [_f(r[2]) for r in cons]
        cons_lows = [_f(r[3]) for r in cons]
        cons_closes = [_f(r[4]) for r in cons]
        cons_width = max(cons_highs) - min(cons_lows)
        cons_width_atr = cons_width / scale
        cons_slope = _normalized_slope(cons_closes, scale)
        cons_volume_ratio = _volume_ratio(rows[-30:], recent=8, baseline=14)
        if impulse_pct >= max(1.0, atr_pct * 3.0) and cons_width_atr <= 4.5 and cons_slope <= 0.10:
            add(
                "BULL_FLAG_OR_PENNANT",
                bias="LONG",
                confidence=64 + min(16, impulse_pct * 2.0) + (8 if cons_volume_ratio <= 0.85 else 0),
                state="FORMING",
                evidence=[
                    "sharp_prior_up_impulse",
                    "tight_post_impulse_consolidation",
                    *(["consolidation_slopes_against_trend"] if cons_slope < -0.02 else []),
                    *(["volume_contracts_in_consolidation"] if cons_volume_ratio <= 0.85 else []),
                ],
            )
        elif impulse_pct <= -max(1.0, atr_pct * 3.0) and cons_width_atr <= 4.5 and cons_slope >= -0.10:
            add(
                "BEAR_FLAG_OR_PENNANT",
                bias="SHORT",
                confidence=64 + min(16, abs(impulse_pct) * 2.0) + (8 if cons_volume_ratio <= 0.85 else 0),
                state="FORMING",
                evidence=[
                    "sharp_prior_down_impulse",
                    "tight_post_impulse_consolidation",
                    *(["consolidation_slopes_against_trend"] if cons_slope > 0.02 else []),
                    *(["volume_contracts_in_consolidation"] if cons_volume_ratio <= 0.85 else []),
                ],
            )

    piv_high = _pivots(highs[-72:], mode="high")
    piv_low = _pivots(lows[-72:], mode="low")

    if len(piv_high) >= 2:
        a, b = piv_high[-2], piv_high[-1]
        if b[0] - a[0] >= 5 and _similar(a[1], b[1], max(0.45, atr_pct * 1.25)):
            between = lows[-72:][a[0]:b[0] + 1]
            trough = min(between) if between else 0.0
            confirmed = trough > 0 and current < trough
            add(
                "DOUBLE_TOP",
                bias="SHORT",
                confidence=62 + (12 if confirmed else 0),
                state="NECKLINE_BROKEN" if confirmed else "FORMING",
                confirmed=confirmed,
                level=trough if trough > 0 else None,
                evidence=["similar_swing_highs", "intervening_trough", *(["neckline_broken"] if confirmed else [])],
            )

    if len(piv_low) >= 2:
        a, b = piv_low[-2], piv_low[-1]
        if b[0] - a[0] >= 5 and _similar(a[1], b[1], max(0.45, atr_pct * 1.25)):
            between = highs[-72:][a[0]:b[0] + 1]
            peak = max(between) if between else 0.0
            confirmed = peak > 0 and current > peak
            add(
                "DOUBLE_BOTTOM",
                bias="LONG",
                confidence=62 + (12 if confirmed else 0),
                state="NECKLINE_BROKEN" if confirmed else "FORMING",
                confirmed=confirmed,
                level=peak if peak > 0 else None,
                evidence=["similar_swing_lows", "intervening_peak", *(["neckline_broken"] if confirmed else [])],
            )

    if len(piv_high) >= 3:
        s1, head, s2 = piv_high[-3], piv_high[-2], piv_high[-1]
        shoulders_similar = _similar(s1[1], s2[1], max(0.75, atr_pct * 1.8))
        head_higher = head[1] > max(s1[1], s2[1]) + scale * 0.35
        separated = s1[0] < head[0] < s2[0] and (head[0] - s1[0] >= 4) and (s2[0] - head[0] >= 4)
        if shoulders_similar and head_higher and separated:
            low1 = min(lows[-72:][s1[0]:head[0] + 1])
            low2 = min(lows[-72:][head[0]:s2[0] + 1])
            neckline = (low1 + low2) / 2.0
            confirmed = current < neckline
            add(
                "HEAD_AND_SHOULDERS",
                bias="SHORT",
                confidence=68 + (14 if confirmed else 0),
                state="NECKLINE_BROKEN" if confirmed else "FORMING",
                confirmed=confirmed,
                level=neckline,
                evidence=["three_swing_highs", "higher_central_head", "similar_shoulders", *(["neckline_broken"] if confirmed else [])],
            )

    if len(piv_low) >= 3:
        s1, head, s2 = piv_low[-3], piv_low[-2], piv_low[-1]
        shoulders_similar = _similar(s1[1], s2[1], max(0.75, atr_pct * 1.8))
        head_lower = head[1] < min(s1[1], s2[1]) - scale * 0.35
        separated = s1[0] < head[0] < s2[0] and (head[0] - s1[0] >= 4) and (s2[0] - head[0] >= 4)
        if shoulders_similar and head_lower and separated:
            high1 = max(highs[-72:][s1[0]:head[0] + 1])
            high2 = max(highs[-72:][head[0]:s2[0] + 1])
            neckline = (high1 + high2) / 2.0
            confirmed = current > neckline
            add(
                "INVERSE_HEAD_AND_SHOULDERS",
                bias="LONG",
                confidence=68 + (14 if confirmed else 0),
                state="NECKLINE_BROKEN" if confirmed else "FORMING",
                confirmed=confirmed,
                level=neckline,
                evidence=["three_swing_lows", "lower_central_head", "similar_shoulders", *(["neckline_broken"] if confirmed else [])],
            )

    # Role reversal: look for a level broken several bars ago and now retested
    # within roughly half an ATR while remaining on the new side.
    old_high = max(highs[-36:-12])
    old_low = min(lows[-36:-12])
    near_old_high = abs(current - old_high) <= scale * 0.55
    near_old_low = abs(current - old_low) <= scale * 0.55
    broke_high = max(closes[-12:-2]) > old_high + scale * 0.15 if len(closes) >= 14 else False
    broke_low = min(closes[-12:-2]) < old_low - scale * 0.15 if len(closes) >= 14 else False
    if broke_high and near_old_high and current >= old_high - scale * 0.15:
        add(
            "SUPPORT_RESISTANCE_ROLE_REVERSAL",
            bias="LONG",
            confidence=72,
            state="RETEST",
            level=old_high,
            evidence=["prior_resistance_broken", "retest_near_old_resistance", "level_holding_as_support"],
        )
    elif broke_low and near_old_low and current <= old_low + scale * 0.15:
        add(
            "SUPPORT_RESISTANCE_ROLE_REVERSAL",
            bias="SHORT",
            confidence=72,
            state="RETEST",
            level=old_low,
            evidence=["prior_support_broken", "retest_near_old_support", "level_holding_as_resistance"],
        )

    patterns.sort(key=lambda x: _f(x.get("confidence_score")), reverse=True)
    top = patterns[0] if patterns else None
    long_score = sum(_f(p.get("confidence_score")) - 50.0 for p in patterns if p.get("bias") == "LONG")
    short_score = sum(_f(p.get("confidence_score")) - 50.0 for p in patterns if p.get("bias") == "SHORT")
    if long_score - short_score >= 18:
        aggregate_bias = "LONG"
    elif short_score - long_score >= 18:
        aggregate_bias = "SHORT"
    else:
        aggregate_bias = "NEUTRAL"

    return {
        "version": VERSION,
        "available": True,
        "research_only": True,
        "aggregate_bias": aggregate_bias,
        "top_pattern": top,
        "patterns": patterns[:8],
        "pattern_count": len(patterns),
        "context": {
            "trend_context": trend_context,
            "ema20": round(ema20, 12),
            "ema50": round(ema50, 12),
            "atr": round(atr, 12),
            "atr_pct": round(atr_pct, 4),
            "recent_volume_ratio": round(recent_volume_ratio, 4),
            "volume_dry": volume_dry,
            "range_contraction_ratio": round(range_contraction, 4),
        },
        "policy": {
            "can_create_entry": False,
            "can_veto_entry": False,
            "can_raise_leverage": False,
            "can_move_live_stop": False,
            "shadow_learning_required": True,
            "minimum_sample_before_promotion": MIN_SHADOW_SAMPLE,
        },
        "registry": murphy_pattern_registry(),
        "note": (
            "These are transparent Murphy-style chart-pattern heuristics used as SHADOW evidence. "
            "They are not next-trade probabilities and do not reproduce any private SARPON method."
        ),
    }
