from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import scanner as scanner_module
from app.services.prediction_guarded import build_pre_move_prediction
from app.services.scanner_edge_gate import apply_edge_gate_to_scanner_run
from app.services.verdict_memory_override import install_verdict_memory_overrides

# Install verdict-memory extensions before runtime imports verdict_memory callables.
# This keeps provider failures isolated per symbol and enriches every decision with
# the advanced context that existed at decision time.
install_verdict_memory_overrides()

# Preserve the raw scanner callable exactly once. This module is imported during
# backend startup before main.py imports run_scanner from scanner.py.
_raw_run_scanner = scanner_module.run_scanner

# Risk Guard V2 must be the predictor used inside scanner.py.
scanner_module.build_pre_move_prediction = build_pre_move_prediction


async def run_scanner(db: AsyncSession, deep_limit: int = 20) -> dict[str, Any]:
    scanner_module.build_pre_move_prediction = build_pre_move_prediction
    result = await _raw_run_scanner(db, deep_limit=deep_limit)
    run_id = str(result.get("run_id") or "")
    if run_id:
        try:
            result["edge_gate"] = await apply_edge_gate_to_scanner_run(db, run_id)
        except Exception as exc:
            # Statistical gating should fail closed only when calibrated evidence
            # exists and can be evaluated. A service error is surfaced loudly but
            # does not silently rewrite technical states without evidence.
            result["edge_gate"] = {
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
    return result


# Patch the scanner module itself so callers importing scanner.run_scanner after
# startup (including main.py manual /scanner/run) receive the guarded lifecycle.
scanner_module.run_scanner = run_scanner
