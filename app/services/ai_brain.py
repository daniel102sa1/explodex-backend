from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.config import settings


AI_BRAIN_VERSION = "terra_full_control_paper_v1"

_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": [
            "NO_TRADE", "PRE_ALERT_LONG", "PRE_ALERT_SHORT", "ARMED",
            "LONG_CONFIRMED", "SHORT_CONFIRMED", "COOLING", "INVALIDATED",
        ]},
        "direction": {"type": "string", "enum": ["LONG", "SHORT", "NONE"]},
        "action": {"type": "string", "enum": ["NO_TRADE", "WAIT", "ENTER", "HOLD", "REDUCE", "EXIT"]},
        "allow_entry": {"type": "boolean"},
        "entry_low": {"type": "number"},
        "entry_high": {"type": "number"},
        "stop_loss": {"type": "number"},
        "tp1": {"type": "number"},
        "tp2": {"type": "number"},
        "tp3": {"type": "number"},
        "leverage": {"type": "number"},
        "risk_pct": {"type": "number"},
        "capital_allocation_pct": {"type": "number"},
        "max_hold_minutes": {"type": "integer"},
        "evidence_strength": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "continuation_checks": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "state", "direction", "action", "allow_entry",
        "entry_low", "entry_high", "stop_loss", "tp1", "tp2", "tp3",
        "leverage", "risk_pct", "capital_allocation_pct", "max_hold_minutes",
        "evidence_strength", "reasons", "risks", "continuation_checks", "summary",
    ],
    "additionalProperties": False,
}


def status() -> dict[str, Any]:
    return {
        "version": AI_BRAIN_VERSION,
        "enabled": bool(settings.ai_brain_enabled),
        "configured": bool(settings.openai_api_key),
        "model": settings.openai_model,
        "shadow_only": bool(settings.ai_brain_shadow_only),
        "reasoning_effort": settings.ai_brain_reasoning_effort,
        "role": "FULL_CONTROL_PAPER",
        "paper_only": bool(settings.paper_trading_only),
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _fallback(reason: str) -> dict[str, Any]:
    return {
        "version": AI_BRAIN_VERSION,
        "available": False,
        "eligible": True,
        "model": settings.openai_model,
        "shadow_only": bool(settings.ai_brain_shadow_only),
        "role": "FULL_CONTROL_PAPER",
        "paper_only": bool(settings.paper_trading_only),
        "state": "NO_TRADE",
        "direction": "NONE",
        "action": "NO_TRADE",
        "allow_entry": False,
        "entry_low": 0.0,
        "entry_high": 0.0,
        "stop_loss": 0.0,
        "tp1": 0.0,
        "tp2": 0.0,
        "tp3": 0.0,
        "leverage": 0.0,
        "risk_pct": 0.0,
        "capital_allocation_pct": 0.0,
        "max_hold_minutes": 0,
        "evidence_strength": "LOW",
        "reasons": [],
        "risks": [reason],
        "continuation_checks": [],
        "summary": reason,
        "usage": {},
        "plan": {},
    }


def _tail(value: Any, limit: int) -> list[Any]:
    return list(value[-limit:]) if isinstance(value, list) else []


def _compact_book(value: Any) -> dict[str, Any]:
    book = _dict(value)
    return {
        "lastUpdateId": book.get("lastUpdateId"),
        "bids": _tail(book.get("bids"), 20),
        "asks": _tail(book.get("asks"), 20),
    }


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": snapshot.get("symbol"),
        "source": snapshot.get("source"),
        "provider_warning": snapshot.get("provider_warning"),
        "klines_5m": _tail(snapshot.get("klines"), 36),
        "klines_15m": _tail(snapshot.get("klines_15m"), 24),
        "klines_1h": _tail(snapshot.get("klines_1h"), 24),
        "open_interest": snapshot.get("open_interest"),
        "open_interest_history": _tail(snapshot.get("open_interest_history"), 12),
        "taker": _tail(snapshot.get("taker"), 8),
        "premium": snapshot.get("premium"),
        "long_short": _tail(snapshot.get("long_short"), 8),
        "order_book": _compact_book(snapshot.get("order_book")),
        "agg_trades": _tail(snapshot.get("agg_trades"), 60),
        "spot_agg_trades": _tail(snapshot.get("spot_agg_trades"), 60),
        "top_long_short_accounts": _tail(snapshot.get("top_long_short_accounts"), 8),
        "top_long_short_positions": _tail(snapshot.get("top_long_short_positions"), 8),
    }


def _packet(
    *,
    symbol: str,
    scored: dict[str, Any],
    prediction: dict[str, Any],
    legacy_plan: dict[str, Any],
    deterministic_decision: dict[str, Any],
    market_event: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None,
) -> dict[str, Any]:
    metrics = _dict(scored.get("metrics"))
    components = _dict(scored.get("components"))
    return {
        "mode": "PAPER_SIMULATION_FULL_AUTONOMY",
        "symbol": symbol,
        "current_price": _f(scored.get("current_price")),
        "market_data": _compact_snapshot(snapshot),
        "coinglass": _dict(coinglass),
        "legacy_sensors": {
            "state": scored.get("state"),
            "direction": scored.get("direction"),
            "setup_score": scored.get("setup_score"),
            "risk_score": scored.get("risk_score"),
            "metrics": metrics,
            "components": components,
            "prediction": prediction,
            "market_event": market_event,
            "legacy_plan": legacy_plan,
            "legacy_decision": deterministic_decision,
        },
    }


async def evaluate_candidate(
    *,
    symbol: str,
    scored: dict[str, Any],
    prediction: dict[str, Any],
    plan: dict[str, Any],
    deterministic_decision: dict[str, Any],
    market_event: dict[str, Any],
    snapshot: dict[str, Any],
    coinglass: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not settings.ai_brain_enabled:
        return _fallback("AI Brain disabled")
    if not settings.openai_api_key:
        return _fallback("OPENAI_API_KEY not configured")
    if not settings.paper_trading_only:
        return _fallback("Terra full-control mode is enabled only for PAPER simulation")

    packet = _packet(
        symbol=symbol,
        scored=scored,
        prediction=prediction,
        legacy_plan=plan,
        deterministic_decision=deterministic_decision,
        market_event=market_event,
        snapshot=snapshot,
        coinglass=coinglass,
    )

    instructions = (
        "You are Terra, the sole trading decision brain of ExplodeX in a PAPER-ONLY experiment. "
        "Every legacy module is only a sensor or opinion. None of its states, risk scores, vetoes, "
        "entry zones, stops, targets, no-chase flags or timing decisions are binding on you. "
        "You decide whether there is a trade, LONG or SHORT, when to enter, the entry zone, stop loss, "
        "TP1/TP2/TP3, leverage, risk percentage, capital allocation percentage and maximum hold time. "
        "Size dynamically: when the setup has exceptional multi-source coherence, strong liquidity and clean immediate continuation, "
        "you may allocate more simulated capital and use more leverage; for ordinary or less certain setups use less capital and low leverage; "
        "for weak, noisy or conflicting evidence choose NO_TRADE rather than forcing size. Leverage must follow evidence quality, volatility, liquidity "
        "and stop distance, not excitement or a fast candle. You may disagree with all legacy outputs. "
        "Use the raw multi-timeframe candles, futures and spot aggressive trades, order book, open interest, "
        "funding/premium, long-short positioning, top-trader positioning, CoinGlass context and all derived sensors together. "
        "Do not treat any score as a calibrated probability. Prefer NO_TRADE when the evidence is not coherent. "
        "For ENTER decisions, produce internally coherent numeric levels for the chosen direction. "
        "This is simulation only; do not assume or claim guaranteed profit."
    )

    try:
        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.ai_brain_timeout_seconds,
            max_retries=1,
        )
        response = await client.responses.create(
            model=settings.openai_model,
            store=False,
            reasoning={"effort": settings.ai_brain_reasoning_effort},
            max_output_tokens=settings.ai_brain_max_output_tokens,
            instructions=instructions,
            input="Analyze this complete ExplodeX PAPER market packet:\n" + json.dumps(
                packet, ensure_ascii=False, separators=(",", ":")
            ),
            text={
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "explodex_terra_full_control",
                    "strict": True,
                    "schema": _DECISION_SCHEMA,
                },
            },
        )
        decision = json.loads(response.output_text)
        action = str(decision.get("action") or "NO_TRADE").upper()
        direction = str(decision.get("direction") or "NONE").upper()
        state = str(decision.get("state") or "NO_TRADE").upper()
        allow_entry = bool(decision.get("allow_entry")) and action == "ENTER" and direction in {"LONG", "SHORT"}

        plan_out = {
            "source": "TERRA_FULL_CONTROL",
            "direction": direction,
            "entry_low": _f(decision.get("entry_low")),
            "entry_high": _f(decision.get("entry_high")),
            "stop_loss": _f(decision.get("stop_loss")),
            "tp1": _f(decision.get("tp1")),
            "tp2": _f(decision.get("tp2")),
            "tp3": _f(decision.get("tp3")),
            "leverage": _f(decision.get("leverage")),
            "risk_pct": _f(decision.get("risk_pct")),
            "capital_allocation_pct": _f(decision.get("capital_allocation_pct")),
            "max_hold_minutes": int(decision.get("max_hold_minutes") or 0),
            "do_not_recalculate": True,
        }

        usage = {}
        if response.usage is not None:
            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", None),
                "output_tokens": getattr(response.usage, "output_tokens", None),
                "total_tokens": getattr(response.usage, "total_tokens", None),
            }

        return {
            "version": AI_BRAIN_VERSION,
            "available": True,
            "eligible": True,
            "model": response.model,
            "shadow_only": bool(settings.ai_brain_shadow_only),
            "role": "FULL_CONTROL_PAPER",
            "paper_only": True,
            "response_id": response.id,
            "state": state,
            "direction": direction,
            "action": action,
            "allow_entry": allow_entry,
            "entry_low": plan_out["entry_low"],
            "entry_high": plan_out["entry_high"],
            "stop_loss": plan_out["stop_loss"],
            "tp1": plan_out["tp1"],
            "tp2": plan_out["tp2"],
            "tp3": plan_out["tp3"],
            "leverage": plan_out["leverage"],
            "risk_pct": plan_out["risk_pct"],
            "capital_allocation_pct": plan_out["capital_allocation_pct"],
            "max_hold_minutes": plan_out["max_hold_minutes"],
            "evidence_strength": decision.get("evidence_strength"),
            "reasons": list(decision.get("reasons") or [])[:10],
            "risks": list(decision.get("risks") or [])[:10],
            "continuation_checks": list(decision.get("continuation_checks") or [])[:10],
            "summary": str(decision.get("summary") or "")[:800],
            "usage": usage,
            "plan": plan_out,
        }
    except Exception as exc:
        return _fallback(
            f"Terra unavailable: {type(exc).__name__}: {str(exc)[:240]}"
        )
