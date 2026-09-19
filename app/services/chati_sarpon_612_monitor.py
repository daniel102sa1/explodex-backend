from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "chati_sarpon_612_monitor_v1"

# Exact discipline weights from the CHATI/SARPON/612 manual. They are a
# confluence rubric, not a calibrated next-trade probability.
WEIGHTS = {
    "btc_market": 0.20,
    "structure_accumulation": 0.20,
    "delta_5m_15m": 0.20,
    "open_interest": 0.15,
    "volume": 0.10,
    "funding_top_traders": 0.10,
    "extension_rr": 0.05,
}


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


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _ratio_score(side: str, ratio: float) -> float:
    ratio = max(0.01, ratio)
    directional = ratio if side == "LONG" else (1.0 / ratio)
    if directional >= 1.50:
        return 95.0
    if directional >= 1.15:
        return 78.0
    if directional >= 1.00:
        return 62.0
    if directional >= 0.85:
        return 48.0
    if directional >= 0.65:
        return 28.0
    return 8.0


def _trend_score(side: str, trend: str) -> float:
    trend = str(trend or "NEUTRAL").upper()
    aligned = (side == "LONG" and trend == "BULLISH") or (side == "SHORT" and trend == "BEARISH")
    opposed = (side == "LONG" and trend == "BEARISH") or (side == "SHORT" and trend == "BULLISH")
    if aligned:
        return 90.0
    if opposed:
        return 18.0
    return 58.0


def _delta_score(side: str, value: float) -> float:
    directional = value if side == "LONG" else -value
    # delta_ratio is normally -1..1. Saturate so one noisy print cannot dominate.
    return _clip(50.0 + directional * 190.0)


def _oi_score(oi_change_pct: float) -> float:
    if oi_change_pct >= 1.0:
        return 95.0
    if oi_change_pct >= 0.30:
        return 82.0
    if oi_change_pct >= 0.0:
        return 68.0
    if oi_change_pct >= -0.25:
        return 55.0
    if oi_change_pct >= -0.75:
        return 35.0
    return 12.0


def _volume_score(relative_volume: float, acceleration: float) -> float:
    score = 48.0
    if relative_volume >= 2.0:
        score += 30.0
    elif relative_volume >= 1.35:
        score += 20.0
    elif relative_volume >= 1.05:
        score += 8.0
    elif relative_volume < 0.70:
        score -= 12.0

    if acceleration >= 1.35:
        score += 18.0
    elif acceleration >= 1.10:
        score += 8.0
    elif acceleration < 0.80:
        score -= 10.0
    return _clip(score)


def _funding_top_score(side: str, metrics: dict[str, Any]) -> float:
    funding = _f(metrics.get("funding_rate"))
    top_accounts = _f(metrics.get("top_account_long_short_ratio"), 1.0)
    top_positions = _f(metrics.get("top_position_long_short_ratio"), 1.0)
    global_ls = _f(metrics.get("global_long_short_ratio"), 1.0)

    score = 65.0
    if side == "LONG":
        if funding > 0.0008:
            score -= 30.0
        elif funding > 0.0005:
            score -= 16.0
        elif funding < -0.0005:
            score += 8.0
        if top_accounts >= 1.05 and top_positions >= 1.15:
            score += 10.0
        if global_ls >= 2.2:
            score -= 16.0
    else:
        if funding < -0.0008:
            score -= 30.0
        elif funding < -0.0005:
            score -= 16.0
        elif funding > 0.0005:
            score += 8.0
        if top_accounts <= 0.95 and top_positions <= 0.87:
            score += 10.0
        if global_ls <= 0.45:
            score -= 16.0
    return _clip(score)


def _btc_score(side: str, metrics: dict[str, Any], btc_overlay: dict[str, Any]) -> tuple[float, bool, list[str]]:
    trend = str(btc_overlay.get("direction") or metrics.get("btc_trend") or "NEUTRAL").upper()
    stress = str(btc_overlay.get("stress") or "UNKNOWN").upper()
    aligned = (side == "LONG" and trend == "BULLISH") or (side == "SHORT" and trend == "BEARISH")
    opposed = (side == "LONG" and trend == "BEARISH") or (side == "SHORT" and trend == "BULLISH")

    notes: list[str] = []
    if aligned:
        score = 88.0
        notes.append("btc_direction_aligned")
    elif opposed:
        score = 25.0
        notes.append("btc_direction_conflict")
    else:
        score = 58.0

    if stress == "SHOCK":
        score -= 38.0
        notes.append("btc_shock")
    elif stress == "EXTREME":
        score -= 25.0
        notes.append("btc_extreme_volatility")
    elif stress == "HIGH":
        score -= 14.0
        notes.append("btc_high_volatility")
    elif stress == "ELEVATED":
        score -= 6.0

    hard_conflict = bool(opposed and stress in {"HIGH", "EXTREME", "SHOCK"})
    return _clip(score), hard_conflict, notes


def _extension_score(
    *,
    side: str,
    current_price: float,
    entry_low: float,
    entry_high: float,
    entry_price: float,
    stop: float,
    tp1: float,
    is_open_position: bool,
) -> tuple[float, float | None]:
    if current_price <= 0:
        return 45.0, None

    if is_open_position and entry_price > 0 and stop > 0:
        risk = abs(entry_price - stop)
        if risk <= 1e-12:
            return 45.0, None
        progress = (current_price - entry_price) / risk if side == "LONG" else (entry_price - current_price) / risk
        # Once in the trade, "extension" is not an entry chase penalty.
        return _clip(72.0 + max(-1.5, min(2.0, progress)) * 8.0), progress

    if entry_low > 0 and entry_high > 0:
        lo, hi = sorted((entry_low, entry_high))
        if lo <= current_price <= hi:
            score = 88.0
        else:
            width = max(hi - lo, current_price * 0.0005)
            distance = min(abs(current_price - lo), abs(current_price - hi))
            widths_away = distance / width
            score = _clip(80.0 - widths_away * 35.0)
    else:
        score = 55.0

    if entry_price > 0 and stop > 0 and tp1 > 0:
        risk = abs(entry_price - stop)
        reward = abs(tp1 - entry_price)
        rr = reward / risk if risk > 1e-12 else 0.0
        if rr < 1.2:
            score = min(score, 30.0)
        elif rr >= 2.0:
            score = min(100.0, score + 8.0)
    return score, None


def build_manual_monitor(
    *,
    side: str,
    metrics: dict[str, Any],
    current_price: float,
    entry_low: float = 0.0,
    entry_high: float = 0.0,
    entry_price: float = 0.0,
    stop: float = 0.0,
    tp1: float = 0.0,
    btc_overlay: dict[str, Any] | None = None,
    is_open_position: bool = False,
) -> dict[str, Any]:
    side = str(side or "").upper()
    metrics = _d(metrics)
    btc_overlay = _d(btc_overlay)
    if side not in {"LONG", "SHORT"}:
        return {
            "version": VERSION,
            "available": False,
            "phase": "NO_DATA",
            "entry_gate": "WAIT",
            "reason": "invalid_side",
            "score_is_probability": False,
        }

    ratio_5m = _f(metrics.get("taker_latest"), 1.0)
    ratio_15m_raw = metrics.get("taker_15m_ratio")
    ratio_15m = _f(ratio_15m_raw, _f(metrics.get("taker_avg_3"), 1.0))
    ratio_15m_source = (
        str(metrics.get("taker_15m_ratio_source") or "AGGREGATED_3X5M_BUYSELL_VOLUME")
        if ratio_15m_raw is not None
        else "THREE_5M_RATIO_PROXY"
    )
    futures_delta = _f(metrics.get("futures_delta_ratio"))
    spot_delta = _f(metrics.get("spot_delta_ratio"))
    oi_change = _f(metrics.get("oi_change_pct"))
    relative_volume = _f(metrics.get("relative_volume"), 1.0)
    volume_acceleration = _f(metrics.get("volume_acceleration"), 1.0)
    trend_15m = str(metrics.get("trend_15m") or "NEUTRAL").upper()
    trend_1h = str(metrics.get("trend_1h") or "NEUTRAL").upper()

    selected_absorption = bool(metrics.get("absorption_conflict"))
    if side == "LONG":
        selected_absorption = selected_absorption or bool(metrics.get("long_absorption_conflict"))
    else:
        selected_absorption = selected_absorption or bool(metrics.get("short_absorption_conflict"))

    ratio5_score = _ratio_score(side, ratio_5m)
    ratio15_score = _ratio_score(side, ratio_15m)
    flow_score = _delta_score(side, futures_delta)
    spot_score = _delta_score(side, spot_delta)
    delta_score = _clip(ratio5_score * 0.28 + ratio15_score * 0.37 + flow_score * 0.25 + spot_score * 0.10)
    if selected_absorption:
        delta_score = min(delta_score, 25.0)

    structure_score = _clip(_trend_score(side, trend_15m) * 0.65 + _trend_score(side, trend_1h) * 0.35)
    if selected_absorption:
        structure_score = min(structure_score, 38.0)

    btc_score, btc_hard_conflict, btc_notes = _btc_score(side, metrics, btc_overlay)
    oi_score = _oi_score(oi_change)
    volume_score = _volume_score(relative_volume, volume_acceleration)
    funding_top_score = _funding_top_score(side, metrics)
    extension_score, progress_r = _extension_score(
        side=side,
        current_price=current_price,
        entry_low=entry_low,
        entry_high=entry_high,
        entry_price=entry_price,
        stop=stop,
        tp1=tp1,
        is_open_position=is_open_position,
    )

    components = {
        "btc_market": btc_score,
        "structure_accumulation": structure_score,
        "delta_5m_15m": delta_score,
        "open_interest": oi_score,
        "volume": volume_score,
        "funding_top_traders": funding_top_score,
        "extension_rr": extension_score,
    }
    score = sum(components[key] * weight for key, weight in WEIGHTS.items())

    five_aligned = ratio5_score >= 58.0
    fifteen_aligned = ratio15_score >= 58.0
    fifteen_opposed = ratio15_score <= 35.0
    flow_opposed = flow_score <= 35.0
    structure_opposed = _trend_score(side, trend_15m) <= 25.0
    oi_deteriorated = oi_change <= -0.50

    contradictions: list[str] = []
    support: list[str] = []
    if five_aligned:
        support.append("chati_5m_aligned")
    else:
        contradictions.append("chati_5m_not_aligned")
    if fifteen_aligned:
        support.append("chati_15m_aligned")
    elif fifteen_opposed:
        contradictions.append("chati_15m_opposed")
    else:
        contradictions.append("chati_15m_mixed")
    if flow_score >= 60:
        support.append("aggressive_delta_aligned")
    elif flow_opposed:
        contradictions.append("aggressive_delta_opposed")
    if oi_change >= 0.30:
        support.append("open_interest_expanding")
    elif oi_deteriorated:
        contradictions.append("open_interest_deteriorating")
    if relative_volume >= 1.35 or volume_acceleration >= 1.25:
        support.append("volume_expanding")
    if selected_absorption:
        contradictions.append("absorption_against_thesis")
    if structure_opposed:
        contradictions.append("15m_structure_opposed")
    if btc_hard_conflict:
        contradictions.append("btc_high_stress_direction_conflict")
    contradictions.extend(note for note in btc_notes if "conflict" in note or "shock" in note)

    hard_damaged = bool(
        selected_absorption
        or btc_hard_conflict
        or (fifteen_opposed and oi_deteriorated)
        or (fifteen_opposed and flow_opposed and structure_opposed)
        or score < 38.0
    )

    confirmed = bool(
        not hard_damaged
        and five_aligned
        and fifteen_aligned
        and not selected_absorption
        and oi_change >= -0.25
        and not structure_opposed
        and not btc_hard_conflict
        and score >= 64.0
    )

    if hard_damaged:
        phase = "RED_DAMAGED"
        entry_gate = "BLOCK"
    elif confirmed:
        phase = "GREEN_CONFIRMATION"
        entry_gate = "ALLOW_CONFIRMATION"
    else:
        phase = "YELLOW_PREACTIVATION"
        entry_gate = "WAIT"

    if is_open_position:
        if phase == "GREEN_CONFIRMATION":
            management = "HOLD_PLAN"
        elif progress_r is not None and progress_r >= 1.0:
            management = "PROTECT_ONLY_IF_STRUCTURE_ALLOWS"
        elif phase == "RED_DAMAGED":
            management = "THESIS_DEGRADED_RESPECT_INVALIDATION"
        else:
            management = "CAUTION_REEVALUATE_5M_15M_OI_BTC"
    else:
        management = "ENTRY_ONLY_AFTER_GREEN_CONFIRMATION" if phase != "GREEN_CONFIRMATION" else "ENTRY_CONFIRMATION_PRESENT"

    return {
        "version": VERSION,
        "available": True,
        "manual_source": "CHATI_SARPON_612_CAZADOR_PUMPS_V1",
        "side": side,
        "phase": phase,
        "entry_gate": entry_gate,
        "manual_confluence_score": round(score, 2),
        "score_is_probability": False,
        "weights": WEIGHTS,
        "components": {key: round(value, 2) for key, value in components.items()},
        "chati": {
            "ratio_5m": round(ratio_5m, 4),
            "ratio_15m": round(ratio_15m, 4),
            "ratio_15m_source": ratio_15m_source,
            "taker_5m_delta": metrics.get("taker_5m_delta"),
            "taker_15m_delta": metrics.get("taker_15m_delta"),
            "futures_delta_ratio": round(futures_delta, 4),
            "spot_delta_ratio": round(spot_delta, 4),
            "five_minute_aligned": five_aligned,
            "fifteen_minute_aligned": fifteen_aligned,
        },
        "sarpon": {
            "trend_15m": trend_15m,
            "trend_1h": trend_1h,
            "structure_opposed": structure_opposed,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_invalidation": stop,
            "tp1": tp1,
            "no_chase_score": round(extension_score, 2),
        },
        "participation": {
            "oi_change_pct": round(oi_change, 4),
            "relative_volume": round(relative_volume, 4),
            "volume_acceleration": round(volume_acceleration, 4),
            "funding_rate": _f(metrics.get("funding_rate")),
        },
        "btc": {
            "trend": str(btc_overlay.get("direction") or metrics.get("btc_trend") or "NEUTRAL"),
            "stress": btc_overlay.get("stress"),
            "hard_conflict": btc_hard_conflict,
        },
        "absorption_against_thesis": selected_absorption,
        "support": list(dict.fromkeys(support)),
        "contradictions": list(dict.fromkeys(contradictions)),
        "progress_r": round(progress_r, 3) if progress_r is not None else None,
        "management": management,
        "rules": {
            "preactivation_is_not_entry": True,
            "green_requires_5m_and_15m_flow": True,
            "never_chase_vertical_extension": True,
            "does_not_flip_direction": True,
            "does_not_widen_live_stop": True,
            "may_downgrade_entry": True,
            "may_upgrade_wait_to_entry": False,
        },
        "note": "Manual 612 weighting is a discipline/confluence score. It is not a statistically calibrated probability.",
    }


async def persist_manual_monitor_for_run(db: AsyncSession, run_id: str) -> dict[str, Any]:
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT s.id::text AS signal_id, sy.symbol, s.direction, s.state,
               s.current_price, s.entry_low, s.entry_high, s.stop_loss, s.tp1,
               s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.scanner_run_id=CAST(:run_id AS UUID)
        ORDER BY s.created_at ASC
    """), {"run_id": run_id})).mappings().all()]

    updated = 0
    green = 0
    yellow = 0
    red = 0
    downgraded = 0

    for row in rows:
        reason = _d(row.get("reason"))
        prediction = _d(reason.get("prediction"))
        heart = _d(reason.get("explodex_heart")) or _d(prediction.get("explodex_heart"))
        if not heart:
            continue

        metrics = _d(reason.get("metrics"))
        plan = _d(heart.get("plan"))
        side = str(heart.get("direction") or row.get("direction") or "").upper()
        monitor = build_manual_monitor(
            side=side,
            metrics=metrics,
            current_price=_f(row.get("current_price")),
            entry_low=_f(plan.get("entry_low"), _f(row.get("entry_low"))),
            entry_high=_f(plan.get("entry_high"), _f(row.get("entry_high"))),
            entry_price=_f(row.get("current_price")),
            stop=_f(plan.get("stop_loss"), _f(row.get("stop_loss"))),
            tp1=_f(plan.get("tp1"), _f(row.get("tp1"))),
            btc_overlay=_d(heart.get("btc_overlay")),
            is_open_position=False,
        )

        phase = str(monitor.get("phase") or "")
        if phase == "GREEN_CONFIRMATION":
            green += 1
        elif phase == "RED_DAMAGED":
            red += 1
        else:
            yellow += 1

        decision = _d(heart.get("action_decision"))
        was_enter = bool(decision.get("should_enter"))
        if was_enter and monitor.get("entry_gate") != "ALLOW_CONFIRMATION":
            decision["should_enter"] = False
            decision["action"] = "NO_ENTRAR" if phase == "RED_DAMAGED" else "ESPERAR"
            decision["via"] = "CHATI_SARPON_612_MONITOR"
            decision["reason"] = (
                "CHATI/SARPON/612 degradó la entrada: PREACTIVACIÓN no es entrada confirmada. "
                "Se exige 5m+15m, OI, estructura y BTC sin contradicción fuerte."
            )
            downgraded += 1

        decision["chati_sarpon_612_phase"] = phase
        decision["chati_sarpon_612_score"] = monitor.get("manual_confluence_score")
        decision["chati_sarpon_612_gate"] = monitor.get("entry_gate")
        heart["action_decision"] = decision
        heart["execution_allowed"] = bool(decision.get("should_enter"))
        heart["chati_sarpon_612_monitor"] = monitor

        state = str(row.get("state") or "")
        if was_enter and not decision.get("should_enter"):
            state = "NO_TRADE" if phase == "RED_DAMAGED" else "PREPARING"

        reason["chati_sarpon_612_monitor"] = monitor
        reason["explodex_heart"] = heart
        if prediction:
            prediction["explodex_heart"] = heart
            prediction["chati_sarpon_612_monitor"] = monitor
            reason["prediction"] = prediction

        await db.execute(text("""
            UPDATE signals
            SET state=:state, reason=CAST(:reason AS JSONB), updated_at=NOW()
            WHERE id=CAST(:signal_id AS UUID)
        """), {
            "signal_id": row["signal_id"],
            "state": state,
            "reason": json.dumps(reason),
        })
        updated += 1

    await db.commit()
    return {
        "version": VERSION,
        "seen": len(rows),
        "updated": updated,
        "green_confirmation": green,
        "yellow_preactivation": yellow,
        "red_damaged": red,
        "downgraded_entries": downgraded,
        "can_create_entry": False,
        "can_flip_direction": False,
    }


async def monitor_open_paper_positions(
    db: AsyncSession,
    *,
    btc_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    btc_overlay = _d(btc_overlay)
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT pp.id, pp.symbol, pp.side, pp.entry_price, pp.stop_loss, pp.take_profit,
               pp.opened_at, pp.metadata,
               latest.current_price AS latest_signal_price,
               latest.created_at AS latest_signal_at,
               latest.reason AS latest_reason
        FROM paper_positions pp
        LEFT JOIN LATERAL (
            SELECT s.current_price, s.created_at, s.reason
            FROM signals s
            JOIN symbols sy ON sy.id=s.symbol_id
            WHERE sy.symbol=pp.symbol
            ORDER BY s.created_at DESC
            LIMIT 1
        ) latest ON TRUE
        WHERE pp.status='OPEN'
        ORDER BY pp.opened_at ASC
    """))).mappings().all()]

    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    counts = {"GREEN_CONFIRMATION": 0, "YELLOW_PREACTIVATION": 0, "RED_DAMAGED": 0, "STALE_DATA": 0}

    for row in rows:
        metadata = _d(row.get("metadata"))
        reason = _d(row.get("latest_reason"))
        metrics = _d(reason.get("metrics"))
        signal_at = row.get("latest_signal_at")
        if signal_at and signal_at.tzinfo is None:
            signal_at = signal_at.replace(tzinfo=timezone.utc)
        age_seconds = (now - signal_at).total_seconds() if signal_at else 999999.0
        stale = age_seconds > 180.0

        monitor = build_manual_monitor(
            side=str(row.get("side") or ""),
            metrics=metrics,
            current_price=_f(row.get("latest_signal_price"), _f(row.get("entry_price"))),
            entry_price=_f(row.get("entry_price")),
            stop=_f(metadata.get("hard_stop"), _f(row.get("stop_loss"))),
            tp1=_f(row.get("take_profit")),
            btc_overlay=btc_overlay,
            is_open_position=True,
        )
        monitor["data_age_seconds"] = round(age_seconds, 1) if age_seconds < 999999 else None
        monitor["data_status"] = "STALE" if stale else "LIVE_FROM_SCANNER"

        phase = str(monitor.get("phase") or "YELLOW_PREACTIVATION")
        if stale:
            counts["STALE_DATA"] += 1
            if phase == "GREEN_CONFIRMATION":
                monitor["phase"] = "YELLOW_PREACTIVATION"
                monitor["entry_gate"] = "WAIT"
                monitor["management"] = "CAUTION_DATA_STALE"
                phase = "YELLOW_PREACTIVATION"
        counts[phase] = counts.get(phase, 0) + 1

        previous = _d(metadata.get("chati_sarpon_612_monitor"))
        history = list(metadata.get("chati_sarpon_612_history") or [])
        if str(previous.get("phase") or "") != phase:
            history.append({
                "at": now.isoformat(),
                "phase": phase,
                "score": monitor.get("manual_confluence_score"),
                "management": monitor.get("management"),
            })
            history = history[-8:]

        metadata["chati_sarpon_612_monitor"] = monitor
        metadata["chati_sarpon_612_history"] = history
        await db.execute(text("""
            UPDATE paper_positions
            SET metadata=CAST(:metadata AS JSONB)
            WHERE id=:id AND status='OPEN'
        """), {"id": row["id"], "metadata": json.dumps(metadata)})

        items.append({
            "position_id": row.get("id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "phase": phase,
            "score": monitor.get("manual_confluence_score"),
            "management": monitor.get("management"),
            "chati": monitor.get("chati"),
            "oi_change_pct": _d(monitor.get("participation")).get("oi_change_pct"),
            "btc": monitor.get("btc"),
            "contradictions": monitor.get("contradictions"),
            "progress_r": monitor.get("progress_r"),
            "data_status": monitor.get("data_status"),
        })

    await db.commit()

    red_count = counts.get("RED_DAMAGED", 0)
    stale_count = counts.get("STALE_DATA", 0)
    if red_count >= 2:
        portfolio_risk_multiplier = 0.25
    elif red_count == 1:
        portfolio_risk_multiplier = 0.50
    elif rows and stale_count >= len(rows):
        portfolio_risk_multiplier = 0.70
    else:
        portfolio_risk_multiplier = 1.0

    return {
        "version": VERSION,
        "paper_only": True,
        "open_positions_checked": len(rows),
        "counts": counts,
        "portfolio_new_entry_risk_multiplier": portfolio_risk_multiplier,
        "positions": items,
        "active_management_changes_live_stop": False,
        "rule": "Open-trade monitor can reduce NEW exposure and flag thesis deterioration; it does not widen a live stop or flip direction.",
    }


async def open_monitor_report(db: AsyncSession) -> dict[str, Any]:
    rows = [dict(row) for row in (await db.execute(text("""
        SELECT id, symbol, side, metadata
        FROM paper_positions
        WHERE status='OPEN'
        ORDER BY opened_at ASC
    """))).mappings().all()]
    items = []
    for row in rows:
        metadata = _d(row.get("metadata"))
        monitor = _d(metadata.get("chati_sarpon_612_monitor"))
        if monitor:
            items.append({
                "position_id": row.get("id"),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "monitor": monitor,
                "history": list(metadata.get("chati_sarpon_612_history") or [])[-8:],
            })
    return {
        "version": VERSION,
        "paper_only": True,
        "open_positions": items,
        "score_is_probability": False,
    }
