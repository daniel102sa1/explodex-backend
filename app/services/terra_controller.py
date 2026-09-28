from __future__ import annotations

"""Terra Controller.

Final PAPER gate. It combines Terra's thesis with pattern evidence and market
context, then constrains exposure without changing the technical invalidation.
"""

from typing import Any

from app.services.terra_risk_engine import evaluate_risk


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _pattern_score(pattern: dict[str, Any]) -> float:
    scores = [_f(v.get("score")) for v in pattern.values() if isinstance(v, dict)]
    return sum(scores) / len(scores) if scores else 0.0


def _timeframe_alignment(pattern: dict[str, Any], direction: str) -> float:
    wanted = "SHORT_TERM_UPTREND" if direction == "LONG" else "SHORT_TERM_DOWNTREND"
    frames = [v for v in pattern.values() if isinstance(v, dict)]
    if not frames:
        return 50.0
    aligned = sum(1 for v in frames if wanted in list(v.get("patterns") or []))
    return min(100.0, 35.0 + (aligned / len(frames)) * 65.0)


def derive_market_context(
    *,
    pattern: dict[str, Any],
    decision: dict[str, Any],
    snapshot: dict[str, Any],
    btc_context_score: float = 50.0,
) -> dict[str, float]:
    direction = str(decision.get("direction") or "").upper()
    book = snapshot.get("order_book") if isinstance(snapshot.get("order_book"), dict) else {}
    bids = list(book.get("bids") or [])
    asks = list(book.get("asks") or [])

    def depth(rows: list[Any]) -> float:
        total = 0.0
        for row in rows[:20]:
            try:
                total += float(row[1])
            except Exception:
                pass
        return total

    bid_depth, ask_depth = depth(bids), depth(asks)
    total_depth = bid_depth + ask_depth
    liquidity_score = 50.0
    if total_depth > 0:
        supportive = bid_depth if direction == "LONG" else ask_depth
        liquidity_score = max(20.0, min(90.0, 35.0 + supportive / total_depth * 55.0))

    candles = list(snapshot.get("klines_1h") or [])
    ranges: list[float] = []
    for k in candles[-24:]:
        try:
            high = float(k.get("high")) if isinstance(k, dict) else float(k[2])
            low = float(k.get("low")) if isinstance(k, dict) else float(k[3])
            close = float(k.get("close")) if isinstance(k, dict) else float(k[4])
            if close > 0:
                ranges.append(abs(high - low) / close * 100.0)
        except Exception:
            pass
    avg_range = sum(ranges) / len(ranges) if ranges else 1.0
    volatility_score = max(20.0, min(85.0, 80.0 - abs(avg_range - 1.2) * 18.0))

    return {
        "volatility_score": round(volatility_score, 2),
        "liquidity_score": round(liquidity_score, 2),
        "timeframe_alignment": round(_timeframe_alignment(pattern, direction), 2),
        "btc_context_score": max(0.0, min(100.0, _f(btc_context_score, 50.0))),
    }


def build_terra_context(
    *,
    pattern: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pattern = pattern or {}
    market = market or {}
    decision = decision or {}

    risk = evaluate_risk(
        confidence_score=_f(decision.get("confidence_score"), _f(decision.get("evidence_score"), 0.0)),
        stop_distance_pct=_f(decision.get("stop_distance_pct")),
        volatility_score=_f(market.get("volatility_score"), 50.0),
        liquidity_score=_f(market.get("liquidity_score"), 50.0),
        timeframe_alignment=_f(market.get("timeframe_alignment"), 50.0),
        pattern_score=_pattern_score(pattern),
        btc_context_score=_f(market.get("btc_context_score"), 50.0),
    )

    return {
        "pattern_engine": pattern,
        "market_context": market,
        "terra_decision": decision,
        "adaptive_risk": risk,
        "paper_mode": True,
    }


def apply_adaptive_risk(context: dict[str, Any]) -> dict[str, Any]:
    decision = dict(context.get("terra_decision") or {})
    risk = dict(context.get("adaptive_risk") or {})

    if str(decision.get("action") or "").upper() != "ENTER":
        decision["allow_entry"] = False
        return decision

    decision["leverage"] = risk.get("leverage", 1.0)
    decision["capital_allocation_pct"] = risk.get("capital_allocation_pct", 3.0)
    decision["risk_pct"] = min(
        _f(decision.get("risk_pct"), _f(risk.get("risk_pct"), 0.20)),
        _f(risk.get("risk_pct"), 0.20),
    )
    decision["risk_tier"] = risk.get("tier")
    decision["risk_evidence_score"] = risk.get("evidence_score")

    if _f(risk.get("evidence_score")) < 55.0:
        decision["action"] = "WAIT"
        decision["allow_entry"] = False
    else:
        decision["allow_entry"] = True

    return decision


def should_allow_entry(context: dict[str, Any]) -> bool:
    return bool(apply_adaptive_risk(context).get("allow_entry"))
