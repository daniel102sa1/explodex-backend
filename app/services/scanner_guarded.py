from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import scanner as scanner_module
from app.services.prediction_guarded import build_pre_move_prediction

# Apply immediately on import. runtime.py imports this module during backend
# startup, so scanner.py's module-level predictor is guarded before any manual or
# scheduled scanner cycle can run.
scanner_module.build_pre_move_prediction = build_pre_move_prediction


async def run_scanner(db: AsyncSession, deep_limit: int = 20) -> dict[str, Any]:
    # Re-apply defensively in case another import/test replaced the callable.
    scanner_module.build_pre_move_prediction = build_pre_move_prediction
    return await scanner_module.run_scanner(db, deep_limit=deep_limit)
