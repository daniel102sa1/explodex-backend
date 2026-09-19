from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client
from app.services.quant_brain import VERSION as QUANT_VERSION, build_quant_brain
from app.services.shadow_forecast_memory import shadow_calibration_report

VERSION = "quant_brain_persistence_v1"
MAX_SYMBOLS = 20
CONCURRENCY = 4


def _d(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def persist_quant_brain_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.state,
               s.setup_score, s.risk_score, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.setup_score DESC NULLS LAST, s.risk_score ASC NULLS LAST
        LIMIT :limit
    """), {"run_id": run_id, "limit": MAX_SYMBOLS})).mappings().all()]

    if not rows:
        return {"version": VERSION, "seen": 0, "updated": 0, "quant_version": QUANT_VERSION}

    try:
        btc_5m = await binance_client.klines("BTCUSDT", "5m", 260)
    except Exception as exc:
        return {
            "version": VERSION,
            "seen": len(rows),
            "updated": 0,
            "status": "ERROR",
            "error": f"BTC:{type(exc).__name__}:{str(exc)[:240]}",
            "quant_version": QUANT_VERSION,
        }

    try:
        calibration_report = await shadow_calibration_report(db, horizon="1h")
        calibration_by_direction = {
            str(item.get("direction") or "").upper(): dict(item)
            for item in calibration_report.get("rows", [])
            if isinstance(item, dict)
        }
    except Exception:
        calibration_by_direction = {}

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def analyze(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        symbol = str(row.get("symbol") or "").upper()
        reason = _d(row.get("reason"))
        metrics = _d(reason.get("metrics"))
        direction = str(row.get("direction") or "").upper()
        calibration = calibration_by_direction.get(direction, {
            "direction": direction,
            "sample": 0,
            "status": "CALIBRATING",
            "bounded_conviction_adjustment": 0.0,
        })
        try:
            async with semaphore:
                if symbol == "BTCUSDT":
                    k5 = btc_5m
                else:
                    k5 = await binance_client.klines(symbol, "5m", 260)
                k15 = await binance_client.klines(symbol, "15m", 140)
            quant = build_quant_brain(
                symbol=symbol,
                direction=direction,
                klines_5m=k5,
                klines_15m=k15,
                btc_5m=btc_5m,
                metrics=metrics,
                calibration=calibration,
            )
        except Exception as exc:
            quant = {
                "version": QUANT_VERSION,
                "symbol": symbol,
                "direction": direction,
                "available": False,
                "reason": "quant_runtime_error",
                "error": f"{type(exc).__name__}:{str(exc)[:240]}",
                "block_new_entry": False,
                "risk_multiplier": 0.70,
                "score_is_probability": False,
            }
        return row, quant

    results = await asyncio.gather(*(analyze(row) for row in rows))

    updated = 0
    blocked = 0
    conflicts = 0
    supports = 0
    unavailable = 0
    risk_multipliers: list[float] = []

    for row, quant in results:
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart:
            continue

        if not bool(quant.get("available")):
            unavailable += 1
        if bool(quant.get("block_new_entry")):
            blocked += 1
        if bool(quant.get("strong_conflict")):
            conflicts += 1
        if bool(quant.get("supports_direction")):
            supports += 1
        risk_multipliers.append(_f(quant.get("risk_multiplier"), 0.70))

        decision = _d(heart.get("action_decision"))
        originally_entering = bool(decision.get("should_enter"))
        downgraded = False

        if originally_entering and bool(quant.get("block_new_entry")):
            decision["should_enter"] = False
            decision["action"] = "NO_ENTRAR"
            decision["via"] = "QUANT_BRAIN_BLOCK"
            decision["reason"] = (
                "El setup técnico estaba listo, pero la capa cuantitativa detectó conflicto/riesgo estadístico fuerte. "
                "No se abre una entrada nueva."
            )
            downgraded = True
        elif originally_entering and bool(quant.get("strong_conflict")):
            decision["should_enter"] = False
            decision["action"] = "ESPERAR"
            decision["via"] = "QUANT_BRAIN_CONFLICT"
            decision["reason"] = (
                "La señal técnica existe, pero la evidencia cuantitativa no acompaña con suficiente consistencia. "
                "Se espera confirmación; el Quant Brain no cambia de lado por sí solo."
            )
            downgraded = True

        decision["quant_brain_stance"] = quant.get("stance")
        decision["quant_directional_edge"] = quant.get("directional_edge")
        decision["quant_evidence_strength"] = quant.get("evidence_strength")
        decision["quant_risk_multiplier"] = quant.get("risk_multiplier")
        decision["quant_preferred_strategy"] = _d(quant.get("strategy_selector")).get("preferred")

        heart["action_decision"] = decision
        heart["execution_allowed"] = bool(decision.get("should_enter"))
        heart["quant_brain"] = quant
        learning = _d(heart.get("learning"))
        learning["quant_brain"] = {
            "version": quant.get("version"),
            "calibration": quant.get("calibration"),
            "monte_carlo_is_model_estimate": True,
            "scores_are_not_probabilities": True,
        }
        heart["learning"] = learning

        new_state = row.get("state")
        if downgraded and str(new_state or "").upper() == "READY":
            new_state = "PREPARING"

        reason["quant_brain"] = quant
        reason["explodex_heart"] = heart
        if prediction:
            prediction["explodex_heart"] = heart
            prediction["quant_brain"] = quant
            reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals
            SET state=:state, reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {
            "signal_id": row["signal_id"],
            "state": new_state,
            "reason": json.dumps(reason),
        })
        updated += 1

    await db.commit()
    return {
        "version": VERSION,
        "quant_version": QUANT_VERSION,
        "seen": len(rows),
        "updated": updated,
        "blocked": blocked,
        "strong_conflicts": conflicts,
        "supports": supports,
        "unavailable": unavailable,
        "average_risk_multiplier": round(sum(risk_multipliers) / len(risk_multipliers), 4) if risk_multipliers else None,
        "single_heart": True,
        "can_flip_direction": False,
        "can_upgrade_wait_to_entry": False,
        "can_reduce_or_block": True,
    }
