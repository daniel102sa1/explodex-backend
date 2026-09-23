from __future__ import annotations

from typing import Any

VERSION = "adaptive_profit_trail_v1"

ATR_PERIOD = 14
PIVOT_LOOKBACK = 24
CHANDELIER_LOOKBACK = 22
PIVOT_BUFFER_ATR = 0.25
MIN_HEADROOM_ATR = 1.50
ARM_MFE_R = 1.50
ARM_TP_PROGRESS = 0.55

ATR_MULTIPLIER_BY_STRATEGY = {
    "MICRO_SCALP": 2.20,
    "RANGE_MICRO": 2.20,
    "AGGRESSIVE_PAPER": 2.40,
    "TACTICAL": 2.50,
    "TREND_PREMOVE": 2.60,
    "PRE_EVENT_PAPER": 2.70,
    "STRUCTURE_RETEST_PAPER": 2.70,
    "SWING_PAPER": 3.00,
    "SWING_TRAJECTORY_PAPER": 3.00,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tighten_only(side: str, current_stop: float, candidate: float) -> float:
    side = str(side or "").upper()
    if candidate <= 0:
        return current_stop
    if side == "LONG":
        return max(current_stop, candidate)
    if side == "SHORT":
        return min(current_stop, candidate)
    return current_stop


def _atr(candles: list[list[Any]], period: int = ATR_PERIOD) -> float:
    rows = [row for row in candles if isinstance(row, list) and len(row) >= 5]
    if len(rows) < 3:
        return 0.0
    trs: list[float] = []
    prev_close = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        if min(high, low, close, prev_close) <= 0:
            prev_close = close
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    if not trs:
        return 0.0
    sample = trs[-min(period, len(trs)) :]
    return sum(sample) / len(sample)


def _recent_pivot(candles: list[list[Any]], side: str) -> float | None:
    rows = [row for row in candles[-PIVOT_LOOKBACK:] if isinstance(row, list) and len(row) >= 5]
    if len(rows) < 7:
        return None
    side = str(side or "").upper()
    for i in range(len(rows) - 3, 1, -1):
        left = rows[i - 2 : i]
        right = rows[i + 1 : i + 3]
        if len(left) < 2 or len(right) < 2:
            continue
        if side == "LONG":
            value = _f(rows[i][3])
            if value > 0 and value < min(_f(r[3]) for r in left) and value <= min(_f(r[3]) for r in right):
                return value
        elif side == "SHORT":
            value = _f(rows[i][2])
            if value > 0 and value > max(_f(r[2]) for r in left) and value >= max(_f(r[2]) for r in right):
                return value
    return None


def _mfe_r(*, side: str, entry: float, initial_stop: float, mfe_price: float) -> float:
    initial_risk = abs(entry - initial_stop)
    if initial_risk <= 1e-12:
        return 0.0
    favorable = (mfe_price - entry) if side == "LONG" else (entry - mfe_price)
    return favorable / initial_risk


def _tp_progress(*, side: str, entry: float, tp1: float, mfe_price: float) -> float:
    target_distance = abs(tp1 - entry)
    if target_distance <= 1e-12:
        return 0.0
    favorable = (mfe_price - entry) if side == "LONG" else (entry - mfe_price)
    return favorable / target_distance


def _minimum_profit_floor(*, side: str, entry: float, initial_stop: float, mfe_r: float) -> float:
    """A conservative floor that never replaces the volatility/structure trail.

    It only prevents a mature winner from falling all the way back to the
    original risk. The structure/ATR candidate will usually be tighter.
    """
    initial_risk = abs(entry - initial_stop)
    if initial_risk <= 1e-12:
        return entry
    if mfe_r >= 5.0:
        locked_r = 1.50
    elif mfe_r >= 3.5:
        locked_r = 1.00
    elif mfe_r >= 2.5:
        locked_r = 0.50
    else:
        locked_r = 0.0
    return entry + initial_risk * locked_r if side == "LONG" else entry - initial_risk * locked_r


def build_adaptive_profit_trail(
    *,
    side: str,
    entry: float,
    initial_stop: float,
    current_stop: float,
    tp1: float,
    candles: list[list[Any]],
    strategy_mode: str | None = None,
    prior_mfe_price: float | None = None,
    cost_buffer_rate: float = 0.0018,
) -> dict[str, Any]:
    """Build a completed-candle, volatility-aware trailing stop proposal.

    Research basis: Chandelier-style highest/lowest extreme minus/plus an ATR
    multiple, made more conservative by respecting the latest confirmed pivot
    and a minimum ATR headroom. It never widens an existing stop and cannot
    create or flip a trade.
    """
    side = str(side or "").upper()
    rows = [row for row in candles if isinstance(row, list) and len(row) >= 5]
    if side not in {"LONG", "SHORT"} or min(entry, initial_stop, current_stop) <= 0 or len(rows) < 8:
        return {
            "version": VERSION,
            "available": False,
            "reason": "insufficient_geometry_or_candles",
            "new_stop": current_stop,
            "changed": False,
        }

    last_close = _f(rows[-1][4])
    highs = [_f(row[2]) for row in rows if _f(row[2]) > 0]
    lows = [_f(row[3]) for row in rows if _f(row[3]) > 0]
    if not highs or not lows or last_close <= 0:
        return {
            "version": VERSION,
            "available": False,
            "reason": "invalid_ohlc",
            "new_stop": current_stop,
            "changed": False,
        }

    if side == "LONG":
        observed_mfe = max(highs)
        mfe_price = max(observed_mfe, _f(prior_mfe_price, observed_mfe))
    else:
        observed_mfe = min(lows)
        prior = _f(prior_mfe_price, observed_mfe)
        mfe_price = min(observed_mfe, prior) if prior > 0 else observed_mfe

    trail_rows = rows[-min(CHANDELIER_LOOKBACK, len(rows)) :]
    trail_high = max(_f(row[2]) for row in trail_rows)
    trail_low = min(_f(row[3]) for row in trail_rows)
    atr = _atr(rows)
    mfe_r = _mfe_r(side=side, entry=entry, initial_stop=initial_stop, mfe_price=mfe_price)
    tp_progress = _tp_progress(side=side, entry=entry, tp1=tp1, mfe_price=mfe_price) if tp1 > 0 else 0.0
    armed = mfe_r >= ARM_MFE_R or tp_progress >= ARM_TP_PROGRESS
    if atr <= 0 or not armed:
        return {
            "version": VERSION,
            "available": True,
            "armed": False,
            "reason": "winner_not_mature_enough",
            "mfe_price": mfe_price,
            "mfe_r": round(mfe_r, 4),
            "tp1_progress": round(tp_progress, 4),
            "atr": round(atr, 12),
            "new_stop": current_stop,
            "changed": False,
        }

    strategy = str(strategy_mode or "").upper()
    atr_multiplier = ATR_MULTIPLIER_BY_STRATEGY.get(strategy, 2.70)
    pivot = _recent_pivot(rows, side)

    if side == "LONG":
        chandelier = trail_high - atr_multiplier * atr
        pivot_stop = (pivot - PIVOT_BUFFER_ATR * atr) if pivot else None
        structure_stop = min(chandelier, pivot_stop) if pivot_stop and pivot_stop > 0 else chandelier
        # Never crowd price closer than 1.5 ATR on the first structure trail.
        headroom_cap = last_close - MIN_HEADROOM_ATR * atr
        structure_stop = min(structure_stop, headroom_cap)
        cost_floor = entry * (1.0 + cost_buffer_rate)
        r_floor = _minimum_profit_floor(side=side, entry=entry, initial_stop=initial_stop, mfe_r=mfe_r)
        candidate = max(structure_stop, cost_floor, r_floor)
        epsilon = max(entry * 0.00005, atr * 0.02)
        candidate = min(candidate, last_close - epsilon)
    else:
        chandelier = trail_low + atr_multiplier * atr
        pivot_stop = (pivot + PIVOT_BUFFER_ATR * atr) if pivot else None
        structure_stop = max(chandelier, pivot_stop) if pivot_stop and pivot_stop > 0 else chandelier
        headroom_cap = last_close + MIN_HEADROOM_ATR * atr
        structure_stop = max(structure_stop, headroom_cap)
        cost_floor = entry * (1.0 - cost_buffer_rate)
        r_floor = _minimum_profit_floor(side=side, entry=entry, initial_stop=initial_stop, mfe_r=mfe_r)
        candidate = min(structure_stop, cost_floor, r_floor)
        epsilon = max(entry * 0.00005, atr * 0.02)
        candidate = max(candidate, last_close + epsilon)

    new_stop = _tighten_only(side, current_stop, candidate)
    return {
        "version": VERSION,
        "available": True,
        "armed": True,
        "changed": abs(new_stop - current_stop) > max(1e-12, abs(current_stop) * 1e-10),
        "reason": "chandelier_atr_plus_confirmed_pivot",
        "method": "CHANDELIER_ATR_PLUS_PIVOT",
        "strategy_mode": strategy or None,
        "mfe_price": round(mfe_price, 12),
        "mfe_r": round(mfe_r, 4),
        "tp1_progress": round(tp_progress, 4),
        "atr": round(atr, 12),
        "atr_multiplier": atr_multiplier,
        "pivot": round(pivot, 12) if pivot else None,
        "chandelier_lookback": CHANDELIER_LOOKBACK,
        "chandelier_extreme": round(trail_high if side == "LONG" else trail_low, 12),
        "chandelier_stop": round(chandelier, 12),
        "pivot_stop": round(pivot_stop, 12) if pivot_stop else None,
        "minimum_profit_floor": round(r_floor, 12),
        "cost_floor": round(cost_floor, 12),
        "headroom_atr": MIN_HEADROOM_ATR,
        "old_stop": round(current_stop, 12),
        "new_stop": round(new_stop, 12),
        "never_widens_stop": True,
        "uses_completed_candles_only": True,
        "score_is_probability": False,
    }
