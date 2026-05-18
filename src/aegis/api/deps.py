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
from aegis.compliance.snapshot import (
    DecisionSnapshot,
    InMemoryDecisionSnapshot,
    SupabaseDecisionSnapshot,
)
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
from aegis.scoring.ofac import OFACClient
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


@lru_cache(maxsize=1)
def get_decision_snapshot() -> DecisionSnapshot:
    """Process-wide DecisionSnapshot writer (mp Phase 2).

    Memory backend → in-memory list (tests); supabase → the real writer
    that inserts into the immutable ``decisions`` table.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryDecisionSnapshot()
    return SupabaseDecisionSnapshot()


@lru_cache(maxsize=1)
def get_ofac_client() -> OFACClient | None:
    """OFAC SDN screening client. Returns ``None`` in in-memory test mode so
    the deals route doesn't hit the Treasury feed during unit tests; tests
    that need OFAC behavior override this dependency with a fake client.
    Production (storage_backend == "supabase") returns a real client whose
    cache lives at ``settings.aegis_ofac_cache_path``.
    """
    settings = get_settings()
    if settings.aegis_storage_backend == "memory":
        return None
    settings.aegis_ofac_cache_path.parent.mkdir(parents=True, exist_ok=True)
    return OFACClient(cache_path=settings.aegis_ofac_cache_path)


def reset_dependency_caches() -> None:
    """Drop the lru_cache singletons. For tests that swap settings."""
    get_repository.cache_clear()
    get_merchant_repository.cache_clear()
    get_funder_repository.cache_clear()
    get_audit.cache_clear()
    get_decision_snapshot.cache_clear()
    get_llm.cache_clear()
    get_ofac_client.cache_clear()


__all__ = [
    "get_audit",
    "get_decision_snapshot",
    "get_funder_repository",
    "get_llm",
    "get_merchant_repository",
    "get_ofac_client",
    "get_repository",
    "reset_dependency_caches",
]
