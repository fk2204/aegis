"""Tests for the ``get_llm_cost_repository`` FastAPI dependency factory."""

from __future__ import annotations

import pytest

from aegis.api.deps import get_llm_cost_repository, reset_dependency_caches
from aegis.config import get_settings
from aegis.ops.llm_cost_repository import (
    InMemoryLLMCostRepository,
    LLMCostRepository,
    SupabaseLLMCostRepository,
)


def _clear_all_caches() -> None:
    get_settings.cache_clear()
    reset_dependency_caches()


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    _clear_all_caches()
    yield
    _clear_all_caches()


def test_returns_in_memory_when_backend_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_STORAGE_BACKEND", "memory")
    _clear_all_caches()
    repo: LLMCostRepository = get_llm_cost_repository()
    assert isinstance(repo, InMemoryLLMCostRepository)


def test_returns_supabase_when_backend_supabase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_STORAGE_BACKEND", "supabase")
    _clear_all_caches()
    repo: LLMCostRepository = get_llm_cost_repository()
    assert isinstance(repo, SupabaseLLMCostRepository)


def test_is_singleton_per_lru_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """``@lru_cache`` should keep the same instance for the process lifetime
    (until ``reset_dependency_caches`` is called)."""
    monkeypatch.setenv("AEGIS_STORAGE_BACKEND", "memory")
    _clear_all_caches()
    assert get_llm_cost_repository() is get_llm_cost_repository()
