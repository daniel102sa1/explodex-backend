from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import scanner as scanner_module
from app.services.entry_latch_persistence import apply_entry_latches_for_run
from app.services.heart_persistence import canonicalize_scanner_run
from app.services.horizon_matrix_persistence import persist_horizon_matrix_for_run
from app.services.microstructure_persistence_resilient import install_microstructure_persistence_hardening
from app.services.plan_lifecycle_persistence import expire_exhausted_plans_for_run
from app.services.prediction_guarded import build_pre_move_prediction
from app.services.scanner_edge_gate import apply_edge_gate_to_scanner_run
from app.services.server_snapshot_extensions import install_server_snapshot_extensions
from app.services.trajectory_persistence import persist_trajectory_for_run
from app.services.unified_heart_contract import finalize_unified_contract_for_run
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

        try:
            result["trajectory_forecast"] = await persist_trajectory_for_run(db, run_id)
        except Exception as exc:
            result["trajectory_forecast"] = {
                "version": "trajectory_persistence_v1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }

        try:
            result["entry_latch"] = await apply_entry_latches_for_run(db, run_id)
        except Exception as exc:
            result["entry_latch"] = {
                "version": "entry_latch_persistence_v1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }

        try:
            result["plan_lifecycle"] = await expire_exhausted_plans_for_run(db, run_id)
        except Exception as exc:
            result["plan_lifecycle"] = {
                "version": "plan_lifecycle_persistence_v1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }

        # Final authority. Every upstream module contributes evidence/state, but
        # only this contract is allowed to authorize a PAPER lane or expose the
        # primary prediction consumed by the app.
        try:
            result["unified_heart_contract"] = await finalize_unified_contract_for_run(db, run_id)
        except Exception as exc:
            result["unified_heart_contract"] = {
                "version": "unified_heart_contract_v1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }

        # The matrix is not another authority. It is persisted into that same
        # contract so the Heart can express 15m/1h/4h/6h/24h disagreement without
        # creating a second LONG/SHORT decision path.
        try:
            result["horizon_forecast_matrix"] = await persist_horizon_matrix_for_run(db, run_id)
        except Exception as exc:
            result["horizon_forecast_matrix"] = {
                "version": "horizon_matrix_persistence_v1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
    return result


scanner_module.run_scanner = run_scanner
