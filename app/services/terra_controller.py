from __future__ import annotations

"""Terra Controller V2.

Final deterministic gate after Terra's AI thesis and before PAPER execution.
It never chooses LONG/SHORT; it validates the AI plan and bounds exposure.
"""

from typing import Any

from app.services.pattern_engine import analyze_pattern_context
from app.services.terra_risk_engine import evaluate_risk

VERSION = "terra_controller_v2"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _book_liquidity_score(snapshot: dict[str, Any]) -> float:
    book = snapshot.get("order_book") if isinstance(snapshot.get("order_book"), dict) else {}
    bids = list(book.get("bids") or [])
    asks = list(book.get("asks") or [])
    if not bids or not asks:
        return 45.0
    try:
        best_bid = _f(bids[0][0])
        best_ask = _f(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        spread_bps = ((best_ask - best_bid) / mid * 10000.0) if mid > 0 else 100.0
        depth = 0.0
        for row in bids[:20] + asks[:20]:
            depth += _f(row[0]) * _f(row[1])
        spread_score = 100.0 if spread_bps <= 2 else 85.0 if spread_bps <= 5 else 65.0 if spread_bps <= 10 else 35.0
        depth_score = 100.0 if depth >= 500000 else 85.0 if depth >= 150000 else 65.0 if depth >= 50000 else 40.0
        return max(0.0, min(100.0, spread_score * 0.6 + depth_score * 0.4))
    except Exception:
        return 45.0


def _volatility_quality(snapshot: dict[str, Any]) -> float:
    candles = snapshot.get("klines_15m") or snapshot.get("klines") or []
    ranges: list[float] = []
    closes: list[float] = []
    for row in list(candles)[-24:]:
        try:
            if isinstance(row, dict):
                h, l, c = _f(row.get("high")), _f(row.get("low")), _f(row.get("close"))
            else:
                h, l, c = _f(row[2]), _f(row[3]), _f(row[4])
            if c > 0:
                ranges.append((h - l) / c * 100.0)
                closes.append(c)
        except Exception:
            continue
    if not ranges:
        return 50.0
    avg = sum(ranges) / len(ranges)
    if 0.15 <= avg <= 1.8:
        return 85.0
    if 0.08 <= avg <= 3.0:
        return 65.0
    return 35.0


def _confidence_from_decision(decision: dict[str, Any], setup_score: float) -> float:
    strength = str(decision.get("evidence_strength") or "LOW").upper()
    base = {"LOW": 45.0, "MEDIUM": 70.0, "HIGH": 88.0}.get(strength, 45.0)
    if setup_score > 0:
        base = base * 0.75 + max(0.0, min(100.0, setup_score)) * 0.25
    return max(0.0, min(100.0, base))


def build_terra_context(
    *,
    pattern: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
    learning_multiplier: float = 1.0,
) -> dict[str, Any]:
    market = market or {}
    decision = decision or {}
    snapshot = snapshot or {}
    pattern = pattern or analyze_pattern_context(snapshot)

    entry_low = _f(decision.get("entry_low"))
    entry_high = _f(decision.get("entry_high"))
    entry = (entry_low + entry_high) / 2.0 if entry_low > 0 and entry_high > 0 else _f(market.get("current_price"))
    stop = _f(decision.get("stop_loss"))
    stop_distance_pct = abs(entry - stop) / entry * 100.0 if entry > 0 and stop > 0 else 999.0

    setup_score = _f(market.get("setup_score"))
    confidence = _confidence_from_decision(decision, setup_score)
    directional_frames = int(_f(pattern.get("directional_timeframes"), 0.0))
    alignment = _f(pattern.get("agreement_pct"), 50.0) if directional_frames > 0 else 50.0
    # No named pattern is neutral, not bearish. Patterns can add or subtract
    # confidence but are never mandatory for a trade.
    pattern_score = max(50.0, _f(pattern.get("score"), 0.0))
    liquidity = _book_liquidity_score(snapshot)
    volatility = _volatility_quality(snapshot)
    btc_context = _f(market.get("btc_context_score"), 50.0)

    risk = evaluate_risk(
        confidence_score=confidence,
        stop_distance_pct=stop_distance_pct,
        volatility_score=volatility,
        liquidity_score=liquidity,
        timeframe_alignment=alignment,
        pattern_score=pattern_score,
        btc_context_score=btc_context,
        requested_leverage=_f(decision.get("leverage"), 1.0),
        requested_capital_allocation_pct=_f(decision.get("capital_allocation_pct"), 5.0),
        requested_risk_pct=_f(decision.get("risk_pct"), 0.25),
        learning_multiplier=learning_multiplier,
    )

    action = str(decision.get("action") or "NO_TRADE").upper()
    direction = str(decision.get("direction") or "NONE").upper()
    ai_wants_entry = bool(decision.get("allow_entry")) and action == "ENTER" and direction in {"LONG", "SHORT"}
    allow_entry = ai_wants_entry and bool(risk.get("allow_entry"))

    return {
        "version": VERSION,
        "paper_mode": True,
        "allow_entry": bool(allow_entry),
        "ai_wants_entry": bool(ai_wants_entry),
        "pattern_engine": pattern,
        "market_context": {
            "setup_score": setup_score,
            "confidence_score": round(confidence, 2),
            "liquidity_score": round(liquidity, 2),
            "volatility_score": round(volatility, 2),
            "timeframe_alignment": round(alignment, 2),
            "btc_context_score": round(btc_context, 2),
            "stop_distance_pct": round(stop_distance_pct, 4),
        },
        "adaptive_risk": risk,
        "decision_gate_reason": "approved" if allow_entry else (
            "ai_not_enter" if not ai_wants_entry else "adaptive_risk_veto"
        ),
    }
