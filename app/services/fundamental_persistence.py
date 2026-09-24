from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.fundamental_intelligence import VERSION as FUNDAMENTAL_VERSION, fundamental_context_for_symbol
from app.services.pump_state_machine import VERSION as PUMP_VERSION, classify_pump_state

VERSION = "fundamental_persistence_v1_shadow"


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


def _unavailable_fundamental(symbol: str, reason: str = "not_fetched") -> dict[str, Any]:
    return {
        "version": FUNDAMENTAL_VERSION,
        "enabled": bool(settings.fundamentals_enabled),
        "available": False,
        "paper_only": True,
        "shadow_only": True,
        "symbol": symbol,
        "reason": reason,
        "risk": {
            "risk_score": None,
            "state": "UNAVAILABLE",
            "risk_multiplier_cap": 1.0,
            "flags": [reason],
        },
        "can_create_entry": False,
        "can_change_direction": False,
        "can_raise_leverage": False,
        "can_reduce_risk": False,
    }


async def persist_fundamental_intelligence_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    """Attach point-in-time-ish market/tokenomics context and a pump state.

    CoinGecko market/tokenomics is fetched only for the strongest scanner rows to
    control API traffic. Every signal receives a pump-state snapshot. Neither
    layer can create an entry, flip direction or increase leverage.
    """
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.state,
               s.setup_score, s.risk_score, s.current_price,
               s.expected_duration_min_minutes, s.expected_duration_max_minutes,
               s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.setup_score DESC NULLS LAST, s.risk_score ASC NULLS LAST
    """), {"run_id": run_id})).mappings().all()]

    if not rows:
        return {
            "version": VERSION,
            "fundamental_version": FUNDAMENTAL_VERSION,
            "pump_version": PUMP_VERSION,
            "seen": 0,
            "updated": 0,
        }

    fetch_limit = max(0, min(int(settings.fundamentals_max_scanner_candidates), len(rows)))
    fetch_rows = rows[:fetch_limit] if settings.fundamentals_enabled else []
    semaphore = asyncio.Semaphore(3)

    async def load(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        symbol = str(row.get("symbol") or "").upper()
        try:
            async with semaphore:
                value = await fundamental_context_for_symbol(symbol)
        except Exception as exc:
            value = _unavailable_fundamental(symbol, f"runtime_error:{type(exc).__name__}")
            value["error"] = str(exc)[:300]
        return symbol, value

    fetched = await asyncio.gather(*(load(row) for row in fetch_rows))
    fundamental_by_symbol = {symbol: value for symbol, value in fetched}

    updated = 0
    available = 0
    high_risk = 0
    pump_counts: dict[str, int] = {}

    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        metrics = _d(reason.get("metrics"))
        fundamental = fundamental_by_symbol.get(symbol) or _unavailable_fundamental(
            symbol,
            "outside_fundamental_fetch_budget" if settings.fundamentals_enabled else "fundamentals_disabled",
        )
        risk = _d(fundamental.get("risk"))
        if bool(fundamental.get("available")):
            available += 1
        if _f(risk.get("risk_score")) >= 60.0:
            high_risk += 1

        score = {
            "symbol": symbol,
            "direction": row.get("direction"),
            "state": row.get("state"),
            "setup_score": _f(row.get("setup_score")),
            "risk_score": _f(row.get("risk_score"), 100.0),
            "current_price": _f(row.get("current_price")),
            "expected_duration_min_minutes": row.get("expected_duration_min_minutes"),
            "expected_duration_max_minutes": row.get("expected_duration_max_minutes"),
            "metrics": metrics,
        }
        pump_state = classify_pump_state(
            score=score,
            prediction=prediction,
            fundamental=fundamental,
        )
        state = str(pump_state.get("state") or "NORMAL")
        pump_counts[state] = pump_counts.get(state, 0) + 1

        market = _d(fundamental.get("market"))
        tokenomics = _d(fundamental.get("tokenomics"))
        metrics.update({
            "fundamental_available": bool(fundamental.get("available")),
            "fundamental_risk_score": risk.get("risk_score"),
            "fundamental_risk_state": risk.get("state"),
            "fundamental_risk_multiplier_cap": risk.get("risk_multiplier_cap"),
            "market_cap_usd": market.get("market_cap_usd"),
            "fully_diluted_valuation_usd": market.get("fully_diluted_valuation_usd"),
            "fdv_to_market_cap": tokenomics.get("fdv_to_market_cap"),
            "circulating_to_total_supply": tokenomics.get("circulating_to_total_supply"),
            "volume_to_market_cap_24h": _d(fundamental.get("liquidity_proxy")).get("volume_to_market_cap_24h"),
            "pump_state": state,
            "pump_state_score": pump_state.get("state_score"),
            "pump_state_direction": pump_state.get("dominant_direction"),
        })
        reason["metrics"] = metrics
        reason["fundamental_intelligence"] = fundamental
        reason["pump_state_machine"] = pump_state
        if prediction:
            prediction["fundamental_intelligence"] = fundamental
            prediction["pump_state_machine"] = pump_state
            reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals
            SET reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {
            "signal_id": row["signal_id"],
            "reason": json.dumps(reason),
        })
        updated += 1

    await db.commit()
    return {
        "version": VERSION,
        "fundamental_version": FUNDAMENTAL_VERSION,
        "pump_version": PUMP_VERSION,
        "seen": len(rows),
        "fundamentals_requested": len(fetch_rows),
        "fundamentals_available": available,
        "high_tokenomics_risk": high_risk,
        "updated": updated,
        "pump_states": pump_counts,
        "paper_only": True,
        "shadow_only": True,
        "can_create_entry": False,
        "can_change_direction": False,
        "can_raise_leverage": False,
    }
