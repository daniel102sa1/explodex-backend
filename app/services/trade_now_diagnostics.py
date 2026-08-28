from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.validation_mode import ensure_validation_schema


def _fingerprint(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get("fingerprint")
    return value if isinstance(value, dict) else {}


async def trade_now_reachability_report(db: AsyncSession, limit: int = 2000) -> dict[str, Any]:
    await ensure_validation_schema(db)
    rows = [dict(r) for r in (await db.execute(text("""
        SELECT symbol, observed_at, trade_class, fingerprint_score, locks_passed, payload
        FROM validation_observations
        WHERE observed_at >= NOW() - INTERVAL '30 days'
        ORDER BY observed_at DESC
        LIMIT :limit
    """), {"limit": limit})).mappings().all()]

    class_counts: Counter[str] = Counter()
    distance_counts: Counter[int] = Counter()
    blockers: Counter[str] = Counter()
    near_examples: list[dict[str, Any]] = []

    for row in rows:
        trade_class = str(row.get("trade_class") or "UNCLASSIFIED")
        class_counts[trade_class] += 1
        fp = _fingerprint(row.get("payload"))
        missing = fp.get("yes_missing") if isinstance(fp.get("yes_missing"), list) else []
        steps = fp.get("steps_to_yes")
        try:
            steps_int = int(steps) if steps is not None else len(missing)
        except (TypeError, ValueError):
            steps_int = len(missing)
        steps_int = max(0, min(9, steps_int))
        distance_counts[steps_int] += 1
        blockers.update(str(x) for x in missing if x)

        if steps_int <= 2 and len(near_examples) < 25:
            near_examples.append({
                "symbol": row.get("symbol"),
                "observed_at": row.get("observed_at").isoformat() if row.get("observed_at") else None,
                "trade_class": trade_class,
                "fingerprint_score": float(row.get("fingerprint_score") or 0),
                "locks_passed": int(row.get("locks_passed") or 0),
                "steps_to_yes": steps_int,
                "yes_missing": missing,
            })

    total = len(rows)
    trade_now = class_counts.get("TRADE_NOW", 0)
    within_one = sum(count for distance, count in distance_counts.items() if distance <= 1)
    within_two = sum(count for distance, count in distance_counts.items() if distance <= 2)

    if total == 0:
        diagnosis = "SIN_DATOS"
    elif trade_now > 0:
        diagnosis = "TRADE_NOW_OBSERVED"
    elif within_one > 0:
        diagnosis = "VERY_CLOSE_BUT_NONE_YET"
    elif within_two > 0:
        diagnosis = "CLOSE_BUT_STRICT"
    else:
        diagnosis = "POSSIBLY_OVERFILTERED"

    return {
        "version": "trade_now_reachability_v1",
        "paper_research_only": True,
        "window_days": 30,
        "sample": total,
        "diagnosis": diagnosis,
        "trade_class_counts": dict(class_counts),
        "trade_now_count": trade_now,
        "trade_now_rate_pct": round(trade_now / total * 100.0, 3) if total else None,
        "distance_to_yes": {str(k): v for k, v in sorted(distance_counts.items())},
        "within_1_condition": within_one,
        "within_2_conditions": within_two,
        "top_blockers": [
            {"condition": name, "count": count, "pct_of_sample": round(count / total * 100.0, 2) if total else 0.0}
            for name, count in blockers.most_common(10)
        ],
        "near_examples": near_examples,
        "note": "Diagnóstico descriptivo. No relaja reglas ni crea entradas; sirve para detectar si TRADE_NOW es alcanzable en datos reales.",
    }
