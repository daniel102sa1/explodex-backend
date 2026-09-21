from __future__ import annotations

from typing import Any

VERSION = "sarpon_knowledge_v1"

# The user explicitly confirmed these as core SARPON references. This registry is
# intentionally extensible: additional authors/books can be added later only
# when we have direct evidence from SARPON material, screenshots, notes or books.
CONFIRMED_SOURCES = [
    {
        "author": "John J. Murphy",
        "role": "technical_structure",
        "status": "USER_CONFIRMED",
        "concepts": [
            "trend_and_market_structure",
            "support_and_resistance",
            "trendlines",
            "breakout_and_retest",
            "moving_average_context",
            "volume_confirmation",
        ],
    },
    {
        "author": "Steve Nison",
        "role": "japanese_candlesticks",
        "status": "USER_CONFIRMED",
        "concepts": [
            "candlestick_context",
            "rejection_wicks",
            "engulfing_patterns",
            "hammer_family",
            "shooting_star_family",
            "doji_indecision",
        ],
    },
]


def sarpon_knowledge_registry() -> dict[str, Any]:
    return {
        "version": VERSION,
        "framework": "SARPON_CLASSIC",
        "confirmed_sources": CONFIRMED_SOURCES,
        "pending_sources": True,
        "pending_source_rule": (
            "Do not invent additional SARPON authors/books. Add them only when the user "
            "provides direct evidence, screenshots, notes, titles or source material."
        ),
        "implementation_note": (
            "Pattern rules below are transparent heuristics inspired by the confirmed "
            "frameworks; they are not verbatim reproductions of any book."
        ),
        "user_observed_rules": [
            {
                "rule": "COMPRESSION_PRIORITY",
                "status": "USER_REPORTED_FROM_SARPON",
                "meaning": "Clean compression/pressure structure deserves extra weight for early detection, before late breakout confirmation.",
                "not_attributed_to_book": True,
            }
        ],
        "risk_rule": (
            "SARPON evidence may confirm/downgrade an entry before execution. It never "
            "widens a live stop and never creates a trade by itself."
        ),
    }


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
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
    return sum(sample) / len(sample) if sample else 0.0


def _wick(row: list[Any]) -> dict[str, float]:
    if len(row) < 5:
        return {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "body": 0.0, "range": 0.0, "upper": 0.0, "lower": 0.0}
    open_, high, low, close = map(_f, [row[1], row[2], row[3], row[4]])
    body = abs(close - open_)
    total = max(0.0, high - low)
    upper = max(0.0, high - max(open_, close))
    lower = max(0.0, min(open_, close) - low)
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "body": body,
        "range": total,
        "upper": upper,
        "lower": lower,
    }


def _candlestick_context(rows: list[list[Any]], side: str) -> dict[str, Any]:
    if len(rows) < 2:
        return {"available": False, "score": 50.0, "confirmed": False, "patterns": []}

    prev = _wick(rows[-2])
    last = _wick(rows[-1])
    if last["range"] <= 0:
        return {"available": False, "score": 50.0, "confirmed": False, "patterns": []}

    bull_last = last["close"] > last["open"]
    bear_last = last["close"] < last["open"]
    bull_prev = prev["close"] > prev["open"]
    bear_prev = prev["close"] < prev["open"]

    bullish_engulf = (
        bull_last and bear_prev
        and last["open"] <= prev["close"]
        and last["close"] >= prev["open"]
    )
    bearish_engulf = (
        bear_last and bull_prev
        and last["open"] >= prev["close"]
        and last["close"] <= prev["open"]
    )

    body_floor = max(last["body"], last["range"] * 0.05)
    hammer_like = (
        last["lower"] >= body_floor * 2.0
        and last["lower"] >= last["range"] * 0.42
        and last["upper"] <= last["range"] * 0.25
    )
    shooting_star_like = (
        last["upper"] >= body_floor * 2.0
        and last["upper"] >= last["range"] * 0.42
        and last["lower"] <= last["range"] * 0.25
    )
    doji = last["body"] <= last["range"] * 0.16
    bullish_rejection = last["lower"] >= max(body_floor * 1.5, last["range"] * 0.34) and last["close"] >= last["open"]
    bearish_rejection = last["upper"] >= max(body_floor * 1.5, last["range"] * 0.34) and last["close"] <= last["open"]

    patterns: list[str] = []
    aligned = False
    opposite = False
    score = 45.0

    if side == "LONG":
        if bullish_engulf:
            patterns.append("BULLISH_ENGULFING_HEURISTIC")
            score += 28.0
            aligned = True
        if hammer_like:
            patterns.append("HAMMER_FAMILY_HEURISTIC")
            score += 22.0
            aligned = True
        elif bullish_rejection:
            patterns.append("LOWER_WICK_REJECTION")
            score += 16.0
            aligned = True
        if bearish_engulf or shooting_star_like:
            opposite = True
            score -= 28.0
    else:
        if bearish_engulf:
            patterns.append("BEARISH_ENGULFING_HEURISTIC")
            score += 28.0
            aligned = True
        if shooting_star_like:
            patterns.append("SHOOTING_STAR_FAMILY_HEURISTIC")
            score += 22.0
            aligned = True
        elif bearish_rejection:
            patterns.append("UPPER_WICK_REJECTION")
            score += 16.0
            aligned = True
        if bullish_engulf or hammer_like:
            opposite = True
            score -= 28.0

    if doji:
        patterns.append("DOJI_INDECISION_HEURISTIC")
        score = min(score, 58.0)

    score = max(0.0, min(100.0, score))
    return {
        "available": True,
        "source_family": "STEVE_NISON_JAPANESE_CANDLESTICKS",
        "implementation": "HEURISTIC_NOT_VERBATIM_BOOK_RULE",
        "score": round(score, 2),
        "confirmed": aligned and not opposite and not doji,
        "opposite_signal": opposite,
        "patterns": patterns,
        "last_candle": {
            "body_to_range": round(last["body"] / last["range"], 4) if last["range"] else None,
            "upper_wick_to_range": round(last["upper"] / last["range"], 4) if last["range"] else None,
            "lower_wick_to_range": round(last["lower"] / last["range"], 4) if last["range"] else None,
        },
    }


def _block_extrema(values: list[float], *, blocks: int = 3, block_size: int = 6, mode: str = "max") -> list[float]:
    needed = blocks * block_size
    if len(values) < needed:
        return []
    chunk = values[-needed:]
    out: list[float] = []
    for i in range(blocks):
        part = chunk[i * block_size:(i + 1) * block_size]
        out.append(max(part) if mode == "max" else min(part))
    return out


def _murphy_structure(rows: list[list[Any]], side: str, prediction: dict[str, Any]) -> dict[str, Any]:
    usable = [row for row in rows if len(row) >= 5][-72:]
    if len(usable) < 24:
        return {"available": False, "score": 50.0, "aligned": False, "retest": False}

    highs = [_f(row[2]) for row in usable]
    lows = [_f(row[3]) for row in usable]
    closes = [_f(row[4]) for row in usable]
    current = closes[-1]
    atr = _atr(usable)
    ema9 = _ema(closes, 9)[-1]
    ema21 = _ema(closes, 21)[-1]

    block_highs = _block_extrema(highs[:-1], mode="max")
    block_lows = _block_extrema(lows[:-1], mode="min")
    lower_highs = len(block_highs) >= 3 and block_highs[1] <= block_highs[0] * 1.002 and block_highs[2] < block_highs[1] * 1.002
    higher_lows = len(block_lows) >= 3 and block_lows[1] >= block_lows[0] * 0.998 and block_lows[2] > block_lows[1] * 0.998

    ema_aligned = (side == "LONG" and ema9 >= ema21) or (side == "SHORT" and ema9 <= ema21)
    structure_aligned = higher_lows if side == "LONG" else lower_highs

    entry_low = _f(prediction.get("entry_low"))
    entry_high = _f(prediction.get("entry_high"))
    trigger = _f(prediction.get("trigger_price"))
    in_entry_zone = (
        min(entry_low, entry_high) <= current <= max(entry_low, entry_high)
        if entry_low > 0 and entry_high > 0
        else False
    )
    near_trigger = bool(trigger > 0 and atr > 0 and abs(current - trigger) <= atr * 0.50)
    sequence = prediction.get("sequence") if isinstance(prediction.get("sequence"), dict) else {}
    no_chase = not bool(sequence.get("chase_risk"))
    retest = bool(no_chase and (in_entry_zone or near_trigger))

    invalidation = _f(prediction.get("invalidation_price"), _f(prediction.get("stop_loss")))
    invalidated = bool(
        invalidation > 0 and (
            (side == "LONG" and current <= invalidation)
            or (side == "SHORT" and current >= invalidation)
        )
    )

    score = 35.0
    if structure_aligned:
        score += 30.0
    if ema_aligned:
        score += 18.0
    if retest:
        score += 17.0
    if not no_chase:
        score -= 28.0
    if invalidated:
        score = min(score, 10.0)

    return {
        "available": True,
        "source_family": "JOHN_J_MURPHY_TECHNICAL_STRUCTURE",
        "implementation": "TRANSPARENT_HEURISTIC",
        "score": round(max(0.0, min(100.0, score)), 2),
        "aligned": bool(structure_aligned and ema_aligned),
        "structure_aligned": structure_aligned,
        "ema_aligned": ema_aligned,
        "lower_highs": lower_highs,
        "higher_lows": higher_lows,
        "retest": retest,
        "in_entry_zone": in_entry_zone,
        "near_trigger": near_trigger,
        "no_chase": no_chase,
        "invalidated": invalidated,
        "ema9": round(ema9, 12),
        "ema21": round(ema21, 12),
        "atr": round(atr, 12),
        "structural_invalidation": invalidation or None,
    }


def build_sarpon_classic_context(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    prediction: dict[str, Any],
) -> dict[str, Any]:
    side = str(prediction.get("direction") or scored.get("direction") or "").upper()
    rows = snapshot.get("klines") if isinstance(snapshot, dict) else None
    rows = rows if isinstance(rows, list) else []
    if side not in {"LONG", "SHORT"} or len(rows) < 24:
        return {
            "version": VERSION,
            "available": False,
            "stage": "NO_DATA",
            "entry_gate": "WAIT",
            "score_is_probability": False,
            "knowledge": sarpon_knowledge_registry(),
        }

    murphy = _murphy_structure(rows, side, prediction)
    nison = _candlestick_context(rows, side)
    compression = prediction.get("sarpon_compression") if isinstance(prediction.get("sarpon_compression"), dict) else {}
    compression_available = bool(compression.get("available"))
    compression_stage = str(compression.get("stage") or "NO_COMPRESSION_EDGE").upper()
    compression_direction = str(compression.get("direction") or "NEUTRAL").upper()
    compression_aligned = (
        compression_available
        and compression_stage in {"ARMED_EARLY", "BUILDING"}
        and compression_direction == side
    )
    if compression_available:
        score = (
            _f(murphy.get("score"), 50.0) * 0.48
            + _f(nison.get("score"), 50.0) * 0.27
            + _f(compression.get("early_score"), 50.0) * 0.25
        )
    else:
        score = _f(murphy.get("score"), 50.0) * 0.62 + _f(nison.get("score"), 50.0) * 0.38

    murphy_aligned = bool(murphy.get("aligned"))
    retest = bool(murphy.get("retest"))
    candle_confirmed = bool(nison.get("confirmed"))
    invalidated = bool(murphy.get("invalidated"))
    opposite_candle = bool(nison.get("opposite_signal"))

    if invalidated or (opposite_candle and not murphy_aligned):
        stage = "RED_INVALIDATED"
        entry_gate = "BLOCK"
    elif (murphy_aligned or compression_aligned) and retest and candle_confirmed:
        stage = "GREEN_CONFIRMATION"
        entry_gate = "ALLOW_SUPPORT"
    elif murphy_aligned or compression_aligned or retest or candle_confirmed:
        stage = "YELLOW_FORMING"
        entry_gate = "WAIT"
    else:
        stage = "NO_SETUP"
        entry_gate = "WAIT"

    return {
        "version": VERSION,
        "available": True,
        "side": side,
        "stage": stage,
        "entry_gate": entry_gate,
        "score": round(max(0.0, min(100.0, score)), 2),
        "score_is_probability": False,
        "murphy": murphy,
        "nison": nison,
        "compression_priority": compression if compression_available else {
            "available": False,
            "stage": "NO_DATA",
        },
        "plan_geometry": {
            "entry_low": prediction.get("entry_low"),
            "entry_high": prediction.get("entry_high"),
            "trigger": prediction.get("trigger_price"),
            "invalidation": prediction.get("invalidation_price"),
            "stop_loss": prediction.get("stop_loss"),
            "tp1": prediction.get("tp1"),
            "tp2": prediction.get("tp2"),
            "tp3": prediction.get("tp3"),
        },
        "rules": {
            "structure_before_candle": True,
            "compression_priority_for_early_detection": True,
            "compression_can_form_setup_before_breakout": True,
            "compression_cannot_authorize_entry_alone": True,
            "retest_before_entry": True,
            "candle_confirmation_required_when_available": True,
            "preactivation_is_not_entry": True,
            "never_chase": True,
            "never_widen_live_stop": True,
            "cannot_create_entry_alone": True,
            "can_downgrade_entry": True,
        },
        "knowledge": sarpon_knowledge_registry(),
        "note": (
            "SARPON Classic combines Murphy-style structure with Nison-style candle confirmation. "
            "Compression receives extra EARLY-detection weight from the user-reported SARPON rule, "
            "without claiming it comes from Murphy/Nison or from any undisclosed private method. "
            "Additional books/authors remain pending until directly evidenced."
        ),
    }
