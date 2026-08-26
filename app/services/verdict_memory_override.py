from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.context_meta_shadow import build_context_meta_shadow_report
from app.services.context_veto_shadow import build_graduated_veto_shadow
from app.services.rolling_context_validation import build_rolling_context_validation
from app.services.verdict_context_enrichment import advanced_context_stats, enrich_verdict_memory_context
from app.services.verdict_resolver_resilient import resolve_verdict_outcomes_resilient

_INSTALLED = False


def install_verdict_memory_overrides() -> None:
    """Install conservative verdict-memory extensions before runtime imports them."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services import verdict_memory as module

    original_capture = module.capture_enter_verdicts
    original_stats = module.verdict_memory_stats

    async def capture_enter_verdicts_v2(db: AsyncSession, limit: int = 200) -> dict[str, Any]:
        captured = await original_capture(db, limit=limit)
        enriched = await enrich_verdict_memory_context(db, limit=max(300, limit * 2))
        return {**captured, "advanced_context_enrichment": enriched}

    async def verdict_memory_stats_v2(db: AsyncSession) -> dict[str, Any]:
        base = await original_stats(db)
        base["advanced_context"] = await advanced_context_stats(db)
        meta = await build_context_meta_shadow_report(db)
        rolling = await build_rolling_context_validation(db)
        base["context_meta_shadow"] = meta
        base["rolling_context_validation"] = rolling
        base["context_veto_shadow"] = build_graduated_veto_shadow(meta, rolling)
        return base

    module.capture_enter_verdicts = capture_enter_verdicts_v2
    module.resolve_verdict_outcomes = resolve_verdict_outcomes_resilient
    module.verdict_memory_stats = verdict_memory_stats_v2
    _INSTALLED = True
