"""FastAPI dependency factories.

Single place for the wiring decision "which repository / audit log /
LLM client am I getting?". Tests override these via
``app.dependency_overrides`` to inject fakes.

The defaults read settings: when ``aegis_storage_backend == "memory"``
(test default) the in-memory implementations are used; when ``"supabase"``
(production default) the Supabase-backed implementations are used.
"""

from __future__ import annotations

from functools import lru_cache

from aegis.audit import AuditLog, InMemoryAuditLog, SupabaseAuditLog
from aegis.config import get_settings
from aegis.funders.repository import (
    FunderRepository,
    InMemoryFunderRepository,
    SupabaseFunderRepository,
)
from aegis.llm import BedrockClient, LLMClient
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    MerchantRepository,
    SupabaseMerchantRepository,
)
from aegis.storage import (
    DocumentRepository,
    InMemoryDocumentRepository,
    SupabaseDocumentRepository,
)


@lru_cache(maxsize=1)
def get_repository() -> DocumentRepository:
    """Process-wide DocumentRepository. Backend chosen by settings."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryDocumentRepository()
    return SupabaseDocumentRepository()


@lru_cache(maxsize=1)
def get_audit() -> AuditLog:
    """Process-wide AuditLog. Same backend toggle as the repository."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryAuditLog()
    return SupabaseAuditLog()


@lru_cache(maxsize=1)
def get_merchant_repository() -> MerchantRepository:
    """Process-wide MerchantRepository. Same backend toggle as documents."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryMerchantRepository()
    return SupabaseMerchantRepository()


@lru_cache(maxsize=1)
def get_funder_repository() -> FunderRepository:
    """Process-wide FunderRepository. Same backend toggle as documents."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryFunderRepository()
    return SupabaseFunderRepository()


@lru_cache(maxsize=1)
def get_llm() -> LLMClient:
    """Production LLM client. Tests override with a fake via dependency_overrides."""
    return BedrockClient()


def reset_dependency_caches() -> None:
    """Drop the lru_cache singletons. For tests that swap settings."""
    get_repository.cache_clear()
    get_merchant_repository.cache_clear()
    get_funder_repository.cache_clear()
    get_audit.cache_clear()
    get_llm.cache_clear()


__all__ = [
    "get_audit",
    "get_funder_repository",
    "get_llm",
    "get_merchant_repository",
    "get_repository",
    "reset_dependency_caches",
]
