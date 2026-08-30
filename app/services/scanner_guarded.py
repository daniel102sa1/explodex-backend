from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import scanner as scanner_module
from app.services.entry_latch_persistence import apply_entry_latches_for_run
from app.services.heart_persistence import canonicalize_scanner_run
from app.services.microstructure_persistence_resilient import install_microstructure_persistence_hardening
from app.services.plan_lifecycle_persistence import expire_exhausted_plans_for_run
from app.services.prediction_guarded import build_pre_move_prediction
from app.services.scanner_edge_gate import apply_edge_gate_to_scanner_run
from app.services.server_snapshot_extensions import install_server_snapshot_extensions
from app.services.verdict_memory_override import install_verdict_memory_overrides

# Install runtime extensions before runtime.py imports direct service callables.
install_verdict_memory_overrides()
install_microstructure_persistence_hardening()
install_server_snapshot_extensions()

_raw_run_scanner = scanner_module.run_scanner
scanner_module.build_pre_move_prediction = build_pre_move_prediction


async def run_scanner(db: AsyncSession, deep_limit: int = 20) -> dict[str, Any]:
    scanner_module.build_pre_move_prediction = build_pre_move_prediction
    result = await _raw_run_scanner(db, deep_limit=deep_limit)
    run_id = str(result.get("run_id") or "")
    if run_id:
        try:
            result["edge_gate"] = await apply_edge_gate_to_scanner_run(db, run_id)
        except Exception as exc:
            result["edge_gate"] = {
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }

        try:
            result["explodex_heart"] = await canonicalize_scanner_run(db, run_id)
        except Exception as exc:
            result["explodex_heart"] = {
                "version": "explodex_heart_v1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }

        # Entry latch runs immediately after canonicalization. The first ENTER
        # becomes a persistent activated plan. Later weaker scans may update the
        # radar, but cannot revert the user-facing action to "wait for confirmation".
        try:
            result["entry_latch"] = await apply_entry_latches_for_run(db, run_id)
        except Exception as exc:
            result["entry_latch"] = {
                "version": "entry_latch_persistence_v1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }

        # Missed TP3 lifecycle runs after the latch. A triggered entry must not
        # be classified as a missed opportunity just because PAPER/manual fill
        # state is not yet known.
        try:
            result["plan_lifecycle"] = await expire_exhausted_plans_for_run(db, run_id)
        except Exception as exc:
            result["plan_lifecycle"] = {
                "version": "plan_lifecycle_persistence_v1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
    return result


scanner_module.run_scanner = run_scanner
