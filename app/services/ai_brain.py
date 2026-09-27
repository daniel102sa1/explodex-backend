from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.config import settings


AI_BRAIN_VERSION = "terra_primary_decider_v2"

_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": [
            "NO_TRADE", "PRE_ALERT_LONG", "PRE_ALERT_SHORT", "ARMED",
            "LONG_CONFIRMED", "SHORT_CONFIRMED", "COOLING", "INVALIDATED",
        ]},
        "direction": {"type": "string", "enum": ["LONG", "SHORT", "NONE"]},
        "allow_entry": {"type": "boolean"},
        "evidence_strength": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "continuation_checks": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "state", "direction", "allow_entry", "evidence_strength",
        "reasons", "blockers", "continuation_checks", "summary",
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
        "reasoning_effort": settings.ai_brain_reasoning_effort,\n        "role": "PRIMARY_DECIDER",
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _fallback(reason: str, *, eligible: bool) -> dict[str, Any]:
    return {
        "version": AI_BRAIN_VERSION,
        "available": False,
        "eligible": eligible,
        "model": settings.openai_model,
        "shadow_only": bool(settings.ai_brain_shadow_only),
        "state": "NO_TRADE",
        "direction": "NONE",
        "allow_entry": False,
        "evidence_strength": "LOW",
        "reasons": [],
        "blockers": [reason],
        "continuation_checks": [],
        "summary": reason,
        "usage": {},
    }


def _packet(
    *,
    symbol: str,
    scored: dict[str, Any],
    prediction: dict[str, Any],
    plan: dict[str, Any],
    deterministic_decision: dict[str, Any],
    market_event: dict[str, Any],
    coinglass: dict[str, Any] | None,
) -> dict[str, Any]:
    metrics = _dict(scored.get("metrics"))
    sequence = _dict(prediction.get("sequence"))
    stack = _dict(prediction.get("prediction_stack_v5"))
    veto = _dict(stack.get("risk_veto"))
    metric_keys = (
        "change_5m_pct", "change_15m_pct", "volume_acceleration",
        "relative_volume", "futures_delta_ratio", "spot_delta_ratio",
        "oi_change_pct", "order_book_imbalance", "risk_guard_pass",
        "risk_guard_blocks", "risk_guard_warnings",
    )
    cg = _dict(coinglass)
    return {
        "symbol": symbol,
        "current_price": _f(scored.get("current_price")),
        "deterministic_state": scored.get("state"),
        "deterministic_direction": scored.get("direction"),
        "setup_score": _f(scored.get("setup_score")),
        "risk_score": _f(scored.get("risk_score"), 100.0),
        "prediction": {
            "type": prediction.get("type"),
            "phase": prediction.get("phase"),
            "direction": prediction.get("direction"),
            "preactivation_score": _f(prediction.get("preactivation_score")),
            "chase_risk": bool(sequence.get("chase_risk")),
            "risk_guard_pass": bool(sequence.get("risk_guard_pass", True)),
            "risk_guard_blocks": list(sequence.get("risk_guard_blocks") or []),
            "hard_veto": bool(veto.get("blocked") or veto.get("invalidated") or veto.get("hard_block")),
        },
        "fixed_plan": {
            key: plan.get(key) for key in (
                "direction", "entry_low", "entry_high", "trigger_price",
                "invalidation_price", "stop_loss", "tp1", "tp2", "tp3",
            )
        },
        "deterministic_decision": deterministic_decision,
        "market_event": market_event,
        "metrics": {key: metrics.get(key) for key in metric_keys if key in metrics},
        "coinglass": {
            key: cg.get(key) for key in ("open_interest", "taker", "funding", "liquidations")
            if key in cg
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
    coinglass: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = str(scored.get("state") or "")
    phase = str(prediction.get("phase") or "")
    eligible = state in {"PREPARING", "READY"} or phase in {
        "PREACTIVACION", "VIGILAR_CONFIRMACION", "ACTIVADO", "ESPERAR_RETEST",
    }

    if not settings.ai_brain_enabled:
        return _fallback("AI Brain disabled", eligible=eligible)
    if not settings.openai_api_key:
        return _fallback("OPENAI_API_KEY not configured", eligible=eligible)
    if not eligible:
        result = _fallback("Candidate has not reached PRE-ALERTA/ARMADO", eligible=False)
        result["available"] = True
        return result

    packet = _packet(
        symbol=symbol,
        scored=scored,
        prediction=prediction,
        plan=plan,
        deterministic_decision=deterministic_decision,
        market_event=market_event,
        coinglass=coinglass,
    )
    instructions = (
        "You are ExplodeX Terra Primary Decision Brain for PAPER trading. Be extremely selective. "
        "You own market-state interpretation, direction confirmation and final timing. "
        "The deterministic modules are sensors/advisors: they propose a candidate plan and expose hard safety facts, "
        "but their legacy timing decision is not authoritative. "
        "The hard safety gate is non-negotiable: never override failed risk guard, invalidation, chase, terminal/cooldown, "
        "price outside the fixed entry zone, or a direction mismatch with the fixed plan. "
        "Never invent or change entry, stop, targets, leverage or position size, and never interpret setup scores as probabilities. "
        "You may reject a deterministic entry, and you may confirm before the legacy timing gate if the supplied structure, "
        "flow and microstructure clearly support immediate continuation and hard_safety.pass is true. "
        "If evidence is missing, mixed or weak, prefer ARMED, PRE_ALERT or NO_TRADE. "
        "allow_entry may be true only when your state is LONG_CONFIRMED or SHORT_CONFIRMED, your direction matches the fixed plan, "
        "and hard_safety.pass is true."
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
            input="Evaluate this ExplodeX market packet:\n" + json.dumps(
                packet, ensure_ascii=False, separators=(",", ":")
            ),
            text={
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "explodex_terra_decision",
                    "strict": True,
                    "schema": _DECISION_SCHEMA,
                },
            },
        )
        decision = json.loads(response.output_text)
        direction = str(decision.get("direction") or "NONE").upper()
        ai_state = str(decision.get("state") or "NO_TRADE").upper()
        allow_entry = bool(decision.get("allow_entry"))

        plan_direction = str(plan.get("direction") or scored.get("direction") or "").upper()
        hard_safe = bool(hard_safety.get("pass"))
        confirmed = (
            (ai_state == "LONG_CONFIRMED" and direction == "LONG")
            or (ai_state == "SHORT_CONFIRMED" and direction == "SHORT")
        )
        blockers = list(decision.get("blockers") or [])
        if direction not in {"NONE", plan_direction}:
            allow_entry = False
            blockers.append("Terra direction conflicts with the fixed candidate plan")
        if not hard_safe:
            allow_entry = False
            blockers.extend(list(hard_safety.get("blockers") or []))
        if not confirmed:
            allow_entry = False

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
            "response_id": response.id,
            "state": ai_state,
            "direction": direction,
            "allow_entry": allow_entry,
            "evidence_strength": decision.get("evidence_strength"),
            "reasons": list(decision.get("reasons") or [])[:8],
            "blockers": blockers[:8],
            "continuation_checks": list(decision.get("continuation_checks") or [])[:8],
            "summary": str(decision.get("summary") or "")[:600],
            "usage": usage,
        }
    except Exception as exc:
        return _fallback(
            f"Terra unavailable: {type(exc).__name__}: {str(exc)[:240]}",
            eligible=True,
        )
