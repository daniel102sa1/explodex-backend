from __future__ import annotations

import json
from statistics import median
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.binance import binance_client

VERSION = "market_breadth_v1"


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


def _pct(part: int, total: int) -> float:
    return round(part / total * 100.0, 1) if total else 0.0


def _short_frame(values: list[float]) -> dict[str, Any]:
    clean = [float(v) for v in values]
    if not clean:
        return {"available": False, "sample": 0, "advance_pct": 0.0, "decline_pct": 0.0, "median_change_pct": 0.0, "score": 0.0}
    advances = sum(1 for v in clean if v > 0.05)
    declines = sum(1 for v in clean if v < -0.05)
    score = (advances - declines) / len(clean) * 100.0
    return {
        "available": True,
        "sample": len(clean),
        "advance_pct": _pct(advances, len(clean)),
        "decline_pct": _pct(declines, len(clean)),
        "median_change_pct": round(median(clean), 4),
        "score": round(score, 1),
    }


def _regime(score: float, decline24: float, advance24: float, median24: float) -> str:
    if score <= -62 and decline24 >= 72 and median24 <= -2.0:
        return "PANIC_DOWN"
    if score >= 62 and advance24 >= 72 and median24 >= 2.0:
        return "EUPHORIA_UP"
    if score <= -28:
        return "BEARISH"
    if score >= 28:
        return "BULLISH"
    return "MIXED"


def _alignment(direction: str, breadth_score: float, regime: str) -> dict[str, Any]:
    direction = str(direction or "").upper()
    signed = breadth_score if direction == "LONG" else -breadth_score if direction == "SHORT" else 0.0
    if signed >= 45:
        state, multiplier = "STRONGLY_ALIGNED", 1.0
    elif signed >= 20:
        state, multiplier = "ALIGNED", 1.0
    elif signed <= -55:
        state, multiplier = "STRONG_CONFLICT", 0.55
    elif signed <= -25:
        state, multiplier = "CONFLICT", 0.75
    else:
        state, multiplier = "NEUTRAL", 1.0
    if regime in {"PANIC_DOWN", "EUPHORIA_UP"} and state == "STRONG_CONFLICT":
        multiplier = min(multiplier, 0.45)
    return {"state": state, "signed_alignment_score": round(signed, 1), "risk_multiplier": multiplier}


async def persist_market_breadth_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.reason
        FROM signals s JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.created_at ASC
    """), {"run_id": run_id})).mappings().all()

    frame_values = {"5m": [], "15m": [], "1h": []}
    for raw in rows:
        reason = _d(raw.get("reason"))
        metrics = _d(reason.get("metrics"))
        for label, key in (("5m", "change_5m_pct"), ("15m", "change_15m_pct"), ("1h", "change_1h_pct")):
            value = metrics.get(key)
            if value is not None:
                frame_values[label].append(_f(value))

    frames = {label: _short_frame(values) for label, values in frame_values.items()}

    universe: list[dict[str, Any]] = []
    try:
        tickers = await binance_client.ticker_24h()
        if isinstance(tickers, list):
            universe = [
                t for t in tickers
                if str(t.get("symbol") or "").endswith("USDT")
                and "_" not in str(t.get("symbol") or "")
                and _f(t.get("quoteVolume")) >= settings.scanner_min_quote_volume_usdt
            ]
    except Exception:
        universe = []

    changes24 = [_f(t.get("priceChangePercent")) for t in universe]
    advances24 = sum(1 for v in changes24 if v > 0.10)
    declines24 = sum(1 for v in changes24 if v < -0.10)
    total24 = len(changes24)
    median24 = median(changes24) if changes24 else 0.0
    total_volume = sum(max(0.0, _f(t.get("quoteVolume"))) for t in universe)
    signed_volume = sum(
        max(0.0, _f(t.get("quoteVolume"))) * (1.0 if _f(t.get("priceChangePercent")) > 0 else -1.0 if _f(t.get("priceChangePercent")) < 0 else 0.0)
        for t in universe
    )
    volume_score = signed_volume / total_volume * 100.0 if total_volume > 0 else 0.0
    breadth24_score = (advances24 - declines24) / total24 * 100.0 if total24 else 0.0

    short_scores = [frames[h]["score"] for h in ("5m", "15m", "1h") if frames[h].get("available")]
    short_score = sum(short_scores) / len(short_scores) if short_scores else 0.0
    combined = short_score * 0.55 + breadth24_score * 0.25 + volume_score * 0.20
    advance24_pct = _pct(advances24, total24)
    decline24_pct = _pct(declines24, total24)
    regime = _regime(combined, decline24_pct, advance24_pct, median24)
    directional_bias = "SHORT" if combined <= -20 else "LONG" if combined >= 20 else "NEUTRAL"

    breadth = {
        "version": VERSION,
        "regime": regime,
        "directional_bias": directional_bias,
        "breadth_score": round(combined, 1),
        "short_term_score": round(short_score, 1),
        "volume_weighted_24h_score": round(volume_score, 1),
        "frames": frames,
        "universe_24h": {
            "sample": total24,
            "advance_pct": advance24_pct,
            "decline_pct": decline24_pct,
            "median_change_pct": round(median24, 4),
            "score": round(breadth24_score, 1),
        },
        "creates_entry": False,
        "changes_direction": False,
        "rule": "Breadth is market context inside the same Heart; strong conflict reduces risk but never creates a trade alone.",
    }

    updated = 0
    for raw in rows:
        reason = _d(raw.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart:
            continue
        contract = _d(heart.get("execution_contract"))
        direction = str(contract.get("primary_direction") or heart.get("direction") or raw.get("direction") or "").upper()
        alignment = _alignment(direction, combined, regime)
        local_breadth = {**breadth, "alignment_to_primary": alignment}
        contract["market_breadth"] = local_breadth
        lanes = _d(contract.get("lanes"))
        for lane in lanes.values():
            if not isinstance(lane, dict):
                continue
            lane_direction = str(lane.get("direction") or direction).upper()
            lane_alignment = _alignment(lane_direction, combined, regime)
            lane["market_breadth_regime"] = regime
            lane["market_breadth_score"] = round(combined, 1)
            lane["market_breadth_bias"] = directional_bias
            lane["breadth_alignment"] = lane_alignment["state"]
            lane["breadth_risk_multiplier"] = lane_alignment["risk_multiplier"]
        contract["lanes"] = lanes
        heart["market_breadth"] = local_breadth
        heart["execution_contract"] = contract
        reason["explodex_heart"] = heart
        if prediction:
            prediction["explodex_heart"] = heart
            reason["prediction"] = prediction
        await db.execute(text("UPDATE signals SET reason=CAST(:reason AS JSONB), updated_at=NOW() WHERE id=CAST(:id AS UUID)"), {
            "id": raw["signal_id"], "reason": json.dumps(reason)
        })
        updated += 1
    await db.commit()
    return {"version": VERSION, "updated": updated, "breadth": breadth}
