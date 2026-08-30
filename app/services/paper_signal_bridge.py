from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VERSION = "paper_signal_bridge_v2_legacy_safe_fk"


def as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


async def ensure_signal_fk(db: AsyncSession) -> None:
    """Make new PAPER rows reference canonical signals without breaking history.

    Historical paper_positions were created when signal_id referenced
    validation_observations. Those UUIDs are preserved as trades, but orphan
    links are nulled before the new FK is installed. This prevents the migration
    itself from aborting every automatic PAPER cycle.
    """
    await db.execute(text("""
        DO $$
        DECLARE c RECORD;
        BEGIN
            FOR c IN
                SELECT conname, pg_get_constraintdef(oid) AS definition
                FROM pg_constraint
                WHERE conrelid = 'paper_positions'::regclass
                  AND contype = 'f'
            LOOP
                IF c.definition ILIKE '%validation_observations%' THEN
                    EXECUTE format('ALTER TABLE paper_positions DROP CONSTRAINT %I', c.conname);
                END IF;
            END LOOP;
        END $$;
    """))

    # Keep all historical position/PnL rows; only detach obsolete signal UUIDs.
    await db.execute(text("""
        UPDATE paper_positions pp
        SET signal_id=NULL,
            metadata=COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                'legacy_signal_link_detached', TRUE,
                'legacy_signal_bridge_version', :version
            )
        WHERE pp.signal_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM signals s WHERE s.id=pp.signal_id)
    """), {"version": VERSION})

    await db.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = 'paper_positions'::regclass
                  AND conname = 'paper_positions_signal_id_signals_fkey'
            ) THEN
                ALTER TABLE paper_positions
                ADD CONSTRAINT paper_positions_signal_id_signals_fkey
                FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """))
    await db.commit()


async def heart_diagnostics(db: AsyncSession, minutes: int = 30) -> dict[str, Any]:
    minutes = max(5, min(int(minutes), 180))
    rows = (await db.execute(text(f"""
        SELECT DISTINCT ON (s.symbol_id)
               sy.symbol, s.created_at, s.state, s.direction, s.reason
        FROM signals s
        JOIN symbols sy ON sy.id=s.symbol_id
        WHERE s.is_active=TRUE
          AND s.created_at >= NOW() - INTERVAL '{minutes} minutes'
        ORDER BY s.symbol_id, s.created_at DESC
    """))).mappings().all()

    actions: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    enter_symbols: list[str] = []
    latest: list[dict[str, Any]] = []

    for raw in rows:
        row = dict(raw)
        reason = as_dict(row.get("reason"))
        prediction = as_dict(reason.get("prediction"))
        heart = as_dict(reason.get("explodex_heart")) or as_dict(prediction.get("explodex_heart"))
        decision = as_dict(heart.get("action_decision"))
        action = str(decision.get("action") or "SIN_HEART")
        actions[action] += 1
        for item in decision.get("advanced_stack_missing") or []:
            missing[str(item)] += 1
        if action in {"ENTRAR_LONG", "ENTRAR_SHORT"} and bool(decision.get("should_enter")):
            enter_symbols.append(str(row.get("symbol")))
        latest.append({
            "symbol": row.get("symbol"),
            "direction": heart.get("direction") or row.get("direction"),
            "action": action,
            "state": heart.get("state") or row.get("state"),
            "reason": decision.get("reason"),
            "price_in_entry_zone": decision.get("price_in_entry_zone"),
            "advanced_stack_ready": decision.get("advanced_stack_ready"),
        })

    latest.sort(key=lambda item: 0 if item["action"] in {"ENTRAR_LONG", "ENTRAR_SHORT"} else 1)
    return {
        "version": VERSION,
        "window_minutes": minutes,
        "signals_checked": len(rows),
        "enter_signals": len(enter_symbols),
        "enter_symbols": enter_symbols[:10],
        "actions": dict(actions),
        "missing_checks": dict(missing),
        "latest": latest[:12],
    }
