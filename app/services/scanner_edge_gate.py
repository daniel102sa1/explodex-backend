from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.edge_engine import capture_recent_signals, similar_case_summary
from app.services.expected_value_gate import evaluate_expected_value_gate


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
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


async def apply_edge_gate_to_scanner_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    """Attach learned expected-value evidence to the latest scanner run.

    Only calibrated negative evidence can hard-block READY. Insufficient sample
    remains LEARNING so PAPER can keep collecting outcomes.
    """
    # Make the just-created signals available as current observations before
    # searching their historical neighbors.
    capture = await capture_recent_signals(db, limit=250)

    result = await db.execute(
        text(
            """
            SELECT s.id::text AS signal_id, sy.symbol, s.state, s.reason
            FROM signals s
            JOIN symbols sy ON sy.id=s.symbol_id
            WHERE s.scanner_run_id=CAST(:run_id AS UUID)
            ORDER BY s.setup_score DESC
            """
        ),
        {"run_id": run_id},
    )
    rows = [dict(r) for r in result.mappings().all()]
    checked = 0
    blocked = 0
    positive = 0
    learning = 0
    details: list[dict[str, Any]] = []

    for row in rows:
        reason = _json(row.get("reason"))
        prediction = _json(reason.get("prediction"))
        decision_guard = _json(prediction.get("decision_guard"))
        sequence = _json(prediction.get("sequence"))
        rr1 = _f(
            decision_guard.get("reward_risk_tp1"),
            _f(sequence.get("reward_risk_tp1")),
        )

        similar = await similar_case_summary(db, str(row["symbol"]))
        gate = evaluate_expected_value_gate(similar=similar, reward_risk_tp1=rr1)
        checked += 1
        if gate["status"] == "LEARNING":
            learning += 1
        elif gate["pass"]:
            positive += 1

        reason["edge_gate"] = gate
        reason["similar_cases"] = {
            "decided": similar.get("decided"),
            "sample": similar.get("sample"),
            "avg_similarity_pct": similar.get("avg_similarity_pct"),
            "weighted_win_rate_pct": similar.get("weighted_win_rate_pct"),
            "weighted_avg_r": similar.get("weighted_avg_r"),
            "calibration_status": similar.get("calibration_status"),
        }

        hard_block = bool(gate.get("hard_block")) and str(row.get("state")) == "READY"
        new_state = "PREPARING" if hard_block else str(row.get("state") or "NO_TRADE")
        if hard_block:
            blocked += 1
            metrics = _json(reason.get("metrics"))
            rejects = list(metrics.get("reject_reasons") or [])
            if "edge_expected_value_blocked" not in rejects:
                rejects.append("edge_expected_value_blocked")
            metrics["reject_reasons"] = rejects
            metrics["edge_gate_pass"] = False
            metrics["edge_gate_status"] = gate.get("status")
            metrics["edge_expected_value_r"] = gate.get("expected_value_r")
            reason["metrics"] = metrics

        await db.execute(
            text(
                """
                UPDATE signals
                SET state=:state, reason=CAST(:reason AS JSONB), updated_at=NOW()
                WHERE id=CAST(:signal_id AS UUID)
                """
            ),
            {
                "state": new_state,
                "reason": json.dumps(reason),
                "signal_id": row["signal_id"],
            },
        )

        if hard_block:
            blocks = ", ".join(str(x) for x in gate.get("blocks") or []) or "expectativa insuficiente"
            await db.execute(
                text(
                    """
                    UPDATE alerts
                    SET severity='WARNING',
                        title=:title,
                        message=:message,
                        is_sent=FALSE
                    WHERE signal_id=CAST(:signal_id AS UUID)
                      AND severity='READY'
                    """
                ),
                {
                    "signal_id": row["signal_id"],
                    "title": f"⚠️ {row['symbol']} EDGE GATE · NO TRADE",
                    "message": (
                        f"La señal técnica llegó a READY, pero la evidencia histórica calibrada la bloqueó. "
                        f"Motivos: {blocks}. EV {gate.get('expected_value_r')}R | "
                        f"win rate ponderado {gate.get('weighted_win_rate_pct')}% | "
                        f"similitud media {gate.get('avg_similarity_pct')}%. "
                        "No es garantía de pérdida; el sistema se abstiene porque la expectativa histórica no compensa el riesgo."
                    ),
                },
            )

        details.append(
            {
                "symbol": row["symbol"],
                "state_before": row.get("state"),
                "state_after": new_state,
                "gate": gate.get("status"),
                "ev_r": gate.get("expected_value_r"),
                "sample": gate.get("decided"),
            }
        )

    await db.commit()
    return {
        "captured": capture,
        "checked": checked,
        "blocked_ready": blocked,
        "positive_edge": positive,
        "learning": learning,
        "details": details[:20],
    }
