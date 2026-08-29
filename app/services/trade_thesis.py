from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

THESIS_VERSION = "trade_thesis_v1"
MIN_CREATE_SCORE = 72.0
MAX_CREATE_RISK = 48.0
COOLDOWN_MINUTES = 20
MIN_TTL_MINUTES = 120
MAX_TTL_MINUTES = 720
ACTIVE_STATUSES = {"WAITING_ENTRY", "ENTER_NOW", "NO_CHASE", "IN_POSITION"}
TERMINAL_STATUSES = {"INVALIDATED", "EXPIRED", "CLOSED"}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


async def ensure_trade_thesis_schema(db: AsyncSession) -> None:
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS trade_theses (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            symbol VARCHAR(32) NOT NULL,
            direction VARCHAR(8) NOT NULL,
            status VARCHAR(24) NOT NULL,
            setup_type VARCHAR(48),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            cooldown_until TIMESTAMPTZ,
            entered_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ,
            entry_low NUMERIC(30,12) NOT NULL,
            entry_high NUMERIC(30,12) NOT NULL,
            trigger_price NUMERIC(30,12),
            invalidation_price NUMERIC(30,12) NOT NULL,
            stop_loss NUMERIC(30,12) NOT NULL,
            tp1 NUMERIC(30,12) NOT NULL,
            tp2 NUMERIC(30,12) NOT NULL,
            tp3 NUMERIC(30,12) NOT NULL,
            chase_limit NUMERIC(30,12) NOT NULL,
            initial_score NUMERIC(10,4),
            latest_score NUMERIC(10,4),
            initial_risk_score NUMERIC(10,4),
            contradiction_count INTEGER NOT NULL DEFAULT 0,
            last_candidate_direction VARCHAR(8),
            last_price NUMERIC(30,12),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_trade_theses_symbol_time ON trade_theses(symbol, created_at DESC)"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_trade_theses_status ON trade_theses(status, updated_at DESC)"
    ))
    await db.commit()


def _candidate_plan(score: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    metrics = score.get("metrics") if isinstance(score.get("metrics"), dict) else {}
    current = _f(score.get("current_price"))
    atr_pct = max(0.05, _f(metrics.get("atr_pct"), 0.5))
    atr_abs = current * atr_pct / 100.0 if current > 0 else 0.0
    entry_low = _f(prediction.get("entry_low"), _f(score.get("entry_low")))
    entry_high = _f(prediction.get("entry_high"), _f(score.get("entry_high")))
    if entry_low > entry_high:
        entry_low, entry_high = entry_high, entry_low
    direction = str(prediction.get("direction") or score.get("direction") or "").upper()
    chase_buffer = max(atr_abs * 0.80, max(entry_high - entry_low, current * 0.0015))
    chase_limit = entry_high + chase_buffer if direction == "LONG" else entry_low - chase_buffer
    duration = int(_f(prediction.get("expected_duration_max_minutes"), _f(score.get("expected_duration_max_minutes"), 240)))
    ttl_minutes = max(MIN_TTL_MINUTES, min(MAX_TTL_MINUTES, max(duration, 180)))
    return {
        "direction": direction,
        "setup_type": str(prediction.get("type") or "early_expansion"),
        "entry_low": entry_low,
        "entry_high": entry_high,
        "trigger_price": _f(prediction.get("trigger_price"), entry_low if direction == "LONG" else entry_high),
        "invalidation_price": _f(prediction.get("invalidation_price"), _f(prediction.get("stop_loss"), _f(score.get("stop_loss")))),
        "stop_loss": _f(prediction.get("stop_loss"), _f(score.get("stop_loss"))),
        "tp1": _f(prediction.get("tp1"), _f(score.get("tp1"))),
        "tp2": _f(prediction.get("tp2"), _f(score.get("tp2"))),
        "tp3": _f(prediction.get("tp3"), _f(score.get("tp3"))),
        "chase_limit": chase_limit,
        "ttl_minutes": ttl_minutes,
        "atr_abs": atr_abs,
    }


def _valid_plan(plan: dict[str, Any]) -> bool:
    direction = str(plan.get("direction") or "")
    entry_low = _f(plan.get("entry_low"))
    entry_high = _f(plan.get("entry_high"))
    stop = _f(plan.get("stop_loss"))
    tp1 = _f(plan.get("tp1"))
    if min(entry_low, entry_high, stop, tp1) <= 0:
        return False
    if direction == "LONG":
        return stop < entry_low <= entry_high < tp1
    if direction == "SHORT":
        return tp1 < entry_low <= entry_high < stop
    return False


def _price_state(direction: str, current: float, row: dict[str, Any]) -> tuple[str, str]:
    entry_low = _f(row.get("entry_low"))
    entry_high = _f(row.get("entry_high"))
    chase_limit = _f(row.get("chase_limit"))
    if entry_low <= current <= entry_high:
        return "ENTER_NOW", "Precio dentro de la zona congelada; no perseguir fuera de ella."
    if direction == "LONG" and current > chase_limit:
        return "NO_CHASE", "La vela se fue por encima del plan. No perseguir; esperar retest de la zona."
    if direction == "SHORT" and current < chase_limit:
        return "NO_CHASE", "La vela se fue por debajo del plan. No perseguir; esperar retest de la zona."
    return "WAITING_ENTRY", "Plan vigente. Esperar que el precio llegue a la zona; no anticiparse."


def _invalidated(direction: str, current: float, invalidation: float) -> bool:
    if current <= 0 or invalidation <= 0:
        return False
    return current <= invalidation if direction == "LONG" else current >= invalidation


def _serialize(row: dict[str, Any], *, candidate_direction: str | None = None, action: str | None = None) -> dict[str, Any]:
    direction = str(row.get("direction") or "")
    candidate_direction = str(candidate_direction or row.get("last_candidate_direction") or direction)
    status = str(row.get("status") or "WAITING_ENTRY")
    contradiction = bool(candidate_direction and direction and candidate_direction != direction)
    default_action = {
        "WAITING_ENTRY": "ESPERAR_ENTRADA",
        "ENTER_NOW": "ENTRAR_SOLO_EN_ZONA",
        "NO_CHASE": "NO_PERSIGAS_ESPERA_RETEST",
        "IN_POSITION": "SOSTENER_PLAN_HASTA_TP_O_STOP",
        "INVALIDATED": "PLAN_INVALIDADO_NO_GIRAR_INMEDIATO",
        "EXPIRED": "PLAN_EXPIRADO_BUSCAR_NUEVO_SETUP",
        "CLOSED": "PLAN_CERRADO_COOLDOWN",
    }.get(status, "OBSERVAR")
    return {
        "version": THESIS_VERSION,
        "id": str(row.get("id") or ""),
        "symbol": row.get("symbol"),
        "direction": direction,
        "status": status,
        "action": action or default_action,
        "frozen_plan": True,
        "entry_low": _f(row.get("entry_low")),
        "entry_high": _f(row.get("entry_high")),
        "trigger_price": _f(row.get("trigger_price")),
        "invalidation_price": _f(row.get("invalidation_price")),
        "stop_loss": _f(row.get("stop_loss")),
        "tp1": _f(row.get("tp1")),
        "tp2": _f(row.get("tp2")),
        "tp3": _f(row.get("tp3")),
        "chase_limit": _f(row.get("chase_limit")),
        "initial_score": _f(row.get("initial_score")),
        "latest_score": _f(row.get("latest_score")),
        "candidate_direction_now": candidate_direction,
        "candidate_conflicts_with_plan": contradiction,
        "contradiction_count": int(row.get("contradiction_count") or 0),
        "created_at": _iso(row.get("created_at")),
        "expires_at": _iso(row.get("expires_at")),
        "cooldown_until": _iso(row.get("cooldown_until")),
        "rule": "El radar puede recalcular; dirección, entrada, stop y objetivos de la tesis no cambian hasta invalidación/cierre/expiración.",
    }


async def _latest(db: AsyncSession, symbol: str) -> dict[str, Any] | None:
    row = (await db.execute(text("""
        SELECT * FROM trade_theses
        WHERE symbol=:symbol
        ORDER BY created_at DESC
        LIMIT 1
    """), {"symbol": symbol})).mappings().first()
    return dict(row) if row else None


async def apply_trade_thesis(
    db: AsyncSession,
    *,
    symbol: str,
    score: dict[str, Any],
    prediction: dict[str, Any],
) -> dict[str, Any]:
    await ensure_trade_thesis_schema(db)
    now = datetime.now(timezone.utc)
    current = _f(score.get("current_price"))
    candidate_direction = str(prediction.get("direction") or score.get("direction") or "").upper()
    candidate_score = _f(prediction.get("preactivation_score"))
    candidate_phase = str(prediction.get("phase") or "SIN_SETUP")
    risk_score = _f(score.get("risk_score"), 100.0)
    existing = await _latest(db, symbol)

    if existing:
        status = str(existing.get("status") or "")
        cooldown_until = existing.get("cooldown_until")
        if status in TERMINAL_STATUSES and cooldown_until and cooldown_until > now:
            return _serialize(existing, candidate_direction=candidate_direction, action="COOLDOWN_NO_CAMBIAR_DE_LADO")

        if status in ACTIVE_STATUSES:
            expires_at = existing.get("expires_at")
            direction = str(existing.get("direction") or "").upper()
            invalidation = _f(existing.get("invalidation_price"))
            contradiction = candidate_direction not in {"", direction}
            contradiction_count = int(existing.get("contradiction_count") or 0) + (1 if contradiction else 0)

            if expires_at and expires_at <= now and status != "IN_POSITION":
                await db.execute(text("""
                    UPDATE trade_theses
                    SET status='EXPIRED', updated_at=NOW(), cooldown_until=NOW() + INTERVAL '10 minutes',
                        latest_score=:score, last_candidate_direction=:candidate_direction, last_price=:price
                    WHERE id=:id
                """), {"id": existing["id"], "score": candidate_score, "candidate_direction": candidate_direction, "price": current})
                await db.commit()
                existing.update({"status": "EXPIRED", "cooldown_until": now + timedelta(minutes=10), "latest_score": candidate_score, "last_candidate_direction": candidate_direction, "last_price": current})
                return _serialize(existing, candidate_direction=candidate_direction)

            if status != "IN_POSITION" and _invalidated(direction, current, invalidation):
                cooldown = now + timedelta(minutes=COOLDOWN_MINUTES)
                await db.execute(text("""
                    UPDATE trade_theses
                    SET status='INVALIDATED', updated_at=NOW(), closed_at=NOW(), cooldown_until=:cooldown,
                        latest_score=:score, last_candidate_direction=:candidate_direction, last_price=:price,
                        contradiction_count=:contradictions
                    WHERE id=:id
                """), {
                    "id": existing["id"], "cooldown": cooldown, "score": candidate_score,
                    "candidate_direction": candidate_direction, "price": current,
                    "contradictions": contradiction_count,
                })
                await db.commit()
                existing.update({"status": "INVALIDATED", "closed_at": now, "cooldown_until": cooldown, "latest_score": candidate_score, "last_candidate_direction": candidate_direction, "last_price": current, "contradiction_count": contradiction_count})
                return _serialize(existing, candidate_direction=candidate_direction)

            if status == "IN_POSITION":
                next_status = "IN_POSITION"
                message = "Operación activa: mantener el plan original; nunca ampliar el stop por un recálculo."
            else:
                next_status, message = _price_state(direction, current, existing)

            await db.execute(text("""
                UPDATE trade_theses
                SET status=:status, updated_at=NOW(), latest_score=:score,
                    last_candidate_direction=:candidate_direction, last_price=:price,
                    contradiction_count=:contradictions
                WHERE id=:id
            """), {
                "id": existing["id"], "status": next_status, "score": candidate_score,
                "candidate_direction": candidate_direction, "price": current,
                "contradictions": contradiction_count,
            })
            await db.commit()
            existing.update({"status": next_status, "latest_score": candidate_score, "last_candidate_direction": candidate_direction, "last_price": current, "contradiction_count": contradiction_count})
            action = "MANTENER_TESIS_NO_GIRAR" if contradiction else None
            payload = _serialize(existing, candidate_direction=candidate_direction, action=action)
            payload["message"] = message
            return payload

    plan = _candidate_plan(score, prediction)
    direction_match = candidate_direction == str(score.get("direction") or "").upper()
    create_allowed = (
        direction_match
        and candidate_score >= MIN_CREATE_SCORE
        and risk_score <= MAX_CREATE_RISK
        and candidate_phase in {"PREACTIVACION", "VIGILAR_CONFIRMACION", "ACTIVADO", "ESPERAR_RETEST"}
        and _valid_plan(plan)
    )
    if not create_allowed:
        return {
            "version": THESIS_VERSION,
            "symbol": symbol,
            "status": "OBSERVING",
            "action": "ESPERAR_SETUP_CLARO",
            "frozen_plan": False,
            "candidate_direction_now": candidate_direction,
            "candidate_score": round(candidate_score, 2),
            "candidate_phase": candidate_phase,
            "rule": "No se crea tesis hasta tener una preparación suficientemente fuerte y geometría válida.",
        }

    provisional = {
        **plan,
        "symbol": symbol,
    }
    if bool((prediction.get("sequence") or {}).get("chase_risk")):
        status = "NO_CHASE"
    else:
        status, _ = _price_state(plan["direction"], current, provisional)
        if candidate_phase != "ACTIVADO" and status == "ENTER_NOW":
            status = "WAITING_ENTRY"

    expires_at = now + timedelta(minutes=int(plan["ttl_minutes"]))
    metadata = {
        "created_from_phase": candidate_phase,
        "atr_abs_at_creation": plan["atr_abs"],
        "score_direction_at_creation": score.get("direction"),
        "prediction_direction_at_creation": candidate_direction,
        "paper_only": True,
    }
    row = (await db.execute(text("""
        INSERT INTO trade_theses (
            symbol, direction, status, setup_type, expires_at,
            entry_low, entry_high, trigger_price, invalidation_price, stop_loss,
            tp1, tp2, tp3, chase_limit, initial_score, latest_score,
            initial_risk_score, last_candidate_direction, last_price, metadata
        ) VALUES (
            :symbol, :direction, :status, :setup_type, :expires_at,
            :entry_low, :entry_high, :trigger_price, :invalidation_price, :stop_loss,
            :tp1, :tp2, :tp3, :chase_limit, :initial_score, :latest_score,
            :risk_score, :candidate_direction, :last_price, CAST(:metadata AS JSONB)
        )
        RETURNING *
    """), {
        "symbol": symbol, "direction": plan["direction"], "status": status,
        "setup_type": plan["setup_type"], "expires_at": expires_at,
        "entry_low": plan["entry_low"], "entry_high": plan["entry_high"],
        "trigger_price": plan["trigger_price"], "invalidation_price": plan["invalidation_price"],
        "stop_loss": plan["stop_loss"], "tp1": plan["tp1"], "tp2": plan["tp2"], "tp3": plan["tp3"],
        "chase_limit": plan["chase_limit"], "initial_score": candidate_score, "latest_score": candidate_score,
        "risk_score": risk_score, "candidate_direction": candidate_direction, "last_price": current,
        "metadata": json.dumps(metadata),
    })).mappings().one()
    await db.commit()
    payload = _serialize(dict(row), candidate_direction=candidate_direction)
    payload["message"] = "Nuevo plan congelado. El radar puede cambiar, pero este plan no se recalcula."
    return payload


def apply_thesis_to_score(score: dict[str, Any], prediction: dict[str, Any], thesis: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    out = dict(score)
    pred = dict(prediction)
    out["trade_thesis"] = thesis
    pred["trade_thesis"] = thesis
    status = str(thesis.get("status") or "OBSERVING")
    if not thesis.get("frozen_plan"):
        return out, pred

    out["direction"] = thesis["direction"]
    out["entry_low"] = thesis["entry_low"]
    out["entry_high"] = thesis["entry_high"]
    out["stop_loss"] = thesis["stop_loss"]
    out["tp1"] = thesis["tp1"]
    out["tp2"] = thesis["tp2"]
    out["tp3"] = thesis["tp3"]
    out["invalidation_price"] = thesis["invalidation_price"]

    metrics = dict(out.get("metrics") or {})
    metrics["thesis_id"] = thesis.get("id")
    metrics["thesis_status"] = status
    metrics["thesis_action"] = thesis.get("action")
    metrics["thesis_frozen_plan"] = True
    metrics["thesis_candidate_conflict"] = thesis.get("candidate_conflicts_with_plan", False)
    out["metrics"] = metrics

    if status in {"INVALIDATED", "EXPIRED", "CLOSED"} or str(thesis.get("action")) == "COOLDOWN_NO_CAMBIAR_DE_LADO":
        out["state"] = "NO_TRADE"
    elif status == "ENTER_NOW":
        # Never bypass a safety downgrade. READY remains READY only if the guarded
        # engine already allowed it; otherwise the frozen plan stays PREPARING.
        if str(score.get("state")) != "READY":
            out["state"] = "PREPARING"
    elif status in {"WAITING_ENTRY", "NO_CHASE", "IN_POSITION"}:
        out["state"] = "PREPARING" if status != "IN_POSITION" else "WATCH"

    pred["direction"] = thesis["direction"]
    pred["entry_low"] = thesis["entry_low"]
    pred["entry_high"] = thesis["entry_high"]
    pred["invalidation_price"] = thesis["invalidation_price"]
    pred["stop_loss"] = thesis["stop_loss"]
    pred["tp1"] = thesis["tp1"]
    pred["tp2"] = thesis["tp2"]
    pred["tp3"] = thesis["tp3"]
    pred["plan_is_frozen"] = True
    pred["plan_action"] = thesis.get("action")
    return out, pred


async def mark_thesis_entered(db: AsyncSession, symbol: str) -> None:
    await ensure_trade_thesis_schema(db)
    await db.execute(text("""
        UPDATE trade_theses
        SET status='IN_POSITION', entered_at=COALESCE(entered_at, NOW()), updated_at=NOW()
        WHERE id = (
            SELECT id FROM trade_theses
            WHERE symbol=:symbol AND status IN ('WAITING_ENTRY','ENTER_NOW','NO_CHASE')
            ORDER BY created_at DESC LIMIT 1
        )
    """), {"symbol": symbol})
    await db.commit()


async def mark_thesis_closed(db: AsyncSession, symbol: str, *, reason: str) -> None:
    await ensure_trade_thesis_schema(db)
    cooldown = datetime.now(timezone.utc) + timedelta(minutes=15)
    await db.execute(text("""
        UPDATE trade_theses
        SET status='CLOSED', closed_at=NOW(), updated_at=NOW(), cooldown_until=:cooldown,
            metadata = metadata || CAST(:patch AS JSONB)
        WHERE id = (
            SELECT id FROM trade_theses
            WHERE symbol=:symbol AND status='IN_POSITION'
            ORDER BY created_at DESC LIMIT 1
        )
    """), {"symbol": symbol, "cooldown": cooldown, "patch": json.dumps({"close_reason": reason})})
    await db.commit()
