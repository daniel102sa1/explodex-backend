from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _label_type(value: str) -> str:
    return {
        "IMPULSO_LONG": "IMPULSO LONG",
        "IMPULSO_SHORT": "IMPULSO SHORT",
        "REBOTE_LONG": "REBOTE LONG",
        "RECHAZO_SHORT": "RECHAZO SHORT",
    }.get(value, value.replace("_", " "))


async def create_signal_alert(
    db: AsyncSession,
    *,
    signal_id: str,
    symbol_id: str,
    symbol: str,
    score: dict[str, Any],
) -> bool:
    """Create a deduplicated early/activated/READY prediction alert.

    Alerts remain informational/paper-trading signals. The prediction phase is
    included so PREACTIVACION can never be confused with an allowed entry.
    """
    state = str(score.get("state", "NO_TRADE"))
    if state not in {"PREPARING", "READY"}:
        return False

    direction = str(score.get("direction", ""))
    if direction not in {"LONG", "SHORT"}:
        return False

    prediction = score.get("prediction") or {}
    prediction_type = str(prediction.get("type") or "SETUP")
    phase = str(prediction.get("phase") or "VIGILAR")
    setup_score = float(score.get("setup_score", 0) or 0)
    pre_score = float(prediction.get("preactivation_score", 0) or 0)
    risk_score = float(score.get("risk_score", 100) or 100)
    metrics = score.get("metrics") or {}
    confirmations = list(prediction.get("confirmations") or [])
    conflicts = list(prediction.get("conflicts") or [])

    if state == "READY" and phase == "ACTIVADO":
        severity = "READY"
        title = f"🚨 {symbol} {_label_type(prediction_type)} · READY"
        intro = "Trigger activado y entrada habilitada por el motor PAPER"
    elif phase == "ACTIVADO":
        severity = "ACTIVATED"
        title = f"🟠 {symbol} {_label_type(prediction_type)} · ACTIVADO"
        intro = "El trigger fue alcanzado, pero todavía falta una condición para READY"
    elif prediction_type == "REBOTE_LONG":
        severity = "EARLY"
        title = f"🟣 {symbol} posible REBOTE LONG"
        intro = "Barrido/rechazo inferior en preparación; todavía no entrar"
    elif prediction_type == "RECHAZO_SHORT":
        severity = "EARLY"
        title = f"🔴 {symbol} posible RECHAZO SHORT"
        intro = "Barrido/rechazo superior en preparación; todavía no entrar"
    else:
        severity = "EARLY"
        title = f"⚡ {symbol} PREACTIVACIÓN {direction}"
        intro = "Condiciones previas alineándose; todavía no entrar"

    duplicate = await db.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM alerts a
                JOIN signals s ON s.id = a.signal_id
                WHERE s.symbol_id = :symbol_id
                  AND s.direction = :direction
                  AND a.title = :title
                  AND a.created_at >= NOW() - INTERVAL '30 minutes'
            )
            """
        ),
        {"symbol_id": symbol_id, "direction": direction, "title": title},
    )
    if bool(duplicate.scalar_one()):
        return False

    entry_low = float(score.get("entry_low", 0) or 0)
    entry_high = float(score.get("entry_high", 0) or 0)
    trigger = float(prediction.get("trigger_price", 0) or 0)
    stop_loss = float(score.get("stop_loss", 0) or 0)
    tp1 = float(score.get("tp1", 0) or 0)
    tp2 = float(score.get("tp2", 0) or 0)
    tp3 = float(score.get("tp3", 0) or 0)
    time_stop = int(prediction.get("time_stop_minutes", 0) or 0)
    oi = float(metrics.get("oi_change_pct", 0) or 0)
    taker = float(metrics.get("taker_avg_3", 1) or 1)
    rvol = float(metrics.get("relative_volume", 1) or 1)
    btc = str(metrics.get("btc_trend", "NEUTRAL"))

    status_instruction = (
        "ENTRADA PAPER permitida solo dentro de la zona"
        if state == "READY" and phase == "ACTIVADO"
        else "NO ENTRAR: esperar activación/READY"
    )
    confirm_text = ", ".join(confirmations[:4]) if confirmations else "sin confirmaciones suficientes"
    conflict_text = ", ".join(conflicts[:3]) if conflicts else "sin conflictos fuertes"

    message = (
        f"{intro}. {status_instruction}. Preparación {pre_score:.1f}/100 | setup {setup_score:.1f}/100"
        f" | riesgo {risk_score:.1f}/100 | trigger {trigger:.8g}"
        f" | entrada {entry_low:.8g}-{entry_high:.8g} | SL {stop_loss:.8g}"
        f" | TP1 {tp1:.8g} | TP2 {tp2:.8g} | TP3 {tp3:.8g}"
        f" | time stop {time_stop or '—'} min"
        f" | OI {oi:+.2f}% | taker {taker:.2f} | rVol {rvol:.2f} | BTC {btc}"
        f" | a favor: {confirm_text} | conflictos: {conflict_text}."
        " Predicción probabilística, no garantía."
    )

    await db.execute(
        text(
            """
            INSERT INTO alerts (signal_id, trade_id, channel, severity, title, message, is_sent)
            VALUES (:signal_id, NULL, 'APP', :severity, :title, :message, FALSE)
            """
        ),
        {
            "signal_id": signal_id,
            "severity": severity,
            "title": title,
            "message": message,
        },
    )
    return True
