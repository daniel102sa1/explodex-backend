from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import scanner as scanner_module
from app.services.heart_persistence import canonicalize_scanner_run
from app.services.microstructure_persistence_resilient import install_microstructure_persistence_hardening
from app.services.prediction_guarded import build_pre_move_prediction
from app.services.scanner_edge_gate import apply_edge_gate_to_scanner_run
from app.services.server_snapshot_extensions import install_server_snapshot_extensions
from app.services.verdict_memory_override import install_verdict_memory_overrides

# Install runtime extensions before runtime.py imports direct service callables.
install_verdict_memory_overrides()
install_microstructure_persistence_hardening()
install_server_snapshot_extensions()

# Preserve the raw scanner callable exactly once.
_raw_run_scanner = scanner_module.run_scanner

# The guarded prediction stack is the mathematical input to the unified heart.
scanner_module.build_pre_move_prediction = build_pre_move_prediction


async def run_scanner(db: AsyncSession, deep_limit: int = 20) -> dict[str, Any]:
    scanner_module.build_pre_move_prediction = build_pre_move_prediction
    result = await _raw_run_scanner(db, deep_limit=deep_limit)
    run_id = str(result.get("run_id") or "")
    if run_id:
        # Statistical calibration remains an evidence layer, but it is no longer
        # allowed to become a separate source of truth from the persisted signal.
        try:
            result["edge_gate"] = await apply_edge_gate_to_scanner_run(db, run_id)
        except Exception as exc:
            result["edge_gate"] = {
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }

        # Final canonicalization happens after every other scanner layer. This
        # freezes/maintains the thesis and writes the one decision that Validation
        # Lab and PAPER are allowed to consume.
        try:
            result["explodex_heart"] = await canonicalize_scanner_run(db, run_id)
        except Exception as exc:
            result["explodex_heart"] = {
                "version": "explodex_heart_v1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
            # Heart persistence is safety-critical. Surface the failure in the
            # result instead of silently pretending decisions are unified.
    return result


# Runtime/manual scanner callers receive the same guarded + canonical lifecycle.
scanner_module.run_scanner = run_scanner
