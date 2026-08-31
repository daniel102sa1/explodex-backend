from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.explosion_intelligence import load_timing_model, timing_memory_adjustment
from app.services.higher_timeframe_context import alignment, batch_higher_timeframe_context
from app.services.ignition_engine import build_ignition_signal
from app.services.liquidity_target_engine import build_liquidity_targets
from app.services.trade_thesis import apply_trade_thesis, apply_thesis_to_score

HEART_PERSISTENCE_VERSION = "heart_persistence_v4_learned_timing_liquidity_htf"


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
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


def _stack_checks(prediction: dict[str, Any]) -> tuple[bool, list[str]]:
    fingerprint = _json(prediction.get("premove_fingerprint"))
    stack = _json(prediction.get("prediction_stack_v5"))
    master = _json(stack.get("master_decision"))
    risk_veto = _json(stack.get("risk_veto"))
    timing = _json(stack.get("entry_timing"))
    sequence = _json(prediction.get("sequence"))
    decision_guard = _json(prediction.get("decision_guard"))

    trade_now = bool(fingerprint.get("trade_now_ready")) or str(fingerprint.get("trade_class") or "").upper() == "TRADE_NOW"
    master_yes = str(master.get("state") or "").upper() == "YES"
    timing_enter = str(timing.get("state") or "").upper() in {"ENTER_NOW", "TRADE_NOW"} or trade_now
    veto_clear = not bool(risk_veto.get("blocked"))
    not_chasing = not bool(sequence.get("chase_risk")) and not bool(risk_veto.get("chase"))
    not_invalidated = not bool(risk_veto.get("invalidated")) and not bool(risk_veto.get("hard_block"))
    risk_guard_pass = bool(sequence.get("risk_guard_pass", decision_guard.get("risk_guard_pass", True)))

    checks = {
        "fingerprint_trade_now": trade_now,
        "master_yes": master_yes,
        "timing_enter": timing_enter,
        "veto_clear": veto_clear,
        "not_chasing": not_chasing,
        "not_invalidated": not_invalidated,
        "risk_guard_pass": risk_guard_pass,
    }
    return all(checks.values()), [key for key, ok in checks.items() if not ok]


def _inside_zone(price: float, low: float, high: float) -> bool:
    return price > 0 and low > 0 and high > 0 and min(low, high) <= price <= max(low, high)


def _apply_timing_memory(ignition: dict[str, Any], timing_model: dict[str, Any]) -> dict[str, Any]:
    out = dict(ignition)
    raw_score = _f(out.get("score"))
    memory = timing_memory_adjustment(out, timing_model)
    out["raw_score"] = round(raw_score, 1)
    out["timing_memory"] = memory
    adjusted = _f(memory.get("adjusted_score"), raw_score)
    out["score"] = round(adjusted, 1)

    if bool(memory.get("can_influence_entry")):
        blockers = list(out.get("blockers") or [])
        support = int(out.get("supporting_components") or 0)
        strong = int(out.get("strong_components") or 0)
        ready = adjusted >= 82.0 and support >= 4 and strong >= 2 and not blockers
        out["fast_path_ready"] = ready
        if ready:
            out["stage"] = "IGNITING"
        elif blockers:
            out["stage"] = "BLOCKED"
        elif adjusted >= 72.0 and support >= 3:
            out["stage"] = "ARMED"
        elif adjusted >= 58.0:
            out["stage"] = "LOADING"
        else:
            out["stage"] = "QUIET"
        out["memory_adjusted_entry"] = True
    else:
        out["memory_adjusted_entry"] = False
    return out


def _canonical_action(
    score: dict[str, Any],
    prediction: dict[str, Any],
    thesis: dict[str, Any],
    ignition: dict[str, Any],
) -> dict[str, Any]:
    direction = str(score.get("direction") or prediction.get("direction") or "").upper()
    stack_ready, stack_missing = _stack_checks(prediction)
    sequence = _json(prediction.get("sequence"))
    stack = _json(prediction.get("prediction_stack_v5"))
    risk_veto = _json(stack.get("risk_veto"))

    low = _f(score.get("entry_low"))
    high = _f(score.get("entry_high"))
    current = _f(score.get("current_price"))
    in_zone = _inside_zone(current, low, high)

    terminal = str(thesis.get("status") or "") in {"INVALIDATED", "EXPIRED", "CLOSED"}
    cooldown = str(thesis.get("action") or "") == "COOLDOWN_NO_CAMBIAR_DE_LADO"
    chase = bool(sequence.get("chase_risk")) or bool(risk_veto.get("chase")) or str(thesis.get("status") or "") == "NO_CHASE"
    hard_block = bool(risk_veto.get("blocked")) or bool(risk_veto.get("invalidated")) or bool(risk_veto.get("hard_block"))
    risk_guard_pass = bool(sequence.get("risk_guard_pass", _json(prediction.get("decision_guard")).get("risk_guard_pass", True)))
    risk_ok = _f(score.get("risk_score"), 100.0) <= 48.0
    ignition_ready = bool(ignition.get("fast_path_ready"))
    entry_signal_ready = stack_ready or ignition_ready

    allowed = (
        entry_signal_ready
        and in_zone
        and risk_guard_pass
        and risk_ok
        and not chase
        and not hard_block
        and not terminal
        and not cooldown
        and str(score.get("state") or "") != "NO_TRADE"
    )

    if allowed:
        action = "ENTRAR_LONG" if direction == "LONG" else "ENTRAR_SHORT"
        via = "IGNITION_FAST_PATH" if ignition_ready and not stack_ready else "ADVANCED_STACK"
        if ignition_ready and bool(_json(ignition.get("timing_memory")).get("can_influence_entry")):
            via = "LEARNED_IGNITION_FAST_PATH" if not stack_ready else "ADVANCED_STACK_PLUS_MEMORY"
        reason = (
            "Ignición fuerte confirmada por flujo/volumen/OI y memoria histórica suficiente; bloqueos duros limpios."
            if via == "LEARNED_IGNITION_FAST_PATH"
            else "Ignición fuerte confirmada; flujo/volumen/OI comienzan a expandirse y los bloqueos duros están limpios."
            if "IGNITION" in via
            else "Stack avanzado en TRADE_NOW/YES, sin veto ni chase y precio dentro de la zona."
        )
    elif terminal or cooldown or hard_block or not risk_guard_pass:
        action = "NO_ENTRAR"
        via = "BLOCKED"
        reason = "Hay invalidación, veto duro, cooldown o Risk Guard bloqueando la operación."
    elif chase:
        action = "ESPERAR_RETEST"
        via = "NO_CHASE"
        reason = "La dirección puede seguir siendo válida, pero el precio ya salió de la zona; no perseguir."
    else:
        action = "ESPERAR"
        via = "WAITING"
        reason = "La preparación existe, pero todavía falta ignición/confirmación o que el precio esté en la zona."

    missing = list(stack_missing)
    if not ignition_ready and "ignition_fast_path" not in missing:
        missing.append("ignition_fast_path")
    if not in_zone and "price_in_entry_zone" not in missing:
        missing.append("price_in_entry_zone")

    return {
        "action": action,
        "should_enter": allowed,
        "direction": direction,
        "via": via,
        "price_in_entry_zone": in_zone,
        "advanced_stack_ready": stack_ready,
        "advanced_stack_missing": missing,
        "ignition_fast_path_ready": ignition_ready,
        "ignition_score": ignition.get("score"),
        "ignition_raw_score": ignition.get("raw_score", ignition.get("score")),
        "ignition_stage": ignition.get("stage"),
        "timing_memory": ignition.get("timing_memory"),
        "reason": reason,
    }


async def canonicalize_scanner_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.state,
               s.setup_score, s.risk_score, s.confidence_pct, s.current_price,
               s.entry_low, s.entry_high, s.invalidation_price, s.stop_loss,
               s.tp1, s.tp2, s.tp3, s.expected_move_min_pct, s.expected_move_max_pct,
               s.expected_duration_min_minutes, s.expected_duration_max_minutes,
               s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.created_at ASC
    """), {"run_id": run_id})).mappings().all()

    updated = 0
    frozen = 0
    blocked = 0
    execution_ready = 0
    ignition_ready_count = 0
    memory_influenced = 0
    liquidity_aligned = 0
    htf_aligned = 0
    htf_conflicted = 0
    actions: dict[str, int] = {}

    try:
        timing_model = await load_timing_model(db)
    except Exception:
        timing_model = {"sample": 0, "buckets": {}}

    ranked_for_htf = sorted(
        (dict(item) for item in rows),
        key=lambda item: _f(item.get("setup_score")),
        reverse=True,
    )
    htf_symbols = [str(item.get("symbol")) for item in ranked_for_htf[:8] if _f(item.get("setup_score")) >= 64.0]
    try:
        htf_map = await batch_higher_timeframe_context(htf_symbols, concurrency=4)
    except Exception:
        htf_map = {}

    for raw in rows:
        row = dict(raw)
        reason = _json(row.get("reason"))
        prediction = _json(reason.get("prediction"))
        if not prediction:
            continue

        score = {
            "symbol": row["symbol"],
            "direction": row.get("direction"),
            "state": row.get("state"),
            "setup_score": _f(row.get("setup_score")),
            "risk_score": _f(row.get("risk_score"), 100.0),
            "confidence_pct": row.get("confidence_pct"),
            "current_price": _f(row.get("current_price")),
            "entry_low": _f(row.get("entry_low")),
            "entry_high": _f(row.get("entry_high")),
            "invalidation_price": _f(row.get("invalidation_price")),
            "stop_loss": _f(row.get("stop_loss")),
            "tp1": _f(row.get("tp1")),
            "tp2": _f(row.get("tp2")),
            "tp3": _f(row.get("tp3")),
            "expected_move_min_pct": _f(row.get("expected_move_min_pct")),
            "expected_move_max_pct": _f(row.get("expected_move_max_pct")),
            "expected_duration_min_minutes": row.get("expected_duration_min_minutes"),
            "expected_duration_max_minutes": row.get("expected_duration_max_minutes"),
            "metrics": _json(reason.get("metrics")),
            "components": _json(reason.get("components")),
            "coinglass": _json(reason.get("coinglass")),
        }

        raw_ignition = build_ignition_signal(score, prediction)
        ignition = _apply_timing_memory(raw_ignition, timing_model)
        if ignition.get("memory_adjusted_entry"):
            memory_influenced += 1

        thesis = await apply_trade_thesis(db, symbol=row["symbol"], score=score, prediction=prediction)
        score, prediction = apply_thesis_to_score(score, prediction, thesis)
        liquidity = build_liquidity_targets(score, prediction, thesis)
        if liquidity.get("aligned_with_thesis"):
            liquidity_aligned += 1

        htf = htf_map.get(str(row["symbol"])) or {
            "version": "higher_timeframe_context_v1",
            "symbol": row["symbol"],
            "bias": "NOT_FETCHED",
            "frames": {},
            "role": "Only strongest candidates fetch 4h/6h/1d context to control API load.",
        }
        htf_alignment = alignment(str(score.get("direction") or ""), htf)
        if htf_alignment.get("strong_alignment"):
            htf_aligned += 1
        if htf_alignment.get("strong_conflict"):
            htf_conflicted += 1

        decision = _canonical_action(score, prediction, thesis, ignition)
        decision["higher_timeframe_alignment"] = htf_alignment
        allowed = bool(decision.get("should_enter"))

        if allowed:
            score["state"] = "READY"
            execution_ready += 1
        if ignition.get("fast_path_ready"):
            ignition_ready_count += 1
        action = str(decision.get("action") or "ESPERAR")
        actions[action] = actions.get(action, 0) + 1
        if thesis.get("frozen_plan"):
            frozen += 1
        if str(score.get("state")) == "NO_TRADE":
            blocked += 1

        heart = {
            "version": "explodex_heart_v5_explosion_intelligence_htf",
            "persistence_version": HEART_PERSISTENCE_VERSION,
            "symbol": row["symbol"],
            "direction": score.get("direction"),
            "state": score.get("state"),
            "execution_allowed": allowed,
            "action_decision": decision,
            "ignition": ignition,
            "liquidity_intelligence": liquidity,
            "higher_timeframe": htf,
            "higher_timeframe_alignment": htf_alignment,
            "prediction_phase": prediction.get("phase"),
            "prediction_type": prediction.get("type"),
            "thesis": thesis,
            "plan": {
                "source": "FROZEN_THESIS" if thesis.get("frozen_plan") else "CURRENT_HEART",
                "action": thesis.get("action") if thesis.get("frozen_plan") else "ESPERAR_CONFIRMACION",
                "entry_low": score.get("entry_low"),
                "entry_high": score.get("entry_high"),
                "invalidation_price": score.get("invalidation_price", score.get("stop_loss")),
                "stop_loss": score.get("stop_loss"),
                "tp1": score.get("tp1"),
                "tp2": score.get("tp2"),
                "tp3": score.get("tp3"),
                "do_not_recalculate": bool(thesis.get("frozen_plan")),
            },
            "learning": {
                "timing_model_sample": int(_json(timing_model).get("sample") or 0),
                "timing_memory": ignition.get("timing_memory"),
                "memory_can_influence_entry": bool(_json(ignition.get("timing_memory")).get("can_influence_entry")),
                "higher_timeframe_is_context_only": True,
            },
            "score_is_probability": False,
        }
        reason["prediction"] = prediction
        reason["metrics"] = score.get("metrics") or {}
        reason["explodex_heart"] = heart

        await db.execute(text("""
            UPDATE signals
            SET direction=:direction, state=:state,
                entry_low=:entry_low, entry_high=:entry_high,
                invalidation_price=:invalidation_price, stop_loss=:stop_loss,
                tp1=:tp1, tp2=:tp2, tp3=:tp3,
                reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {
            "signal_id": row["signal_id"],
            "direction": score.get("direction"),
            "state": score.get("state"),
            "entry_low": score.get("entry_low"),
            "entry_high": score.get("entry_high"),
            "invalidation_price": score.get("invalidation_price", score.get("stop_loss")),
            "stop_loss": score.get("stop_loss"),
            "tp1": score.get("tp1"),
            "tp2": score.get("tp2"),
            "tp3": score.get("tp3"),
            "reason": json.dumps(reason),
        })
        updated += 1

    await db.commit()
    return {
        "version": HEART_PERSISTENCE_VERSION,
        "seen": len(rows),
        "updated": updated,
        "frozen_theses": frozen,
        "blocked": blocked,
        "execution_ready": execution_ready,
        "ignition_ready": ignition_ready_count,
        "memory_influenced": memory_influenced,
        "liquidity_aligned": liquidity_aligned,
        "htf_aligned": htf_aligned,
        "htf_conflicted": htf_conflicted,
        "htf_symbols_checked": len(htf_symbols),
        "timing_model_sample": int(_json(timing_model).get("sample") or 0),
        "actions": actions,
    }
