from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.config import settings


_AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
        "evidence_strength": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "summary": {"type": "string"},
        "entry": {"type": "number"},
        "stop_loss": {"type": "number"},
        "tp1": {"type": "number"},
        "tp2": {"type": "number"},
        "tp3": {"type": "number"},
        "invalidation": {"type": "number"},
        "breakout_level": {"type": "number"},
        "projection_from": {"type": "number"},
        "projection_to": {"type": "number"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "what_to_wait_for": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "direction", "evidence_strength", "summary", "entry", "stop_loss",
        "tp1", "tp2", "tp3", "invalidation", "breakout_level",
        "projection_from", "projection_to", "reasons", "risks", "what_to_wait_for",
    ],
    "additionalProperties": False,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _fallback(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    current = _dict(payload.get("current"))
    pattern = _dict(current.get("pattern"))
    price = _f(current.get("price"))
    direction = str(payload.get("engine_direction") or "WAIT").upper()
    if direction not in {"LONG", "SHORT"}:
        direction = "WAIT"

    pattern_direction = str(pattern.get("direction") or "WAIT").upper()
    if pattern_direction in {"LONG", "SHORT"}:
        direction = pattern_direction

    entry = _f(pattern.get("entry"), price if direction != "WAIT" else 0.0)
    stop = _f(pattern.get("stop"))
    tp1 = _f(pattern.get("tp1"))
    tp2 = _f(pattern.get("tp2"))
    tp3 = _f(pattern.get("tp3"))
    breakout = (
        _f(pattern.get("breakoutLong"))
        if direction == "LONG"
        else _f(pattern.get("breakoutShort"))
        if direction == "SHORT"
        else 0.0
    )

    if direction == "LONG" and tp2 <= 0:
        atr = max(_f(current.get("atr14")), price * 0.005)
        tp1, tp2, tp3 = price + atr, price + atr * 2, price + atr * 3
        if stop <= 0:
            stop = min(_f(current.get("support"), price - atr), price - atr)
    elif direction == "SHORT" and tp2 <= 0:
        atr = max(_f(current.get("atr14")), price * 0.005)
        tp1, tp2, tp3 = price - atr, price - atr * 2, price - atr * 3
        if stop <= 0:
            stop = max(_f(current.get("resistance"), price + atr), price + atr)

    projection_to = tp2 if direction in {"LONG", "SHORT"} else price
    summary = (
        f"Motor técnico: sesgo {direction}. {reason}"
        if direction != "WAIT"
        else f"Motor técnico: no hay dirección suficientemente confirmada. {reason}"
    )
    return {
        "available": False,
        "mode": "TECHNICAL_ENGINE",
        "model": None,
        "direction": direction,
        "evidence_strength": "MEDIUM" if direction != "WAIT" else "LOW",
        "summary": summary,
        "entry": entry,
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "invalidation": _f(pattern.get("invalidation"), stop),
        "breakout_level": breakout,
        "projection_from": price,
        "projection_to": projection_to,
        "reasons": list(pattern.get("rationale") or [])[:6],
        "risks": ["La IA generativa no está configurada; esta respuesta viene del motor técnico."],
        "what_to_wait_for": [
            "Confirmar ruptura/cierre y retest cuando corresponda.",
            "No tratar la proyección como certeza ni como garantía de resultado.",
        ],
        "usage": {},
    }


async def analyze_practice_direction(payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.paper_trading_only:
        return _fallback(payload, "Disponible únicamente en modo PAPER.")
    if not settings.openai_api_key:
        return _fallback(payload, "OPENAI_API_KEY no está configurada en Railway.")

    packet = {
        "symbol": payload.get("symbol"),
        "interval": payload.get("interval"),
        "engine_direction": payload.get("engine_direction"),
        "current": payload.get("current"),
        "multi_timeframe": payload.get("multi_timeframe"),
        "recent_candles": list(payload.get("recent_candles") or [])[-120:],
    }

    instructions = (
        "Eres el asistente técnico bajo demanda del Trading Lab PAPER de ExplodeX. "
        "No ejecutas órdenes y no prometes dirección futura. Tu tarea es interpretar un paquete ya calculado "
        "por un motor geométrico reproducible y elegir LONG, SHORT o WAIT para un escenario de práctica. "
        "Da prioridad a figuras CONFIRMADAS, estructura, invalidación, EMA20/50/200, RSI, MACD, volumen, ATR "
        "y alineación 5m/15m/1h/4h. Una figura EN FORMACIÓN no autoriza dirección por sí sola: normalmente WAIT "
        "hasta ruptura/retest. Si existe conflicto importante entre temporalidades, responde WAIT. "
        "Si eliges LONG o SHORT, devuelve niveles internamente coherentes: entrada, stop estructural, TP1/TP2/TP3, "
        "invalidación y un projection_to razonable. No inventes una precisión falsa. "
        "Los precios son solo para simulación PAPER."
    )

    try:
        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=min(settings.ai_brain_timeout_seconds, 20.0),
            max_retries=1,
        )
        response = await client.responses.create(
            model=settings.openai_model,
            store=False,
            reasoning={"effort": "low"},
            max_output_tokens=650,
            instructions=instructions,
            input="Analiza este paquete del Trading Lab:\n" + json.dumps(
                packet, ensure_ascii=False, separators=(",", ":")
            ),
            text={
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "explodex_practice_ai_direction",
                    "strict": True,
                    "schema": _AI_SCHEMA,
                },
            },
        )
        result = json.loads(response.output_text)
        usage: dict[str, Any] = {}
        if response.usage is not None:
            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", None),
                "output_tokens": getattr(response.usage, "output_tokens", None),
                "total_tokens": getattr(response.usage, "total_tokens", None),
            }
        return {
            "available": True,
            "mode": "OPENAI_ON_DEMAND",
            "model": response.model,
            **result,
            "usage": usage,
        }
    except Exception as exc:
        return _fallback(payload, f"IA no disponible: {type(exc).__name__}.")
