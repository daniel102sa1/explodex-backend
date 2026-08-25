from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import scanner as scanner_module
from app.services.prediction_guarded import build_pre_move_prediction


async def run_scanner(db: AsyncSession, deep_limit: int = 20) -> dict[str, Any]:
    # scanner.py historically imported the raw predictor. Keep its persistence
    # logic intact but replace that module-level callable for this cycle so every
    # persisted READY candidate must pass Risk Guard V2 as well.
    scanner_module.build_pre_move_prediction = build_pre_move_prediction
    return await scanner_module.run_scanner(db, deep_limit=deep_limit)
