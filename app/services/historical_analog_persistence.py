from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.historical_market_brain import VERSION, analog_context


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
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def persist_historical_analogs_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    """Attach historical analog evidence before Heart canonicalization.

    This module is shadow-only. It writes evidence into signal.reason but does not
    change direction, state, setup score or risk by itself.
    """
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.state,
               s.setup_score, s.risk_score, s.current_price, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.setup_score DESC, s.created_at ASC
        LIMIT :limit
    """), {
        "run_id": run_id,
        "limit": max(1, min(int(settings.fundamentals_max_scanner_candidates), 12)),
    })).mappings().all()

    updated = 0
    usable = 0
    errors: list[str] = []
    for raw in rows:
        row = dict(raw)
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        if not prediction:
            continue
        score = {
            "symbol": row.get("symbol"),
            "direction": row.get("direction"),
            "state": row.get("state"),
            "setup_score": _f(row.get("setup_score")),
            "risk_score": _f(row.get("risk_score")),
            "current_price": _f(row.get("current_price")),
            "metrics": _d(reason.get("metrics")),
        }
        try:
            analog = await analog_context(
                db,
                symbol=str(row.get("symbol") or ""),
                scored=score,
                prediction=prediction,
            )
        except Exception as exc:
            await db.rollback()
            analog = {
                "version": VERSION,
                "available": False,
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "policy": {
                    "shadow_only": True,
                    "can_create_entry": False,
                    "can_raise_leverage": False,
                },
            }
            errors.append(f"{row.get('symbol')}:{analog['error']}")

        reason["historical_analog"] = analog
        metrics = _d(reason.get("metrics"))
        metrics["historical_analog_sample"] = analog.get("sample")
        metrics["historical_analog_top_similarity"] = analog.get("top_similarity")
        metrics["historical_analog_status"] = analog.get("status")
        metrics["historical_analog_oos_status"] = _d(analog.get("out_of_sample")).get("status")
        reason["metrics"] = metrics

        await db.execute(text("""
            UPDATE signals
            SET reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {
            "signal_id": row["signal_id"],
            "reason": json.dumps(reason),
        })
        updated += 1
        usable += int(str(analog.get("status") or "") == "USABLE")

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "updated": updated,
        "usable": usable,
        "errors": errors[:5],
        "shadow_only": True,
    }
