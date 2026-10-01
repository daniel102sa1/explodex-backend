from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from openai import AsyncOpenAI

from app.config import settings

# A bounded in-memory per-session limit: zero PostgreSQL writes.
# For a hard account-wide spending cap, also set a provider-side project budget.
_AI_USAGE: dict[str, tuple[float, float, int]] = {}
_AI_USAGE_LOCK = asyncio.Lock()


async def _reserve_practice_ai(session_id: str) -> str | None:
    if not (8 <= len(session_id) <= 80) or any(not (ch.isalnum() or ch in "-_") for ch in session_id):
        return "Sesión no válida para usar la IA de pago; utiliza el análisis técnico gratuito."
    limit = max(0, min(50, settings.practice_ai_daily_limit))
    if limit == 0:
        return "La IA de pago está desactivada para controlar los gastos."
    now = time.monotonic()
    async with _AI_USAGE_LOCK:
        if len(_AI_USAGE) > 1024:
            for key, (start, _, _) in list(_AI_USAGE.items()):
                if now - start > 86_400:
                    _AI_USAGE.pop(key, None)
        start, last, count = _AI_USAGE.get(session_id, (now, -10_000.0, 0))
        if now - start >= 86_400:
            start, last, count = now, -10_000.0, 0
        if count >= limit:
            return "Se alcanzó el límite diario de consultas a la IA; usa el motor técnico gratuito."
        if now - last < max(0, settings.practice_ai_cooldown_seconds):
            return "Consulta demasiado reciente; espera antes de volver a gastar en IA."
        _AI_USAGE[session_id] = (start, now, count + 1)
    return None


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

    budget_error = await _reserve_practice_ai(str(payload.get("session_id") or ""))
    if budget_error:
        return _fallback(payload, budget_error)

    # The model receives compact market features and a handful of trade
    # outcomes. It never receives session IDs, raw database rows or overlays.
    current = dict(_dict(payload.get("current")))
    pattern = dict(_dict(current.get("pattern")))
    pattern.pop("overlays", None)
    current["pattern"] = pattern
    current.pop("indicatorNotes", None)
    current.pop("autoOverlays", None)
    features = ("timestamp", "open", "high", "low", "close", "volume")
    candles = []
    for row in list(payload.get("recent_candles") or [])[-36:]:
        if isinstance(row, dict):
            candles.append({key: row.get(key) for key in features if key in row})
    trades = []
    trade_keys = ("side", "symbol", "timeframe", "pattern", "entry_price", "exit_price", "net_pnl", "r_multiple", "close_reason")
    for item in list(payload.get("recent_trades") or [])[:10]:
        if isinstance(item, dict):
            trades.append({key: item.get(key) for key in trade_keys if key in item})
    packet = {
        "symbol": str(payload.get("symbol") or "")[:32],
        "interval": str(payload.get("interval") or "")[:16],
        "engine_direction": payload.get("engine_direction"),
        "current": current,
        "multi_timeframe": list(payload.get("multi_timeframe") or [])[:4],
        "recent_candles": candles,
        "recent_trades": trades,
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
        "Si se incluyen operaciones históricas, identifica errores recurrentes observables " 
        "sin asumir que los resultados garantizan operaciones futuras. " 
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
