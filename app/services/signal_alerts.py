from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def create_signal_alert(
    db: AsyncSession,
    *,
    signal_id: str,
    symbol_id: str,
    symbol: str,
    score: dict[str, Any],
) -> bool:
    """Create a deduplicated early prediction alert for PREPARING/READY setups.

    Alerts are informational/paper-trading signals, not guarantees. A PREPARING
    alert means conditions are aligning; READY means the deterministic engine
    passed its stronger entry filters at scan time.
    """
    state = str(score.get("state", "NO_TRADE"))
    if state not in {"PREPARING", "READY"}:
        return False

    direction = str(score.get("direction", ""))
    if direction not in {"LONG", "SHORT"}:
        return False

    setup_score = float(score.get("setup_score", 0) or 0)
    risk_score = float(score.get("risk_score", 100) or 100)
    confirmations = int(score.get("confirmations", 0) or 0)
    metrics = score.get("metrics") or {}

    if state == "READY":
        severity = "READY"
        title = f"🚨 {symbol} {direction} READY"
        intro = "Entrada potencial confirmada por el motor"
    else:
        severity = "EARLY"
        title = f"⚡ {symbol} posible {direction} temprano"
        intro = "Condiciones alineándose; todavía requiere confirmación"

    # Don't spam the same symbol/direction/state every scanner cycle.
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
                  AND a.created_at >= NOW() - INTERVAL '45 minutes'
            )
            """
        ),
        {"symbol_id": symbol_id, "direction": direction, "title": title},
    )
    if bool(duplicate.scalar_one()):
        return False

    entry_low = float(score.get("entry_low", 0) or 0)
    entry_high = float(score.get("entry_high", 0) or 0)
    stop_loss = float(score.get("stop_loss", 0) or 0)
    tp1 = float(score.get("tp1", 0) or 0)
    expected_min = float(score.get("expected_move_min_pct", 0) or 0)
    expected_max = float(score.get("expected_move_max_pct", 0) or 0)

    oi = float(metrics.get("oi_change_pct", 0) or 0)
    taker = float(metrics.get("taker_avg_3", 1) or 1)
    rvol = float(metrics.get("relative_volume", 1) or 1)
    btc = str(metrics.get("btc_trend", "NEUTRAL"))

    message = (
        f"{intro}. Score {setup_score:.1f}/100 | riesgo {risk_score:.1f}/100"
        f" | confirmaciones {confirmations}. Entrada {entry_low:.8g}-{entry_high:.8g}"
        f" | SL {stop_loss:.8g} | TP1 {tp1:.8g}"
        f" | potencial estimado {expected_min:.1f}-{expected_max:.1f}%"
        f" | OI {oi:+.2f}% | taker {taker:.2f} | rVol {rvol:.2f} | BTC {btc}."
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
