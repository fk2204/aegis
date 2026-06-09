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
from aegis.close.client import CloseClient
from aegis.compliance.overrides import (
    InMemoryOverrideRepository,
    OverrideRepository,
    SupabaseOverrideRepository,
)
from aegis.compliance.render_events import (
    DisclosureRenderEventRepository,
    InMemoryDisclosureRenderEventRepository,
    SupabaseDisclosureRenderEventRepository,
)
from aegis.compliance.snapshot import (
    DecisionSnapshot,
    InMemoryDecisionSnapshot,
    SupabaseDecisionSnapshot,
)
from aegis.config import get_settings
from aegis.deals.repository import (
    DealRepository,
    InMemoryDealRepository,
    SupabaseDealRepository,
)
from aegis.funders.replies import (
    FunderReplyRepository,
    InMemoryFunderReplyRepository,
    SupabaseFunderReplyRepository,
)
from aegis.funders.repository import (
    FunderRepository,
    InMemoryFunderRepository,
    SupabaseFunderRepository,
)
from aegis.llm import BedrockClient, LLMClient
from aegis.merchants.renewal_attestations import (
    InMemoryRenewalAttestationRepository,
    RenewalAttestationRepository,
    SupabaseRenewalAttestationRepository,
)
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    MerchantRepository,
    SupabaseMerchantRepository,
)
from aegis.merchants.shadow_signals import (
    InMemoryMerchantShadowSignalRepository,
    MerchantShadowSignalRepository,
    SupabaseMerchantShadowSignalRepository,
)
from aegis.scoring.ofac import OFACClient
from aegis.scoring_v2.shadow_disagreements import (
    InMemoryScoringDisagreementRepository,
    ScoringDisagreementRepository,
    SupabaseScoringDisagreementRepository,
)
from aegis.storage import (
    DocumentRepository,
    InMemoryDocumentRepository,
    SupabaseDocumentRepository,
)
from aegis.submissions import (
    InMemorySubmissionRepository,
    SubmissionRepository,
    SupabaseSubmissionRepository,
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
def get_renewal_attestation_repository() -> RenewalAttestationRepository:
    """Process-wide RenewalAttestationRepository (U6 — migration 040).

    Same memory / supabase toggle as the merchant repository.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryRenewalAttestationRepository()
    return SupabaseRenewalAttestationRepository()


@lru_cache(maxsize=1)
def get_merchant_shadow_signal_repository() -> MerchantShadowSignalRepository:
    """Process-wide MerchantShadowSignalRepository (U22 — migration 044).

    Persists the cross-statement Pattern list that U15 stashes on
    ``PipelineResult.cross_statement_patterns``. Same memory / supabase
    toggle as the other repositories.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryMerchantShadowSignalRepository()
    return SupabaseMerchantShadowSignalRepository()


@lru_cache(maxsize=1)
def get_funder_repository() -> FunderRepository:
    """Process-wide FunderRepository. Same backend toggle as documents."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryFunderRepository()
    return SupabaseFunderRepository()


@lru_cache(maxsize=1)
def get_deal_repository() -> DealRepository:
    """Process-wide DealRepository (deals are a merchants x documents projection).

    In-memory backend composes from the in-memory merchant + document
    repos so a deal added via either flows through immediately. Supabase
    backend issues nested-select joins against the documents table.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryDealRepository(
            merchants=get_merchant_repository(),
            documents=get_repository(),
        )
    return SupabaseDealRepository()


@lru_cache(maxsize=1)
def get_llm() -> LLMClient:
    """Production LLM client. Tests override with a fake via dependency_overrides."""
    return BedrockClient()


@lru_cache(maxsize=1)
def get_disclosure_render_event_repository() -> DisclosureRenderEventRepository:
    """Process-wide DisclosureRenderEventRepository (U16 — migration 042).

    Same memory / supabase toggle as the other repositories. The route
    layer in ``api/routes/disclosures.py`` uses this to persist the
    render-event status (``ok`` / ``needs_review`` / ``apr_compute_failed``)
    that U3 deferred.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryDisclosureRenderEventRepository()
    return SupabaseDisclosureRenderEventRepository()


@lru_cache(maxsize=1)
def get_override_repository() -> OverrideRepository:
    """Process-wide OverrideRepository (mp Phase 10). Same memory /
    supabase toggle as the other repositories."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryOverrideRepository()
    return SupabaseOverrideRepository()


@lru_cache(maxsize=1)
def get_funder_reply_repository() -> FunderReplyRepository:
    """Process-wide FunderReplyRepository (mp Phase 10). Same memory /
    supabase toggle as the other repositories."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryFunderReplyRepository()
    return SupabaseFunderReplyRepository()


@lru_cache(maxsize=1)
def get_submission_repository() -> SubmissionRepository:
    """Process-wide SubmissionRepository (U20 — migration 013).

    Same memory / supabase toggle as the other repositories. The
    dashboard ``merchant_submit_to_funders`` handler uses this to persist
    one row per matched funder so the portfolio funder-approval panel
    reads from a durable table instead of audit_log JSON.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemorySubmissionRepository()
    return SupabaseSubmissionRepository()


@lru_cache(maxsize=1)
def get_scoring_disagreement_repository() -> ScoringDisagreementRepository:
    """Process-wide ScoringDisagreementRepository (U4 — migration 037+038).

    Drives the U24 ``/ui/triage`` aggregate KPI tile for the scoring
    shadow-comparison backlog. Same memory / supabase toggle as the
    other repositories. The U4 triage CLI continues to own the write
    paths; this factory is for the read-only dashboard surface.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryScoringDisagreementRepository()
    return SupabaseScoringDisagreementRepository()


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


@lru_cache(maxsize=1)
def get_close_client() -> CloseClient:
    """Process-wide CloseClient. Audit log is injected so 429 rate-limit
    hits land in audit_log. Tests override via dependency_overrides to
    supply a MockTransport-backed client."""
    return CloseClient(audit=get_audit())


def reset_dependency_caches() -> None:
    """Drop the lru_cache singletons. For tests that swap settings."""
    get_repository.cache_clear()
    get_merchant_repository.cache_clear()
    get_renewal_attestation_repository.cache_clear()
    get_merchant_shadow_signal_repository.cache_clear()
    get_funder_repository.cache_clear()
    get_deal_repository.cache_clear()
    get_funder_reply_repository.cache_clear()
    get_override_repository.cache_clear()
    get_audit.cache_clear()
    get_decision_snapshot.cache_clear()
    get_disclosure_render_event_repository.cache_clear()
    get_scoring_disagreement_repository.cache_clear()
    get_submission_repository.cache_clear()
    get_llm.cache_clear()
    get_ofac_client.cache_clear()
    get_close_client.cache_clear()


__all__ = [
    "get_audit",
    "get_close_client",
    "get_deal_repository",
    "get_decision_snapshot",
    "get_disclosure_render_event_repository",
    "get_funder_reply_repository",
    "get_funder_repository",
    "get_llm",
    "get_merchant_repository",
    "get_merchant_shadow_signal_repository",
    "get_ofac_client",
    "get_override_repository",
    "get_renewal_attestation_repository",
    "get_repository",
    "get_scoring_disagreement_repository",
    "get_submission_repository",
    "reset_dependency_caches",
]
