from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from math import sqrt
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client
from app.services.paper_portfolio import ensure_paper_schema
from app.services.scoring import build_btc_context, score_snapshot

VERSION = "paper_trade_auditor_v1"
MIN_CALIBRATION_SAMPLE = 30
OPEN_AUDIT_REFRESH_MINUTES = 5
CLOSED_BACKFILL_PER_CYCLE = 1


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _bucket(score: float) -> str:
    value = max(0.0, min(100.0, _f(score)))
    floor = int(value // 10) * 10
    ceiling = min(100, floor + 9)
    return f"{floor}-{ceiling}"


def _bars(rows: list[list[Any]]) -> list[dict[str, float]]:
    output: list[dict[str, float]] = []
    for row in rows:
        if len(row) < 5:
            continue
        output.append({
            "time": _f(row[0]),
            "open": _f(row[1]),
            "high": _f(row[2]),
            "low": _f(row[3]),
            "close": _f(row[4]),
            "volume": _f(row[7] if len(row) > 7 else row[5] if len(row) > 5 else 0),
        })
    return [bar for bar in output if min(bar["open"], bar["high"], bar["low"], bar["close"]) > 0]


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _ema(values: list[float], period: int) -> float:
    series = _ema_series(values, period)
    return series[-1] if series else 0.0


def _atr(bars: list[dict[str, float]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    previous = bars[0]["close"]
    for bar in bars[1:]:
        trs.append(max(
            bar["high"] - bar["low"],
            abs(bar["high"] - previous),
            abs(bar["low"] - previous),
        ))
        previous = bar["close"]
    sample = trs[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(closes[-(period + 1):-1], closes[-period:]):
        change = current - previous
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(closes: list[float]) -> tuple[float | None, float | None, float | None]:
    if len(closes) < 35:
        return None, None, None
    fast = _ema_series(closes, 12)
    slow = _ema_series(closes, 26)
    offset = len(fast) - len(slow)
    macd_series = [fast[i + offset] - slow[i] for i in range(len(slow))]
    signal_series = _ema_series(macd_series, 9)
    if not macd_series or not signal_series:
        return None, None, None
    macd_value = macd_series[-1]
    signal_value = signal_series[-1]
    return macd_value, signal_value, macd_value - signal_value


def technical_snapshot(klines_5m: list[list[Any]]) -> dict[str, Any]:
    bars = _bars(klines_5m)
    closes = [bar["close"] for bar in bars]
    macd_value, signal_value, histogram = _macd(closes)
    atr = _atr(bars, 14)
    current = closes[-1] if closes else 0.0
    return {
        "current_price": current,
        "ema20": round(_ema(closes, 20), 12) if closes else None,
        "ema50": round(_ema(closes, 50), 12) if closes else None,
        "rsi14": round(_rsi(closes, 14), 2) if _rsi(closes, 14) is not None else None,
        "macd": round(macd_value, 12) if macd_value is not None else None,
        "macd_signal": round(signal_value, 12) if signal_value is not None else None,
        "macd_histogram": round(histogram, 12) if histogram is not None else None,
        "atr": round(atr, 12),
        "atr_pct": round((atr / current * 100.0), 4) if current > 0 else None,
    }


def _path_metrics(
    *,
    side: str,
    entry: float,
    stop: float,
    tp1: float,
    candles: list[dict[str, float]],
) -> dict[str, Any]:
    side = str(side or "").upper()
    risk = abs(entry - stop)
    if side not in {"LONG", "SHORT"} or min(entry, stop, tp1) <= 0 or risk <= 1e-12 or not candles:
        return {
            "available": False,
            "one_r_before_stop": None,
            "tp1_before_stop": None,
            "stop_before_tp1": None,
        }

    one_r = entry + risk if side == "LONG" else entry - risk
    two_r = entry + 2.0 * risk if side == "LONG" else entry - 2.0 * risk
    three_r = entry + 3.0 * risk if side == "LONG" else entry - 3.0 * risk
    max_favorable = 0.0
    max_adverse = 0.0
    first_stop: int | None = None
    first_one_r: int | None = None
    first_tp1: int | None = None
    first_two_r: int | None = None
    first_three_r: int | None = None

    for index, candle in enumerate(candles):
        high, low = candle["high"], candle["low"]
        favorable = high - entry if side == "LONG" else entry - low
        adverse = entry - low if side == "LONG" else high - entry
        max_favorable = max(max_favorable, favorable)
        max_adverse = max(max_adverse, adverse)

        stop_hit = low <= stop if side == "LONG" else high >= stop
        one_r_hit = high >= one_r if side == "LONG" else low <= one_r
        tp1_hit = high >= tp1 if side == "LONG" else low <= tp1
        two_r_hit = high >= two_r if side == "LONG" else low <= two_r
        three_r_hit = high >= three_r if side == "LONG" else low <= three_r

        # Conservative ordering for ambiguous same-candle stop/target events.
        if stop_hit and first_stop is None:
            first_stop = index
        if one_r_hit and first_one_r is None:
            first_one_r = index
        if tp1_hit and first_tp1 is None:
            first_tp1 = index
        if two_r_hit and first_two_r is None:
            first_two_r = index
        if three_r_hit and first_three_r is None:
            first_three_r = index

    one_r_before_stop = first_one_r is not None and (first_stop is None or first_one_r < first_stop)
    tp1_before_stop = first_tp1 is not None and (first_stop is None or first_tp1 < first_stop)
    stop_before_tp1 = first_stop is not None and (first_tp1 is None or first_stop <= first_tp1)

    runner_2r_after_tp1 = bool(first_tp1 is not None and first_two_r is not None and first_two_r >= first_tp1)
    runner_3r_after_tp1 = bool(first_tp1 is not None and first_three_r is not None and first_three_r >= first_tp1)

    reversed_to_entry_after_tp1 = False
    if first_tp1 is not None:
        for candle in candles[first_tp1 + 1:]:
            if side == "LONG" and candle["low"] <= entry:
                reversed_to_entry_after_tp1 = True
                break
            if side == "SHORT" and candle["high"] >= entry:
                reversed_to_entry_after_tp1 = True
                break

    return {
        "available": True,
        "risk_per_unit": risk,
        "one_r_price": one_r,
        "two_r_price": two_r,
        "three_r_price": three_r,
        "max_favorable_r": round(max_favorable / risk, 4),
        "max_adverse_r": round(max_adverse / risk, 4),
        "one_r_before_stop": one_r_before_stop,
        "tp1_before_stop": tp1_before_stop,
        "stop_before_tp1": stop_before_tp1,
        "tp1_touched": first_tp1 is not None,
        "stop_touched": first_stop is not None,
        "runner_2r_after_tp1": runner_2r_after_tp1,
        "runner_3r_after_tp1": runner_3r_after_tp1,
        "reversed_to_entry_after_tp1": reversed_to_entry_after_tp1,
        "first_stop_index": first_stop,
        "first_one_r_index": first_one_r,
        "first_tp1_index": first_tp1,
    }


def _entry_structure_audit(
    *,
    side: str,
    entry: float,
    actual_stop: float,
    klines_15m: list[list[Any]],
    opened_at: datetime,
) -> dict[str, Any]:
    bars = _bars(klines_15m)
    opened_ms = opened_at.timestamp() * 1000.0
    before = [bar for bar in bars if bar["time"] < opened_ms]
    if len(before) < 10 or min(entry, actual_stop) <= 0:
        return {
            "status": "UNKNOWN",
            "reason": "insufficient_pre_entry_structure",
            "can_widen_live_stop": False,
        }

    atr = _atr(before, 14)
    recent = before[-8:]
    if atr <= 0:
        return {
            "status": "UNKNOWN",
            "reason": "atr_unavailable_at_entry",
            "can_widen_live_stop": False,
        }

    side = str(side or "").upper()
    buffer = max(atr * 0.45, entry * 0.0015)
    if side == "LONG":
        structural_level = min(bar["low"] for bar in recent)
        ideal_hard_stop = structural_level - buffer
        if actual_stop >= structural_level - atr * 0.10:
            status = "TOO_TIGHT"
            reason = "stop_inside_or_too_close_to_pre_entry_swing"
        elif actual_stop < ideal_hard_stop - atr:
            status = "TOO_WIDE"
            reason = "stop_more_than_one_atr_beyond_structural_buffer"
        else:
            status = "LOGICAL"
            reason = "stop_below_structure_with_volatility_buffer"
    elif side == "SHORT":
        structural_level = max(bar["high"] for bar in recent)
        ideal_hard_stop = structural_level + buffer
        if actual_stop <= structural_level + atr * 0.10:
            status = "TOO_TIGHT"
            reason = "stop_inside_or_too_close_to_pre_entry_swing"
        elif actual_stop > ideal_hard_stop + atr:
            status = "TOO_WIDE"
            reason = "stop_more_than_one_atr_beyond_structural_buffer"
        else:
            status = "LOGICAL"
            reason = "stop_above_structure_with_volatility_buffer"
    else:
        return {"status": "UNKNOWN", "reason": "invalid_side", "can_widen_live_stop": False}

    return {
        "status": status,
        "reason": reason,
        "entry_atr": round(atr, 12),
        "entry_atr_pct": round(atr / entry * 100.0, 4),
        "stop_distance_atr": round(abs(entry - actual_stop) / atr, 3),
        "structural_level": round(structural_level, 12),
        "ideal_hard_stop_for_future_similar_setups": round(ideal_hard_stop, 12),
        "actual_stop": actual_stop,
        "can_widen_live_stop": False,
        "rule": "If the original stop was too tight, learn for FUTURE setups; never widen a losing live stop.",
    }


def _alignment(
    *,
    side: str,
    technical: dict[str, Any],
    flow: dict[str, Any],
) -> dict[str, Any]:
    side = str(side or "").upper()
    ema20 = _f(technical.get("ema20"))
    ema50 = _f(technical.get("ema50"))
    rsi = technical.get("rsi14")
    macd_hist = technical.get("macd_histogram")
    futures_delta = _f(flow.get("futures_delta_ratio"))
    spot_delta = _f(flow.get("spot_delta_ratio"))
    orderbook = _f(flow.get("order_book_imbalance"))
    oi_change = _f(flow.get("oi_change_pct"))
    funding = _f(flow.get("funding_rate"))

    positives: list[str] = []
    conflicts: list[str] = []

    if side == "LONG":
        (positives if ema20 > ema50 else conflicts).append("EMA20>EMA50" if ema20 > ema50 else "EMA20<=EMA50")
        (positives if macd_hist is not None and _f(macd_hist) > 0 else conflicts).append("MACD+" if macd_hist is not None and _f(macd_hist) > 0 else "MACD-")
        if rsi is not None:
            if 48 <= _f(rsi) <= 72:
                positives.append("RSI_sano")
            elif _f(rsi) < 42:
                conflicts.append("RSI_debil")
            elif _f(rsi) > 78:
                conflicts.append("RSI_sobreextendido")
        (positives if futures_delta >= 0 else conflicts).append("delta_futuros+" if futures_delta >= 0 else "delta_futuros-")
        (positives if spot_delta >= 0 else conflicts).append("delta_spot+" if spot_delta >= 0 else "delta_spot-")
        (positives if orderbook >= 0 else conflicts).append("book_bid" if orderbook >= 0 else "book_ask")
    elif side == "SHORT":
        (positives if ema20 < ema50 else conflicts).append("EMA20<EMA50" if ema20 < ema50 else "EMA20>=EMA50")
        (positives if macd_hist is not None and _f(macd_hist) < 0 else conflicts).append("MACD-" if macd_hist is not None and _f(macd_hist) < 0 else "MACD+")
        if rsi is not None:
            if 28 <= _f(rsi) <= 52:
                positives.append("RSI_sano")
            elif _f(rsi) > 58:
                conflicts.append("RSI_fuerte_contra_short")
            elif _f(rsi) < 22:
                conflicts.append("RSI_sobreextendido")
        (positives if futures_delta <= 0 else conflicts).append("delta_futuros-" if futures_delta <= 0 else "delta_futuros+")
        (positives if spot_delta <= 0 else conflicts).append("delta_spot-" if spot_delta <= 0 else "delta_spot+")
        (positives if orderbook <= 0 else conflicts).append("book_ask" if orderbook <= 0 else "book_bid")

    if oi_change >= 0:
        positives.append("OI_sostiene")
    elif oi_change <= -0.75:
        conflicts.append("OI_cae")

    if abs(funding) > 0.0008:
        conflicts.append("funding_extremo")

    score = len(positives) - len(conflicts)
    if score >= 5:
        state = "STRONG"
    elif score >= 2:
        state = "OK"
    elif score >= 0:
        state = "MIXED"
    else:
        state = "WEAK"

    return {
        "state": state,
        "score": score,
        "positives": positives,
        "conflicts": conflicts,
    }


def _management_recommendation(
    *,
    side: str,
    entry: float,
    current_price: float,
    stop: float,
    tp1: float,
    path: dict[str, Any],
    alignment: dict[str, Any],
    current_structure_stop: float | None,
) -> dict[str, Any]:
    risk = abs(entry - stop)
    progress_r = 0.0
    if risk > 0 and current_price > 0:
        progress_r = ((current_price - entry) / risk) if side == "LONG" else ((entry - current_price) / risk)

    tp1_reached = bool(path.get("tp1_touched"))
    one_r_reached = bool(path.get("first_one_r_index") is not None)
    alignment_state = str(alignment.get("state") or "MIXED")
    candidate_stop = _f(current_structure_stop)
    be = entry

    if tp1_reached:
        if alignment_state == "STRONG":
            action = "TAKE_PARTIAL_AND_TRAIL_STRUCTURE"
            explanation = "TP1 ya fue tocado y momentum/flujo siguen alineados: auditar parcial en TP1 y dejar runner con stop estructural."
        elif alignment_state in {"WEAK", "MIXED"}:
            action = "CLOSE_MOST_OR_ALL_AT_TP1"
            explanation = "TP1 fue tocado pero la continuación perdió calidad: auditar si cerrar la mayor parte o todo en TP1 supera dejar runner."
        else:
            action = "TAKE_PARTIAL_PROTECT_REST"
            explanation = "TP1 fue tocado con contexto aceptable: asegurar una parte y proteger el resto sin ensanchar riesgo."
    elif one_r_reached:
        if alignment_state == "WEAK":
            action = "PROTECT_IF_NEW_STRUCTURE_ALLOWS"
            explanation = "Ya hubo +1R pero el contexto se debilitó. Solo acercar stop si una nueva estructura lo justifica; no usar break-even dentro del ruido."
        else:
            action = "HOLD_AND_DO_NOT_CHOKE"
            explanation = "Ya hubo +1R y la tesis sigue viva. No apretar el stop dentro del ATR; esperar estructura o TP1."
    elif progress_r >= 0.65 and alignment_state == "WEAK":
        action = "WATCH_FOR_STRUCTURE_PROTECTION"
        explanation = "Está cerca de +1R pero con deterioro. Preparar protección si aparece un nuevo swing válido; no mover stop por miedo."
    elif progress_r < 0 and alignment_state == "WEAK":
        action = "HOLD_OR_EXIT_ON_STRUCTURAL_INVALIDATION"
        explanation = "La operación va en contra y el contexto es débil. No ensanchar el stop; respetar la invalidación estructural original."
    else:
        action = "HOLD_ORIGINAL_PLAN"
        explanation = "No hay evidencia suficiente para modificar el manejo. Mantener el plan original y evitar microgestión."

    suggested_protective_stop = None
    if action in {"TAKE_PARTIAL_AND_TRAIL_STRUCTURE", "TAKE_PARTIAL_PROTECT_REST", "PROTECT_IF_NEW_STRUCTURE_ALLOWS", "WATCH_FOR_STRUCTURE_PROTECTION"}:
        if candidate_stop > 0:
            if side == "LONG" and candidate_stop > stop and candidate_stop < current_price:
                suggested_protective_stop = candidate_stop
            if side == "SHORT" and candidate_stop < stop and candidate_stop > current_price:
                suggested_protective_stop = candidate_stop

    return {
        "action": action,
        "explanation": explanation,
        "progress_r": round(progress_r, 3),
        "breakeven_price": be,
        "suggested_protective_stop": suggested_protective_stop,
        "may_reduce_risk": True,
        "may_increase_risk": False,
        "may_widen_live_stop": False,
        "note": "Audit recommendation only. It does not mutate the live PAPER stop in v1.",
    }


async def ensure_paper_trade_audit_schema(db: AsyncSession) -> None:
    await ensure_paper_schema(db)
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_trade_audits (
            id BIGSERIAL PRIMARY KEY,
            position_id BIGINT NOT NULL REFERENCES paper_positions(id) ON DELETE CASCADE,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            stage VARCHAR(16) NOT NULL,
            strategy_mode VARCHAR(40) NOT NULL,
            side VARCHAR(8) NOT NULL,
            score_bucket VARCHAR(16),
            current_price NUMERIC(30,12),
            progress_r NUMERIC(12,6),
            max_favorable_r NUMERIC(12,6),
            max_adverse_r NUMERIC(12,6),
            one_r_before_stop BOOLEAN,
            tp1_before_stop BOOLEAN,
            stop_before_tp1 BOOLEAN,
            runner_2r_after_tp1 BOOLEAN,
            runner_3r_after_tp1 BOOLEAN,
            reversed_to_entry_after_tp1 BOOLEAN,
            stop_quality VARCHAR(24),
            recommendation VARCHAR(64),
            technical JSONB NOT NULL DEFAULT '{}'::jsonb,
            flow JSONB NOT NULL DEFAULT '{}'::jsonb,
            stop_analysis JSONB NOT NULL DEFAULT '{}'::jsonb,
            management JSONB NOT NULL DEFAULT '{}'::jsonb,
            calibration JSONB NOT NULL DEFAULT '{}'::jsonb,
            narrative TEXT,
            UNIQUE(position_id, stage)
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_paper_trade_audits_stage_strategy "
        "ON paper_trade_audits(stage, strategy_mode, side, observed_at DESC)"
    ))
    await db.commit()


async def _historical_calibration(
    db: AsyncSession,
    *,
    strategy_mode: str,
    side: str,
    score_bucket: str,
) -> dict[str, Any]:
    exact = dict((await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE tp1_before_stop IS NOT NULL) AS sample,
            COUNT(*) FILTER (WHERE tp1_before_stop=TRUE) AS tp1_first,
            COUNT(*) FILTER (WHERE one_r_before_stop=TRUE) AS one_r_first,
            COUNT(*) FILTER (WHERE tp1_before_stop=TRUE AND runner_2r_after_tp1=TRUE) AS runner_2r,
            COUNT(*) FILTER (WHERE tp1_before_stop=TRUE AND runner_3r_after_tp1=TRUE) AS runner_3r,
            COUNT(*) FILTER (WHERE tp1_before_stop=TRUE) AS tp1_cases
        FROM paper_trade_audits
        WHERE stage='CLOSED' AND strategy_mode=:strategy AND side=:side AND score_bucket=:bucket
    """), {"strategy": strategy_mode, "side": side, "bucket": score_bucket})).mappings().one())

    broad = dict((await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE tp1_before_stop IS NOT NULL) AS sample,
            COUNT(*) FILTER (WHERE tp1_before_stop=TRUE) AS tp1_first,
            COUNT(*) FILTER (WHERE one_r_before_stop=TRUE) AS one_r_first,
            COUNT(*) FILTER (WHERE tp1_before_stop=TRUE AND runner_2r_after_tp1=TRUE) AS runner_2r,
            COUNT(*) FILTER (WHERE tp1_before_stop=TRUE AND runner_3r_after_tp1=TRUE) AS runner_3r,
            COUNT(*) FILTER (WHERE tp1_before_stop=TRUE) AS tp1_cases
        FROM paper_trade_audits
        WHERE stage='CLOSED' AND strategy_mode=:strategy AND side=:side
    """), {"strategy": strategy_mode, "side": side})).mappings().one())

    chosen = exact if int(exact.get("sample") or 0) >= MIN_CALIBRATION_SAMPLE else broad
    cohort = "strategy_side_score_bucket" if chosen is exact else "strategy_side"
    sample = int(chosen.get("sample") or 0)
    tp1 = int(chosen.get("tp1_first") or 0)
    one_r = int(chosen.get("one_r_first") or 0)
    tp1_cases = int(chosen.get("tp1_cases") or 0)
    runner_2r = int(chosen.get("runner_2r") or 0)
    runner_3r = int(chosen.get("runner_3r") or 0)

    status = "MATURE" if sample >= MIN_CALIBRATION_SAMPLE else "CALIBRATING"
    return {
        "status": status,
        "cohort": cohort,
        "sample": sample,
        "minimum_sample": MIN_CALIBRATION_SAMPLE,
        "tp1_before_sl_observed_pct": round(tp1 / sample * 100.0, 2) if sample else None,
        "one_r_before_sl_observed_pct": round(one_r / sample * 100.0, 2) if sample else None,
        "runner_2r_after_tp1_observed_pct": round(runner_2r / tp1_cases * 100.0, 2) if tp1_cases else None,
        "runner_3r_after_tp1_observed_pct": round(runner_3r / tp1_cases * 100.0, 2) if tp1_cases else None,
        "tp1_cases": tp1_cases,
        "is_probability_forecast": False,
        "label": "frecuencia_historica_paper",
        "note": (
            "Frecuencia observada en casos PAPER comparables; no es una probabilidad garantizada del siguiente trade."
            if status == "MATURE"
            else "CALIBRANDO: se muestran conteos, pero no se trata la tasa como estimación estable hasta reunir 30 casos comparables."
        ),
    }


def _current_structure_stop(side: str, entry: float, klines_15m: list[list[Any]]) -> float | None:
    bars = _bars(klines_15m)
    if len(bars) < 10 or entry <= 0:
        return None
    completed = bars[:-1] if len(bars) > 10 else bars
    recent = completed[-6:]
    atr = _atr(completed, 14)
    if atr <= 0:
        return None
    buffer = max(atr * 0.30, entry * 0.0010)
    if side == "LONG":
        return min(bar["low"] for bar in recent) - buffer
    if side == "SHORT":
        return max(bar["high"] for bar in recent) + buffer
    return None


def _flow_from_score(score: dict[str, Any]) -> dict[str, Any]:
    metrics = _d(score.get("metrics"))
    return {
        "oi_change_pct": metrics.get("oi_change_pct"),
        "taker_ratio": metrics.get("taker_avg_3"),
        "funding_rate": metrics.get("funding_rate"),
        "futures_delta_ratio": metrics.get("futures_delta_ratio"),
        "spot_delta_ratio": metrics.get("spot_delta_ratio"),
        "order_book_imbalance": metrics.get("order_book_imbalance"),
        "relative_volume": metrics.get("relative_volume"),
        "volume_acceleration": metrics.get("volume_acceleration"),
        "trend_15m": metrics.get("trend_15m"),
        "trend_1h": metrics.get("trend_1h"),
        "btc_trend": metrics.get("btc_trend"),
        "confirmations": metrics.get("confirmations"),
        "scanner_risk_score": score.get("risk_score"),
        "scanner_direction": score.get("direction"),
    }


async def _upsert_audit(
    db: AsyncSession,
    *,
    position: dict[str, Any],
    stage: str,
    technical: dict[str, Any],
    flow: dict[str, Any],
    stop_analysis: dict[str, Any],
    path: dict[str, Any],
    management: dict[str, Any],
    calibration: dict[str, Any],
    current_price: float,
    narrative: str,
) -> None:
    metadata = _d(position.get("metadata"))
    strategy = str(metadata.get("strategy_mode") or "TREND_PREMOVE").upper()
    score = _f(metadata.get("pattern_score"), _f(position.get("fingerprint_score")))
    await db.execute(text("""
        INSERT INTO paper_trade_audits (
            position_id, stage, strategy_mode, side, score_bucket,
            current_price, progress_r, max_favorable_r, max_adverse_r,
            one_r_before_stop, tp1_before_stop, stop_before_tp1,
            runner_2r_after_tp1, runner_3r_after_tp1, reversed_to_entry_after_tp1,
            stop_quality, recommendation, technical, flow, stop_analysis,
            management, calibration, narrative
        ) VALUES (
            :position_id, :stage, :strategy_mode, :side, :score_bucket,
            :current_price, :progress_r, :max_favorable_r, :max_adverse_r,
            :one_r_before_stop, :tp1_before_stop, :stop_before_tp1,
            :runner_2r_after_tp1, :runner_3r_after_tp1, :reversed_to_entry_after_tp1,
            :stop_quality, :recommendation, CAST(:technical AS JSONB), CAST(:flow AS JSONB),
            CAST(:stop_analysis AS JSONB), CAST(:management AS JSONB),
            CAST(:calibration AS JSONB), :narrative
        )
        ON CONFLICT (position_id, stage) DO UPDATE SET
            observed_at=NOW(),
            current_price=EXCLUDED.current_price,
            progress_r=EXCLUDED.progress_r,
            max_favorable_r=EXCLUDED.max_favorable_r,
            max_adverse_r=EXCLUDED.max_adverse_r,
            one_r_before_stop=EXCLUDED.one_r_before_stop,
            tp1_before_stop=EXCLUDED.tp1_before_stop,
            stop_before_tp1=EXCLUDED.stop_before_tp1,
            runner_2r_after_tp1=EXCLUDED.runner_2r_after_tp1,
            runner_3r_after_tp1=EXCLUDED.runner_3r_after_tp1,
            reversed_to_entry_after_tp1=EXCLUDED.reversed_to_entry_after_tp1,
            stop_quality=EXCLUDED.stop_quality,
            recommendation=EXCLUDED.recommendation,
            technical=EXCLUDED.technical,
            flow=EXCLUDED.flow,
            stop_analysis=EXCLUDED.stop_analysis,
            management=EXCLUDED.management,
            calibration=EXCLUDED.calibration,
            narrative=EXCLUDED.narrative
    """), {
        "position_id": position["id"],
        "stage": stage,
        "strategy_mode": strategy,
        "side": str(position.get("side") or "").upper(),
        "score_bucket": _bucket(score),
        "current_price": current_price,
        "progress_r": management.get("progress_r"),
        "max_favorable_r": path.get("max_favorable_r"),
        "max_adverse_r": path.get("max_adverse_r"),
        "one_r_before_stop": path.get("one_r_before_stop"),
        "tp1_before_stop": path.get("tp1_before_stop"),
        "stop_before_tp1": path.get("stop_before_tp1"),
        "runner_2r_after_tp1": path.get("runner_2r_after_tp1"),
        "runner_3r_after_tp1": path.get("runner_3r_after_tp1"),
        "reversed_to_entry_after_tp1": path.get("reversed_to_entry_after_tp1"),
        "stop_quality": stop_analysis.get("status"),
        "recommendation": management.get("action"),
        "technical": json.dumps(technical),
        "flow": json.dumps(flow),
        "stop_analysis": json.dumps(stop_analysis),
        "management": json.dumps(management),
        "calibration": json.dumps(calibration),
        "narrative": narrative,
    })
    await db.commit()


async def _audit_open_position(
    db: AsyncSession,
    position: dict[str, Any],
    *,
    btc_context: dict[str, Any],
) -> dict[str, Any]:
    symbol = str(position.get("symbol") or "")
    side = str(position.get("side") or "").upper()
    entry = _f(position.get("entry_price"))
    stop = _f(position.get("stop_loss"))
    tp1 = _f(position.get("take_profit"))
    opened_at = position.get("opened_at")
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)

    snapshot = await binance_client.deep_snapshot(symbol)
    score = score_snapshot(snapshot, btc_context=btc_context)
    technical = technical_snapshot(snapshot.get("klines") or [])
    flow = _flow_from_score(score)
    current_price = _f(technical.get("current_price"), entry)

    bars5 = _bars(snapshot.get("klines") or [])
    opened_ms = opened_at.timestamp() * 1000.0
    since_entry = [bar for bar in bars5 if bar["time"] >= opened_ms]
    path = _path_metrics(side=side, entry=entry, stop=stop, tp1=tp1, candles=since_entry)

    klines15 = snapshot.get("klines_15m") or []
    stop_analysis = _entry_structure_audit(
        side=side,
        entry=entry,
        actual_stop=stop,
        klines_15m=klines15,
        opened_at=opened_at,
    )
    current_structure = _current_structure_stop(side, entry, klines15)
    alignment = _alignment(side=side, technical=technical, flow=flow)
    management = _management_recommendation(
        side=side,
        entry=entry,
        current_price=current_price,
        stop=stop,
        tp1=tp1,
        path=path,
        alignment=alignment,
        current_structure_stop=current_structure,
    )
    management["alignment"] = alignment
    management["current_structure_stop"] = current_structure

    metadata = _d(position.get("metadata"))
    calibration = await _historical_calibration(
        db,
        strategy_mode=str(metadata.get("strategy_mode") or "TREND_PREMOVE").upper(),
        side=side,
        score_bucket=_bucket(_f(metadata.get("pattern_score"), _f(position.get("fingerprint_score")))),
    )

    narrative = (
        f"SL: {stop_analysis.get('status','UNKNOWN')}. "
        f"Ahora: {management.get('action')}. "
        f"Contexto: {alignment.get('state')}. "
        f"TP1 antes de SL histórico: "
        f"{calibration.get('tp1_before_sl_observed_pct') if calibration.get('tp1_before_sl_observed_pct') is not None else 'sin muestra'}."
    )

    await _upsert_audit(
        db,
        position=position,
        stage="OPEN",
        technical=technical,
        flow=flow,
        stop_analysis=stop_analysis,
        path=path,
        management=management,
        calibration=calibration,
        current_price=current_price,
        narrative=narrative,
    )
    return {
        "position_id": position["id"],
        "symbol": symbol,
        "side": side,
        "stop_quality": stop_analysis.get("status"),
        "recommendation": management.get("action"),
        "progress_r": management.get("progress_r"),
        "calibration_status": calibration.get("status"),
    }


async def _audit_closed_position(db: AsyncSession, position: dict[str, Any]) -> dict[str, Any]:
    symbol = str(position.get("symbol") or "")
    side = str(position.get("side") or "").upper()
    entry = _f(position.get("entry_price"))
    stop = _f(position.get("stop_loss"))
    tp1 = _f(position.get("take_profit"))
    opened_at = position.get("opened_at")
    closed_at = position.get("closed_at")
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    if closed_at and closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)

    klines5 = await binance_client.klines(symbol, "5m", 1000)
    klines15 = await binance_client.klines(symbol, "15m", 500)
    bars5 = _bars(klines5)
    opened_ms = opened_at.timestamp() * 1000.0
    metadata = _d(position.get("metadata"))
    max_hold = int(metadata.get("max_hold_minutes") or 120)
    audit_end = opened_at + timedelta(minutes=max(30, min(max_hold, 4320)))
    end_ms = audit_end.timestamp() * 1000.0

    earliest_ms = bars5[0]["time"] if bars5 else 0.0
    path_available = bool(bars5 and earliest_ms <= opened_ms)
    horizon = [bar for bar in bars5 if opened_ms <= bar["time"] <= end_ms] if path_available else []
    path = _path_metrics(side=side, entry=entry, stop=stop, tp1=tp1, candles=horizon)

    stop_analysis = _entry_structure_audit(
        side=side,
        entry=entry,
        actual_stop=stop,
        klines_15m=klines15,
        opened_at=opened_at,
    )

    technical = {
        "path_data_available": path_available,
        "opened_at": opened_at.isoformat(),
        "closed_at": closed_at.isoformat() if closed_at else None,
        "exit_reason": position.get("exit_reason"),
        "exit_price": _f(position.get("exit_price")),
        "net_pnl": _f(position.get("net_pnl")),
    }
    flow: dict[str, Any] = {}
    management = {
        "action": "POST_TRADE_AUDIT",
        "progress_r": None,
        "may_widen_live_stop": False,
        "counterfactual": {
            "tp1_before_original_stop": path.get("tp1_before_stop"),
            "one_r_before_original_stop": path.get("one_r_before_stop"),
            "runner_reached_2r_after_tp1": path.get("runner_2r_after_tp1"),
            "runner_reached_3r_after_tp1": path.get("runner_3r_after_tp1"),
            "runner_reversed_to_entry_after_tp1": path.get("reversed_to_entry_after_tp1"),
        },
    }
    calibration = await _historical_calibration(
        db,
        strategy_mode=str(metadata.get("strategy_mode") or "TREND_PREMOVE").upper(),
        side=side,
        score_bucket=_bucket(_f(metadata.get("pattern_score"), _f(position.get("fingerprint_score")))),
    )

    if not path_available:
        narrative = "Auditoría cerrada con datos de trayectoria limitados; no se usa este caso para inferir TP1/1R vs SL."
    elif stop_analysis.get("status") == "TOO_TIGHT" and path.get("stop_before_tp1") and path.get("max_favorable_r", 0) > 1.0:
        narrative = "El SL original quedó demasiado cerca de la estructura y el precio mostró recuperación favorable después. Aprender para futuros setups; no ensanchar stops vivos."
    elif path.get("tp1_before_stop"):
        if path.get("runner_2r_after_tp1"):
            narrative = "TP1 llegó antes del SL y el runner alcanzó al menos 2R: este caso favorece estudiar parcial + trailing estructural."
        elif path.get("reversed_to_entry_after_tp1"):
            narrative = "TP1 llegó antes del SL pero luego volvió a entrada: este caso favorece estudiar cierre mayor en TP1."
        else:
            narrative = "TP1 llegó antes del SL; continuación posterior no fue concluyente."
    else:
        narrative = "El SL llegó antes de TP1 o no hubo recorrido suficiente; revisar estructura, ATR y calidad del setup."

    await _upsert_audit(
        db,
        position=position,
        stage="CLOSED",
        technical=technical,
        flow=flow,
        stop_analysis=stop_analysis,
        path=path,
        management=management,
        calibration=calibration,
        current_price=_f(position.get("exit_price"), entry),
        narrative=narrative,
    )
    return {
        "position_id": position["id"],
        "symbol": symbol,
        "path_available": path_available,
        "stop_quality": stop_analysis.get("status"),
        "tp1_before_stop": path.get("tp1_before_stop"),
        "one_r_before_stop": path.get("one_r_before_stop"),
    }


async def run_paper_trade_audits(db: AsyncSession) -> dict[str, Any]:
    await ensure_paper_trade_audit_schema(db)

    closed_rows = [dict(row) for row in (await db.execute(text("""
        SELECT pp.*
        FROM paper_positions pp
        WHERE pp.status='CLOSED'
          AND NOT EXISTS (
              SELECT 1 FROM paper_trade_audits a
              WHERE a.position_id=pp.id AND a.stage='CLOSED'
          )
        ORDER BY pp.closed_at DESC
        LIMIT :limit
    """), {"limit": CLOSED_BACKFILL_PER_CYCLE})).mappings().all()]

    closed_audited: list[dict[str, Any]] = []
    for position in closed_rows:
        try:
            closed_audited.append(await _audit_closed_position(db, position))
        except Exception as exc:
            await db.rollback()
            closed_audited.append({
                "position_id": position.get("id"),
                "symbol": position.get("symbol"),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            })

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=OPEN_AUDIT_REFRESH_MINUTES)
    open_rows = [dict(row) for row in (await db.execute(text("""
        SELECT pp.*
        FROM paper_positions pp
        LEFT JOIN paper_trade_audits a
          ON a.position_id=pp.id AND a.stage='OPEN'
        WHERE pp.status='OPEN'
          AND (a.observed_at IS NULL OR a.observed_at < :cutoff)
        ORDER BY pp.opened_at ASC
    """), {"cutoff": cutoff})).mappings().all()]

    btc_context: dict[str, Any] = {"trend": "NEUTRAL", "change_15m_pct": 0.0, "change_1h_pct": 0.0}
    if open_rows:
        try:
            btc_context = build_btc_context(await binance_client.klines("BTCUSDT", "5m", 60))
        except Exception:
            pass

    open_audited: list[dict[str, Any]] = []
    for position in open_rows[:3]:
        try:
            open_audited.append(await _audit_open_position(db, position, btc_context=btc_context))
        except Exception as exc:
            await db.rollback()
            open_audited.append({
                "position_id": position.get("id"),
                "symbol": position.get("symbol"),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            })

    return {
        "version": VERSION,
        "paper_only": True,
        "closed_backfilled": len(closed_audited),
        "open_refreshed": len(open_audited),
        "closed": closed_audited,
        "open": open_audited,
        "policy": {
            "never_widen_live_stop": True,
            "audit_can_reduce_risk": True,
            "audit_can_increase_risk": False,
            "minimum_calibration_sample": MIN_CALIBRATION_SAMPLE,
            "historical_rates_are_not_next_trade_probabilities": True,
        },
    }


async def paper_trade_audit_report(db: AsyncSession, *, closed_limit: int = 20) -> dict[str, Any]:
    await ensure_paper_trade_audit_schema(db)
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT a.*, pp.symbol, pp.entry_price, pp.stop_loss, pp.take_profit,
               pp.exit_price, pp.exit_reason, pp.net_pnl, pp.opened_at, pp.closed_at
        FROM paper_trade_audits a
        JOIN paper_positions pp ON pp.id=a.position_id
        ORDER BY CASE WHEN a.stage='OPEN' THEN 0 ELSE 1 END, a.observed_at DESC
        LIMIT :limit
    """), {"limit": max(10, min(100, closed_limit + 10))})).mappings().all()]

    open_items: list[dict[str, Any]] = []
    closed_items: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "position_id": row.get("position_id"),
            "symbol": row.get("symbol"),
            "stage": row.get("stage"),
            "strategy_mode": row.get("strategy_mode"),
            "side": row.get("side"),
            "score_bucket": row.get("score_bucket"),
            "observed_at": row.get("observed_at").isoformat() if hasattr(row.get("observed_at"), "isoformat") else None,
            "entry_price": _f(row.get("entry_price")),
            "current_price": _f(row.get("current_price")),
            "stop_loss": _f(row.get("stop_loss")),
            "tp1": _f(row.get("take_profit")),
            "exit_price": _f(row.get("exit_price")) if row.get("exit_price") is not None else None,
            "exit_reason": row.get("exit_reason"),
            "net_pnl": _f(row.get("net_pnl")) if row.get("net_pnl") is not None else None,
            "progress_r": _f(row.get("progress_r")) if row.get("progress_r") is not None else None,
            "max_favorable_r": _f(row.get("max_favorable_r")) if row.get("max_favorable_r") is not None else None,
            "max_adverse_r": _f(row.get("max_adverse_r")) if row.get("max_adverse_r") is not None else None,
            "one_r_before_stop": row.get("one_r_before_stop"),
            "tp1_before_stop": row.get("tp1_before_stop"),
            "stop_before_tp1": row.get("stop_before_tp1"),
            "runner_2r_after_tp1": row.get("runner_2r_after_tp1"),
            "runner_3r_after_tp1": row.get("runner_3r_after_tp1"),
            "reversed_to_entry_after_tp1": row.get("reversed_to_entry_after_tp1"),
            "stop_quality": row.get("stop_quality"),
            "recommendation": row.get("recommendation"),
            "technical": _d(row.get("technical")),
            "flow": _d(row.get("flow")),
            "stop_analysis": _d(row.get("stop_analysis")),
            "management": _d(row.get("management")),
            "calibration": _d(row.get("calibration")),
            "narrative": row.get("narrative"),
        }
        if row.get("stage") == "OPEN":
            open_items.append(item)
        elif len(closed_items) < closed_limit:
            closed_items.append(item)

    aggregate = dict((await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE stage='CLOSED' AND tp1_before_stop IS NOT NULL) AS mature_cases,
            COUNT(*) FILTER (WHERE stage='CLOSED' AND tp1_before_stop=TRUE) AS tp1_first,
            COUNT(*) FILTER (WHERE stage='CLOSED' AND one_r_before_stop=TRUE) AS one_r_first,
            COUNT(*) FILTER (WHERE stage='CLOSED' AND stop_quality='TOO_TIGHT') AS tight_stops,
            COUNT(*) FILTER (WHERE stage='CLOSED' AND stop_quality='TOO_WIDE') AS wide_stops,
            COUNT(*) FILTER (WHERE stage='CLOSED' AND stop_quality='LOGICAL') AS logical_stops
        FROM paper_trade_audits
    """))).mappings().one())
    cases = int(aggregate.get("mature_cases") or 0)
    return {
        "version": VERSION,
        "paper_only": True,
        "open": open_items,
        "recent_closed": closed_items,
        "aggregate": {
            "audited_closed_cases": cases,
            "tp1_before_sl_observed_pct": round(int(aggregate.get("tp1_first") or 0) / cases * 100.0, 2) if cases else None,
            "one_r_before_sl_observed_pct": round(int(aggregate.get("one_r_first") or 0) / cases * 100.0, 2) if cases else None,
            "tight_stops": int(aggregate.get("tight_stops") or 0),
            "wide_stops": int(aggregate.get("wide_stops") or 0),
            "logical_stops": int(aggregate.get("logical_stops") or 0),
            "status": "MATURE" if cases >= MIN_CALIBRATION_SAMPLE else "CALIBRATING",
            "minimum_sample": MIN_CALIBRATION_SAMPLE,
        },
        "explanation": {
            "TP1_before_SL": "Frecuencia histórica PAPER de alcanzar TP1 antes del stop original.",
            "one_R_before_SL": "Frecuencia histórica PAPER de alcanzar +1R antes del stop original.",
            "stop_quality": "Compara el stop original con swing previo + ATR. Si fue estrecho, se aprende para futuros setups; nunca se ensancha un stop vivo.",
            "runner": "Después de TP1 compara si un runner habría alcanzado 2R/3R o habría regresado a entrada.",
        },
        "policy": {
            "audit_only_v1": True,
            "does_not_mutate_live_stop": True,
            "never_widen_live_stop": True,
            "historical_rates_are_not_next_trade_probabilities": True,
        },
    }
