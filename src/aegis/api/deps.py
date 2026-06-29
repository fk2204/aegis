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
from typing import TYPE_CHECKING

from aegis.audit import AuditLog, InMemoryAuditLog, SupabaseAuditLog
from aegis.bank_layouts import (
    BankLayoutRepository,
    InMemoryBankLayoutRepository,
    SupabaseBankLayoutRepository,
)
from aegis.close.client import CloseClient
from aegis.compliance.override_outcome_links import (
    InMemoryOverrideOutcomeLinkRepository,
    OverrideOutcomeLinkRepository,
    SupabaseOverrideOutcomeLinkRepository,
)
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
from aegis.funder_note_submissions import (
    FunderNoteSubmissionRepository,
    InMemoryFunderNoteSubmissionRepository,
    SupabaseFunderNoteSubmissionRepository,
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
from aegis.ops.deal_assignment_repository import (
    DealAssignmentRepository,
    InMemoryDealAssignmentRepository,
    SupabaseDealAssignmentRepository,
)
from aegis.ops.llm_cost_repository import (
    InMemoryLLMCostRepository,
    LLMCostRepository,
    SupabaseLLMCostRepository,
)
from aegis.ops.notification_repository import (
    InMemoryNotificationRepository,
    NotificationRepository,
    SupabaseNotificationRepository,
)
from aegis.ops.operator_repository import (
    InMemoryOperatorRepository,
    OperatorRepository,
    SupabaseOperatorRepository,
)
from aegis.ops.webhook_circuit import (
    InMemoryCircuitBackend,
    WebhookCircuit,
    build_default_circuit,
)
from aegis.parser.processor.repository import (
    InMemoryProcessorStatementRepository,
    ProcessorStatementRepository,
    SupabaseProcessorStatementRepository,
)
from aegis.pdf_store import (
    InMemoryPdfStoreRepository,
    PdfStoreRepository,
    SupabasePdfStoreRepository,
)
from aegis.probe_review import (
    InMemoryProbeReviewRepository,
    ProbeReviewRepository,
    SupabaseProbeReviewRepository,
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

# ``SchemaMigrationsReader`` (U32) lives in ``aegis.web.routers.admin``
# alongside its sole consumer route. The factory below imports it
# lazily to avoid the circular dependency that a top-level import would
# create (admin router imports from this deps module).
if TYPE_CHECKING:
    from aegis.web.routers.admin import SchemaMigrationsReader


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
def get_override_outcome_link_repository() -> OverrideOutcomeLinkRepository:
    """Process-wide OverrideOutcomeLinkRepository — junction store that
    links operator overrides to recorded deal outcomes for the flywheel
    summary."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryOverrideOutcomeLinkRepository()
    return SupabaseOverrideOutcomeLinkRepository()


@lru_cache(maxsize=1)
def get_renewal_attestation_repository() -> RenewalAttestationRepository:
    """Process-wide RenewalAttestationRepository (U6 — migration 040).

    Same memory / supabase toggle as the merchant repository.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryRenewalAttestationRepository()
    return SupabaseRenewalAttestationRepository()


@lru_cache(maxsize=1)
def get_processor_statement_repository() -> ProcessorStatementRepository:
    """Process-wide ProcessorStatementRepository (migration 073).

    Persists Stripe / Square / Toast / Clover / PayPal aggregates that
    the worker's ``_run_processor_branch`` writes after a successful
    parse. The dossier ``processor_section`` builder reads here to
    populate ``processor_revenue`` without re-parsing. Same memory /
    supabase toggle as the other repositories.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryProcessorStatementRepository()
    return SupabaseProcessorStatementRepository()


@lru_cache(maxsize=1)
def get_pdf_store_repository() -> PdfStoreRepository:
    """Process-wide PdfStoreRepository (migration 060 — chunk B + C).

    Backs the in-Postgres ciphertext blob store the 2026-06-15 operator
    directive substitutes for the Supabase Storage chunk-B path. The
    worker writes here after a successful parse; the view route
    ``GET /ui/merchants/{merchant_id}/documents/{document_id}/pdf``
    reads here to stream the original PDF back to the operator.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryPdfStoreRepository()
    return SupabasePdfStoreRepository()


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
def get_llm_cost_repository() -> LLMCostRepository:
    """Process-wide LLMCostRepository (migration 078).

    Powers the dual-write inside ``CostTrackingBedrockClient`` so every
    Bedrock call lands a row in ``llm_costs`` alongside the existing
    ``audit_log`` bedrock.usage row.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryLLMCostRepository()
    return SupabaseLLMCostRepository()


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
def get_funder_note_submission_repository() -> FunderNoteSubmissionRepository:
    """Process-wide FunderNoteSubmissionRepository (migration 057).

    Persists one row per ``POST /ui/merchants/{id}/submit-to-funder``
    click — the dossier history block reads from this durable table
    instead of parsing audit_log JSON. Same memory / supabase toggle as
    the other repositories.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryFunderNoteSubmissionRepository()
    return SupabaseFunderNoteSubmissionRepository()


@lru_cache(maxsize=1)
def get_bank_layout_repository() -> BankLayoutRepository:
    """Process-wide BankLayoutRepository (migration 059).

    Persists per-bank layout fingerprints + operator-authored
    ``extraction_hints`` that the parser pipeline injects into the
    Bedrock extraction system prompt on subsequent parses of the same
    bank. Same memory / supabase toggle as the other repositories.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryBankLayoutRepository()
    return SupabaseBankLayoutRepository()


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


@lru_cache(maxsize=1)
def get_operator_repository() -> OperatorRepository:
    """Process-wide OperatorRepository (migration 022 / 076).

    Backs the role-based permission gate, the dossier assignment chip,
    and the notifications fan-out. Same memory / supabase toggle as the
    other repositories.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryOperatorRepository()
    return SupabaseOperatorRepository()


@lru_cache(maxsize=1)
def get_notification_repository() -> NotificationRepository:
    """Process-wide NotificationRepository (migration 077).

    Backs the bell-icon dropdown + the event emitters in
    ``aegis.web._notify``. Same memory / supabase toggle as the other
    repositories.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryNotificationRepository()
    return SupabaseNotificationRepository()


@lru_cache(maxsize=1)
def get_deal_assignment_repository() -> DealAssignmentRepository:
    """Process-wide DealAssignmentRepository (migration 076).

    Powers the per-merchant assignment chip on the dossier, the
    "My deals" filter on Today + the merchant list, and the Assignee
    column. Same memory / supabase toggle as the other repositories.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryDealAssignmentRepository()
    return SupabaseDealAssignmentRepository()


@lru_cache(maxsize=1)
def get_schema_migrations_reader() -> SchemaMigrationsReader:
    """Process-wide SchemaMigrationsReader (U32 — operator visibility).

    Powers the ``/ui/admin/applied-migrations`` page. Memory backend →
    empty in-memory reader (tests pin a deterministic row list via
    ``app.dependency_overrides``); supabase → service-role-keyed reader
    that bypasses the RLS migration 030 turned on for the table.

    Imported lazily so the top-level import chain stays
    ``deps -> admin router`` (admin router imports from deps, so the
    reverse cannot live at module scope).
    """
    from aegis.web.routers.admin import (
        InMemorySchemaMigrationsReader,
        SupabaseSchemaMigrationsReader,
    )

    if get_settings().aegis_storage_backend == "memory":
        return InMemorySchemaMigrationsReader()
    return SupabaseSchemaMigrationsReader()


@lru_cache(maxsize=1)
def get_probe_review_repository() -> ProbeReviewRepository:
    """Process-wide ProbeReviewRepository (item 3.8 — migration 091).

    Persists operator verdicts on shadow-probe disagreements. Powers
    the ``/ui/admin/text-layer-probe-review`` operator validation UI:
    every ``[SHADOW] text_layer_probe_v2_disagrees`` flag on a
    document needs a human verdict before the probe can flip from
    shadow to live.

    Same memory / supabase toggle as the other repositories.
    """
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryProbeReviewRepository()
    return SupabaseProbeReviewRepository()


@lru_cache(maxsize=1)
def get_webhook_circuit() -> WebhookCircuit:
    """Process-wide Close webhook circuit breaker.

    Memory backend in tests / dev; Redis-backed in production. The Redis
    URL comes from ``Settings.redis_url`` (same source the arq worker
    uses), so the breaker shares the box's existing Redis without
    needing a second connection pool.
    """
    if get_settings().aegis_storage_backend == "memory":
        return WebhookCircuit(InMemoryCircuitBackend())
    return build_default_circuit()


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
    get_funder_note_submission_repository.cache_clear()
    get_bank_layout_repository.cache_clear()
    get_pdf_store_repository.cache_clear()
    get_processor_statement_repository.cache_clear()
    get_operator_repository.cache_clear()
    get_deal_assignment_repository.cache_clear()
    get_notification_repository.cache_clear()
    get_schema_migrations_reader.cache_clear()
    get_probe_review_repository.cache_clear()
    get_llm.cache_clear()
    get_llm_cost_repository.cache_clear()
    get_ofac_client.cache_clear()
    get_close_client.cache_clear()
    get_webhook_circuit.cache_clear()


__all__ = [
    "get_audit",
    "get_bank_layout_repository",
    "get_close_client",
    "get_deal_assignment_repository",
    "get_deal_repository",
    "get_decision_snapshot",
    "get_disclosure_render_event_repository",
    "get_funder_note_submission_repository",
    "get_funder_reply_repository",
    "get_funder_repository",
    "get_llm",
    "get_llm_cost_repository",
    "get_merchant_repository",
    "get_merchant_shadow_signal_repository",
    "get_notification_repository",
    "get_ofac_client",
    "get_operator_repository",
    "get_override_repository",
    "get_pdf_store_repository",
    "get_probe_review_repository",
    "get_processor_statement_repository",
    "get_renewal_attestation_repository",
    "get_repository",
    "get_schema_migrations_reader",
    "get_scoring_disagreement_repository",
    "get_submission_repository",
    "get_webhook_circuit",
    "reset_dependency_caches",
]
