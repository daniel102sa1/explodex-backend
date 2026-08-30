from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.edge_engine import edge_summary


PHASE_PRIORITY = {
    "ACTIVADO": 90,
    "PREACTIVACION": 80,
    "VIGILAR_CONFIRMACION": 70,
    "VIGILAR_CONFLICTOS": 60,
    "ESPERAR_RETEST": 55,
    "VIGILAR": 40,
    "SIN_SETUP": 10,
    "SIN_DATOS": 0,
}

ACTION_PRIORITY = {
    "ENTRAR_LONG": 100,
    "ENTRAR_SHORT": 100,
    "ESPERAR_RETEST": 60,
    "ESPERAR": 50,
    "NO_ENTRAR": 0,
}


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _heart_summary(reason: dict[str, Any], prediction: dict[str, Any]) -> tuple[dict[str, Any], int, int, str, bool]:
    heart = _json_dict(reason.get("explodex_heart"))
    if not heart:
        heart = _json_dict(prediction.get("explodex_heart"))
    action_decision = _json_dict(heart.get("action_decision"))
    action = str(action_decision.get("action") or "ESPERAR")
    should_enter = bool(action_decision.get("should_enter"))

    # These are the actual Heart entry checks. Do not fabricate a green 9/9
    # from a list of descriptive confirmations.
    missing = list(action_decision.get("advanced_stack_missing") or [])
    stack_ready = bool(action_decision.get("advanced_stack_ready"))
    stack_total = 7
    stack_ready_count = stack_total if stack_ready else max(0, stack_total - len(missing))

    # Add the live price-in-zone check as a separate, meaningful condition.
    total_conditions = stack_total + 1
    ready_conditions = stack_ready_count + (1 if bool(action_decision.get("price_in_entry_zone")) else 0)
    operable = should_enter and action in {"ENTRAR_LONG", "ENTRAR_SHORT"}
    return heart, ready_conditions, total_conditions, action, operable


async def live_predictions(db: AsyncSession, limit: int = 50) -> dict[str, Any]:
    latest_result = await db.execute(
        text(
            """
            SELECT DISTINCT ON (s.symbol_id)
                   s.id::text, sy.symbol, s.created_at, s.updated_at,
                   s.direction, s.state, s.setup_type, s.setup_score, s.risk_score,
                   s.current_price, s.entry_low, s.entry_high, s.invalidation_price,
                   s.stop_loss, s.tp1, s.tp2, s.tp3,
                   s.expected_move_min_pct, s.expected_move_max_pct,
                   s.expected_duration_min_minutes, s.expected_duration_max_minutes,
                   s.reason
            FROM signals s
            JOIN symbols sy ON sy.id = s.symbol_id
            WHERE s.created_at >= NOW() - INTERVAL '8 hours'
            ORDER BY s.symbol_id, s.created_at DESC
            """
        )
    )
    rows = [dict(row) for row in latest_result.mappings().all()]

    history_result = await db.execute(
        text(
            """
            SELECT sy.symbol, s.created_at, s.setup_score, s.risk_score, s.state, s.reason
            FROM signals s
            JOIN symbols sy ON sy.id = s.symbol_id
            WHERE s.created_at >= NOW() - INTERVAL '90 minutes'
            ORDER BY sy.symbol, s.created_at ASC
            """
        )
    )
    trajectories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history_result.mappings().all():
        reason = _json_dict(row["reason"])
        prediction = _json_dict(reason.get("prediction"))
        trajectories[str(row["symbol"])].append(
            {
                "at": _iso(row["created_at"]),
                "preactivation_score": _f(prediction.get("preactivation_score")),
                "setup_score": _f(row["setup_score"]),
                "risk_score": _f(row["risk_score"]),
                "phase": str(prediction.get("phase") or row["state"] or "SIN_SETUP"),
            }
        )

    items: list[dict[str, Any]] = []
    for row in rows:
        reason = _json_dict(row.get("reason"))
        prediction = _json_dict(reason.get("prediction"))
        metrics = _json_dict(reason.get("metrics"))
        coinglass = _json_dict(reason.get("coinglass"))
        heart, ready_conditions, total_conditions, action, operable = _heart_summary(reason, prediction)
        history = trajectories.get(str(row["symbol"]), [])[-6:]
        scores = [x["preactivation_score"] for x in history if x["preactivation_score"] > 0]
        velocity = (scores[-1] - scores[0]) if len(scores) >= 2 else 0.0
        accelerating = len(scores) >= 3 and scores[-1] > scores[-2] > scores[-3]
        phase = str(prediction.get("phase") or "SIN_SETUP")
        state = str(row.get("state") or "NO_TRADE")
        items.append(
            {
                "id": row["id"],
                "symbol": row["symbol"],
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row.get("updated_at")),
                "direction": row["direction"],
                "state": state,
                "setup_type": row.get("setup_type"),
                "setup_score": _f(row["setup_score"]),
                "risk_score": _f(row["risk_score"]),
                "current_price": _f(row["current_price"]),
                "entry_low": _f(row["entry_low"]),
                "entry_high": _f(row["entry_high"]),
                "invalidation_price": _f(row.get("invalidation_price")),
                "stop_loss": _f(row["stop_loss"]),
                "tp1": _f(row["tp1"]),
                "tp2": _f(row["tp2"]),
                "tp3": _f(row["tp3"]),
                "expected_move_min_pct": _f(row.get("expected_move_min_pct")),
                "expected_move_max_pct": _f(row.get("expected_move_max_pct")),
                "expected_duration_min_minutes": row.get("expected_duration_min_minutes"),
                "expected_duration_max_minutes": row.get("expected_duration_max_minutes"),
                "prediction": prediction,
                "explodex_heart": heart,
                "heart_action": action,
                "metrics": metrics,
                "coinglass": coinglass,
                "conditions_ready": ready_conditions,
                "conditions_total": total_conditions,
                "operable": operable,
                "preparation_trajectory": history,
                "preparation_velocity": round(velocity, 2),
                "preparation_accelerating": accelerating,
            }
        )

    items.sort(
        key=lambda item: (
            ACTION_PRIORITY.get(str(item.get("heart_action")), 0),
            1 if item["operable"] else 0,
            PHASE_PRIORITY.get(str(item["prediction"].get("phase")), 0),
            _f(item["prediction"].get("preactivation_score")),
            item["setup_score"],
            -item["risk_score"],
        ),
        reverse=True,
    )
    items = items[:limit]
    operable_count = sum(1 for item in items if item["operable"])
    preactivation_count = sum(1 for item in items if str(item["prediction"].get("phase")) == "PREACTIVACION")
    activated_count = sum(1 for item in items if str(item["heart_action"]) in {"ENTRAR_LONG", "ENTRAR_SHORT"})
    accelerating_count = sum(1 for item in items if item["preparation_accelerating"])

    return {
        "items": items,
        "summary": {
            "symbols": len(items),
            "operable": operable_count,
            "preactivation": preactivation_count,
            "activated": activated_count,
            "accelerating": accelerating_count,
        },
        "note": "La decisión y los checks vienen del ExplodeX Heart. El score es técnico y no una probabilidad garantizada.",
    }


async def prediction_history(db: AsyncSession, symbol: str, limit: int = 12) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT s.created_at, s.direction, s.state, s.setup_score, s.risk_score,
                   s.current_price, s.reason
            FROM signals s
            JOIN symbols sy ON sy.id = s.symbol_id
            WHERE sy.symbol = :symbol
            ORDER BY s.created_at DESC
            LIMIT :limit
            """
        ),
        {"symbol": symbol, "limit": limit},
    )
    rows = []
    for row in reversed(result.mappings().all()):
        reason = _json_dict(row["reason"])
        prediction = _json_dict(reason.get("prediction"))
        heart = _json_dict(reason.get("explodex_heart")) or _json_dict(prediction.get("explodex_heart"))
        action = _json_dict(heart.get("action_decision")).get("action")
        rows.append(
            {
                "at": _iso(row["created_at"]),
                "price": _f(row["current_price"]),
                "direction": row["direction"],
                "state": row["state"],
                "setup_score": _f(row["setup_score"]),
                "risk_score": _f(row["risk_score"]),
                "preactivation_score": _f(prediction.get("preactivation_score")),
                "phase": prediction.get("phase") or "SIN_SETUP",
                "type": prediction.get("type") or "SIN_SETUP",
                "heart_action": action or "ESPERAR",
            }
        )
    learned = await edge_summary(db, symbol=symbol)
    return {"symbol": symbol, "history": rows, "edge": learned}


async def live_event_feed(db: AsyncSession, limit: int = 80) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    signal_result = await db.execute(
        text(
            """
            SELECT s.created_at, sy.symbol, s.direction, s.state, s.setup_score,
                   s.risk_score, s.reason
            FROM signals s
            JOIN symbols sy ON sy.id = s.symbol_id
            WHERE s.created_at >= NOW() - INTERVAL '3 hours'
            ORDER BY s.created_at DESC
            LIMIT 60
            """
        )
    )
    for row in signal_result.mappings().all():
        reason = _json_dict(row["reason"])
        prediction = _json_dict(reason.get("prediction"))
        heart = _json_dict(reason.get("explodex_heart")) or _json_dict(prediction.get("explodex_heart"))
        action = str(_json_dict(heart.get("action_decision")).get("action") or "ESPERAR")
        phase = str(prediction.get("phase") or row["state"])
        kind = str(prediction.get("type") or "SETUP")
        pre = _f(prediction.get("preactivation_score"))
        entering = action in {"ENTRAR_LONG", "ENTRAR_SHORT"}
        severity = "READY" if entering else "EARLY" if phase in {"ACTIVADO", "PREACTIVACION"} else "INFO"
        title = f"{row['symbol']} {action.replace('_', ' ')}" if entering else f"{row['symbol']} {phase.replace('_', ' ')}"
        events.append(
            {
                "at": _iso(row["created_at"]),
                "source": "SCANNER",
                "severity": severity,
                "symbol": row["symbol"],
                "direction": row["direction"],
                "state": row["state"],
                "phase": phase,
                "heart_action": action,
                "title": title,
                "message": f"{kind.replace('_', ' ')} · preparación {pre:.1f}/100 · setup {_f(row['setup_score']):.1f}/100",
            }
        )

    trade_result = await db.execute(
        text(
            """
            SELECT te.created_at, sy.symbol, t.direction, te.event_type, te.price, te.message
            FROM trade_events te
            JOIN trades t ON t.id = te.trade_id
            JOIN symbols sy ON sy.id = t.symbol_id
            WHERE te.created_at >= NOW() - INTERVAL '12 hours'
            ORDER BY te.created_at DESC
            LIMIT 40
            """
        )
    )
    for row in trade_result.mappings().all():
        event_type = str(row["event_type"])
        severity = "EXIT" if event_type in {"STOP", "TP2", "TIME_STOP", "MAX_DURATION"} else "ENTRY" if event_type == "OPEN" else "INFO"
        events.append(
            {
                "at": _iso(row["created_at"]),
                "source": "PAPER",
                "severity": severity,
                "symbol": row["symbol"],
                "direction": row["direction"],
                "state": event_type,
                "phase": event_type,
                "title": f"{row['symbol']} · {event_type.replace('_', ' ')}",
                "message": row["message"] or f"Evento paper a {_f(row['price']):.8g}",
            }
        )

    alert_result = await db.execute(
        text(
            """
            SELECT a.created_at, a.severity, a.title, a.message, sy.symbol, s.direction
            FROM alerts a
            LEFT JOIN signals s ON s.id = a.signal_id
            LEFT JOIN symbols sy ON sy.id = s.symbol_id
            WHERE a.created_at >= NOW() - INTERVAL '12 hours'
            ORDER BY a.created_at DESC
            LIMIT 40
            """
        )
    )
    for row in alert_result.mappings().all():
        events.append(
            {
                "at": _iso(row["created_at"]),
                "source": "ALERTA",
                "severity": row["severity"] or "INFO",
                "symbol": row["symbol"],
                "direction": row["direction"],
                "state": row["severity"],
                "phase": row["severity"],
                "title": row["title"],
                "message": row["message"],
            }
        )

    events.sort(key=lambda item: item.get("at") or "", reverse=True)
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in events:
        key = (str(item.get("symbol")), str(item.get("title")), str(item.get("at"))[:16])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped
