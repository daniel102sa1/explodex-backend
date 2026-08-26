from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.context_meta_shadow import build_context_meta_shadow_report
from app.services.context_veto_shadow import build_graduated_veto_shadow
from app.services.fusion_edge_research import build_fusion_edge_research
from app.services.rolling_context_validation import build_rolling_context_validation
from app.services.runner_shadow_model import build_runner_shadow_model
from app.services.tp1_continuation_research import build_tp1_continuation_report, update_tp1_continuation_memory
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

    async def resolve_verdict_outcomes_v3(db: AsyncSession, limit: int = 60) -> dict[str, Any]:
        resolved = await resolve_verdict_outcomes_resilient(db, limit=limit)
        continuation = await update_tp1_continuation_memory(db, limit=max(120, limit * 2))
        return {**resolved, "tp1_continuation": continuation}

    async def verdict_memory_stats_v2(db: AsyncSession) -> dict[str, Any]:
        base = await original_stats(db)
        base["advanced_context"] = await advanced_context_stats(db)
        base["fusion_edge_research"] = await build_fusion_edge_research(db)
        base["tp1_continuation_research"] = await build_tp1_continuation_report(db)
        base["runner_shadow_model"] = await build_runner_shadow_model(db)
        meta = await build_context_meta_shadow_report(db)
        rolling = await build_rolling_context_validation(db)
        base["context_meta_shadow"] = meta
        base["rolling_context_validation"] = rolling
        base["context_veto_shadow"] = build_graduated_veto_shadow(meta, rolling)
        return base

    module.capture_enter_verdicts = capture_enter_verdicts_v2
    module.resolve_verdict_outcomes = resolve_verdict_outcomes_v3
    module.verdict_memory_stats = verdict_memory_stats_v2
    _INSTALLED = True
