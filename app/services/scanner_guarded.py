from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import scanner as scanner_module
from app.services.elliott_persistence import persist_elliott_for_run
from app.services.entry_latch_persistence import apply_entry_latches_for_run
from app.services.event_risk_persistence import persist_event_risk_for_run
from app.services.heart_persistence import canonicalize_scanner_run
from app.services.horizon_matrix_persistence import persist_horizon_matrix_for_run
from app.services.market_breadth_persistence import persist_market_breadth_for_run
from app.services.microstructure_persistence_resilient import install_microstructure_persistence_hardening
from app.services.plan_lifecycle_persistence import expire_exhausted_plans_for_run
from app.services.pre_event_persistence import persist_pre_event_for_run
from app.services.prediction_guarded import build_pre_move_prediction
from app.services.scanner_edge_gate import apply_edge_gate_to_scanner_run
from app.services.server_snapshot_extensions import install_server_snapshot_extensions
from app.services.shadow_forecast_memory import capture_shadow_forecasts_for_run, evaluate_shadow_forecasts, persist_shadow_calibration_for_run
from app.services.storage_guard import SCANNER_JSON_PROXY, prune_market_snapshots
from app.services.trajectory_persistence import persist_trajectory_for_run
from app.services.unified_heart_contract import finalize_unified_contract_for_run
from app.services.verdict_memory_override import install_verdict_memory_overrides

install_verdict_memory_overrides()
install_microstructure_persistence_hardening()
install_server_snapshot_extensions()

_raw_run_scanner = scanner_module.run_scanner
scanner_module.build_pre_move_prediction = build_pre_move_prediction
# Replace json only inside scanner.py so its duplicated market_snapshots.raw_data
# bundle is compacted. Normal signal reason/learning JSON remains untouched.
scanner_module.json = SCANNER_JSON_PROXY


async def run_scanner(db: AsyncSession, deep_limit: int = 20) -> dict[str, Any]:
    scanner_module.build_pre_move_prediction = build_pre_move_prediction
    scanner_module.json = SCANNER_JSON_PROXY

    storage_cleanup: dict[str, Any]
    try:
        storage_cleanup = await prune_market_snapshots(db)
    except Exception as exc:
        # Cleanup is best-effort. If the DB is completely full the DELETE may
        # also fail; the scanner then surfaces the original storage problem.
        await db.rollback()
        storage_cleanup = {
            "version": "storage_guard_v1",
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "hint": "Database may already be at zero free bytes; increase/free Railway storage once, then automatic retention will keep it bounded.",
        }

    result = await _raw_run_scanner(db, deep_limit=deep_limit)
    result["storage_cleanup"] = storage_cleanup
    run_id = str(result.get("run_id") or "")
    if run_id:
        steps = [
            ("edge_gate", apply_edge_gate_to_scanner_run, "scanner_edge_gate"),
            ("explodex_heart", canonicalize_scanner_run, "explodex_heart"),
            ("trajectory_forecast", persist_trajectory_for_run, "trajectory_persistence"),
            ("entry_latch", apply_entry_latches_for_run, "entry_latch_persistence"),
            ("plan_lifecycle", expire_exhausted_plans_for_run, "plan_lifecycle_persistence"),
            ("unified_heart_contract", finalize_unified_contract_for_run, "unified_heart_contract"),
            ("horizon_forecast_matrix", persist_horizon_matrix_for_run, "horizon_matrix_persistence"),
            ("elliott_structure", persist_elliott_for_run, "elliott_persistence"),
            ("event_risk", persist_event_risk_for_run, "event_risk_persistence"),
            ("pre_event_prediction", persist_pre_event_for_run, "pre_event_persistence"),
            ("market_breadth", persist_market_breadth_for_run, "market_breadth"),
        ]
        for key, fn, version in steps:
            try:
                result[key] = await fn(db, run_id)
            except Exception as exc:
                result[key] = {"version": f"{version}_v1", "status": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}

        try:
            result["shadow_forecast_evaluation"] = await evaluate_shadow_forecasts(db, limit=80)
        except Exception as exc:
            result["shadow_forecast_evaluation"] = {"version": "shadow_forecast_memory_v1", "status": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        try:
            result["shadow_calibration"] = await persist_shadow_calibration_for_run(db, run_id)
        except Exception as exc:
            result["shadow_calibration"] = {"version": "shadow_forecast_memory_v1", "status": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        try:
            result["shadow_forecast_capture"] = await capture_shadow_forecasts_for_run(db, run_id)
        except Exception as exc:
            result["shadow_forecast_capture"] = {"version": "shadow_forecast_memory_v1", "status": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    return result


scanner_module.run_scanner = run_scanner
