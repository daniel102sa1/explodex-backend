from __future__ import annotations

from typing import Any

from app.services.binance import binance_client

VERSION = "paper_adaptive_risk_v1"


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _atr(rows: list[list[Any]], period: int = 14) -> float:
    usable = [row for row in rows if len(row) >= 5]
    if len(usable) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = _f(usable[0][4])
    for row in usable[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    sample = trs[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def market_direction_guard(side: str, regime: dict[str, Any]) -> dict[str, Any]:
    side = str(side or "").upper()
    name = str(regime.get("regime") or "UNKNOWN").upper()
    btc = regime.get("btc") or {}
    eth = regime.get("eth") or {}
    btc15 = _f(btc.get("ret_15m_pct"))
    eth15 = _f(eth.get("ret_15m_pct"))
    btc60 = _f(btc.get("ret_60m_pct"))
    eth60 = _f(eth.get("ret_60m_pct"))

    dump = (btc15 <= -1.0 and eth15 <= -0.8) or (btc60 <= -2.0 and eth60 <= -1.6)
    pump = (btc15 >= 1.0 and eth15 >= 0.8) or (btc60 >= 2.0 and eth60 >= 1.6)
    strong_down = name == "TREND_DOWN" or dump
    strong_up = name == "TREND_UP" or pump

    if side == "LONG" and strong_down:
        return {"allowed": False, "reason": "blocked_long_into_market_dump", "risk_multiplier": 0.0}
    if side == "SHORT" and strong_up:
        return {"allowed": False, "reason": "blocked_short_into_market_pump", "risk_multiplier": 0.0}
    if side == "SHORT" and strong_down:
        return {"allowed": True, "reason": "aligned_with_downside_pressure", "risk_multiplier": 1.10}
    if side == "LONG" and strong_up:
        return {"allowed": True, "reason": "aligned_with_upside_pressure", "risk_multiplier": 1.10}
    if name == "HIGH_VOLATILITY":
        return {"allowed": True, "reason": "high_volatility_reduce_risk", "risk_multiplier": 0.60}
    return {"allowed": True, "reason": "neutral_market_alignment", "risk_multiplier": 1.0}


def adaptive_leverage(*, grade: str | None, fingerprint_score: float, catalyst_state: str | None, regime_aligned: bool, defensive: bool) -> int:
    grade = str(grade or "").upper()
    catalyst = str(catalyst_state or "").upper()
    score = _f(fingerprint_score)
    if defensive or catalyst in {"CONFLICT", "SHOCK_RISK"}:
        return 1
    if grade == "A+" and score >= 90 and regime_aligned:
        return 6
    if grade == "A+" and score >= 84 and regime_aligned:
        return 5
    if grade in {"A+", "A"} and score >= 78:
        return 4
    if grade in {"A+", "A", "B+"} and score >= 72:
        return 3
    return 2


def adaptive_geometry(*, side: str, entry: float, original_stop: float, original_tp: float, atr: float, fingerprint_score: float) -> dict[str, float]:
    side = str(side or "").upper()
    entry = _f(entry)
    original_stop = _f(original_stop)
    original_tp = _f(original_tp)
    atr = max(0.0, _f(atr))
    score = _f(fingerprint_score)
    if entry <= 0 or original_stop <= 0 or original_tp <= 0:
        return {"stop": original_stop, "tp": original_tp, "rr": 0.0, "stop_widened": False}

    original_risk = abs(entry - original_stop)
    atr_floor = atr * (1.35 if score >= 84 else 1.15)
    risk_distance = max(original_risk, atr_floor)
    max_risk_distance = entry * 0.025
    if max_risk_distance > 0:
        risk_distance = min(risk_distance, max_risk_distance)

    target_rr = 2.4 if score >= 90 else 2.0 if score >= 82 else 1.65
    if side == "LONG":
        stop = entry - risk_distance
        minimum_tp = entry + risk_distance * target_rr
        tp = max(original_tp, minimum_tp)
    else:
        stop = entry + risk_distance
        minimum_tp = entry - risk_distance * target_rr
        tp = min(original_tp, minimum_tp)

    reward = abs(tp - entry)
    rr = reward / risk_distance if risk_distance > 0 else 0.0
    return {
        "stop": round(stop, 12),
        "tp": round(tp, 12),
        "rr": round(rr, 4),
        "stop_widened": risk_distance > original_risk * 1.02,
    }


async def symbol_adaptive_risk(symbol: str, *, side: str, entry: float, original_stop: float, original_tp: float, fingerprint_score: float) -> dict[str, Any]:
    rows = await binance_client.klines(symbol, interval="5m", limit=40)
    atr = _atr(rows)
    geometry = adaptive_geometry(
        side=side,
        entry=entry,
        original_stop=original_stop,
        original_tp=original_tp,
        atr=atr,
        fingerprint_score=fingerprint_score,
    )
    return {"version": VERSION, "atr": round(atr, 12), **geometry}
