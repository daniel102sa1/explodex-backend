import asyncio

from app.services import practice_ai


def test_practice_ai_budget_and_cooldown(monkeypatch):
    practice_ai._AI_USAGE.clear()
    monkeypatch.setattr(practice_ai.settings, "practice_ai_daily_limit", 2)
    monkeypatch.setattr(practice_ai.settings, "practice_ai_cooldown_seconds", 0)

    async def exercise():
        assert await practice_ai._reserve_practice_ai("practice-test-a") is None
        assert await practice_ai._reserve_practice_ai("practice-test-a") is None
        assert "límite diario" in (await practice_ai._reserve_practice_ai("practice-test-a") or "")
        assert await practice_ai._reserve_practice_ai("practice-test-b") is None

    asyncio.run(exercise())
    practice_ai._AI_USAGE.clear()


def test_practice_ai_budget_rejects_missing_session(monkeypatch):
    practice_ai._AI_USAGE.clear()
    monkeypatch.setattr(practice_ai.settings, "practice_ai_daily_limit", 8)
    assert "Sesión" in (asyncio.run(practice_ai._reserve_practice_ai("")) or "")


def test_practice_ai_can_be_disabled_without_database(monkeypatch):
    practice_ai._AI_USAGE.clear()
    monkeypatch.setattr(practice_ai.settings, "practice_ai_daily_limit", 0)
    assert "desactivada" in (asyncio.run(practice_ai._reserve_practice_ai("practice-test-x")) or "")
