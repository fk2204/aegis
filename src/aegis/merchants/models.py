"""Merchant data shape.

Mirrors the ``merchants`` Postgres table (migration 000 + 008). Pydantic-
strict: unknown fields are rejected at parse time so a Supabase schema
drift can't silently land on a Python field name that doesn't exist.

PII fields (``business_name``, ``owner_name``, ``email``, ``phone``,
``ein``) must NEVER be logged in plaintext; the project logger masks
them by name + value pattern.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money
from aegis.product_types import DEFAULT_PRODUCT_TYPE, ProductType

IndustryRiskTier = Literal["low", "moderate", "elevated", "high", "avoid"]
EntityType = Literal["llc", "corp", "sole_prop", "partnership", "other"]
MerchantStatus = Literal["provisional", "needs_manual_naming", "finalized"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class MerchantRow(_StrictModel):
    """One business AEGIS underwrites.

    The 2-letter state code is the routing key for compliance — every
    deal coming from this merchant inherits its state's regulation tier
    in the disclosure render path.

    Lifecycle (migration 034 — merchant-from-statement flow):
      * ``status='provisional'``         — auto-created at dashboard
        upload time, awaiting parse-completion. ``business_name`` is
        a placeholder string (e.g. ``"(awaiting parse)"``);
        ``owner_name`` and ``state`` are ``None``.
      * ``status='needs_manual_naming'`` — parse done but couldn't
        auto-name (blank ``account_holder``, parse exception, parse
        cancellation, processor-branch success). Placeholder
        ``business_name`` survives; operator names via intake.
      * ``status='finalized'``           — has a real
        ``business_name`` from the statement OR operator-curated.
        Default for every existing row and every operator-curated
        merchant. ``owner_name`` and ``state`` may still be ``None``
        on auto-finalized rows; operator sets them via existing edit.

    ``business_name`` is intentionally NOT nullable in the type system
    even though provisional rows have only a placeholder. Reasons:
      * Production reads (slugify, dossier render, sort keys) all read
        business_name and would each need a None-guard if it were
        nullable — wide type cascade for no behavior benefit.
      * The OFAC and scoring code paths (the ones where a placeholder
        would be most dangerous) are guarded by ``status='finalized'``
        at the consumer site, so a placeholder never reaches them.
      * The DB CHECK ``merchants_finalized_has_business_name`` is
        trivially satisfied — business_name is never NULL — but kept
        as belt-and-suspenders for SQL-only operations.
    """

    id: UUID = Field(default_factory=uuid4)
    status: MerchantStatus = "finalized"
    business_name: str = Field(min_length=1)
    dba: str | None = None
    # Migration 080 — Commera lending product this merchant is being
    # underwritten for. Defaults to ``revenue_based`` because that is
    # truthfully what Commera offered exclusively before migration 080
    # (per AEGIS operating-principle 4 — no fabricated defaults). Every
    # legacy merchant row carries this value via the migration-080
    # ``DEFAULT``; new merchants land with the Close-side product_type
    # when the operator has populated it, else default.
    product_type: ProductType = DEFAULT_PRODUCT_TYPE
    owner_name: str | None = Field(default=None, min_length=1)
    state: str | None = Field(
        default=None, min_length=2, max_length=2, description="USPS state code"
    )
    industry_naics: str | None = None
    industry_risk_tier: IndustryRiskTier | None = None
    # Lead-side Close ``Industry`` choice string (em-dash form),
    # captured at webhook upsert time and persisted alongside the
    # derived ``industry_naics``. Drives the
    # ``aegis.scoring_v2.industry`` tier lookup. ``None`` for
    # legacy merchants pre-migration 055 or merchants Close hasn't
    # populated yet.
    industry_choice: str | None = None
    time_in_business_months: Annotated[int, Field(ge=0)] | None = None
    credit_score: Annotated[int, Field(ge=300, le=850)] | None = None

    # Contact (PII). Stored as plain str to avoid the email-validator
    # dep; format validation belongs at the API boundary.
    email: str | None = None
    phone: str | None = None

    # Intake fields (migration 008). All optional — operator may know any
    # subset of these at create time. EIN is PII and is masked by the
    # logger and excluded from CSV/JSON exports unless explicitly opted
    # into via a future include_pii flag.
    entity_type: EntityType | None = None
    ein: str | None = Field(default=None, max_length=32)
    requested_amount: Money | None = None
    requested_factor: Decimal | None = Field(
        default=None, gt=Decimal("0"), description="e.g. 1.30 for 30% margin"
    )
    requested_term_days: Annotated[int, Field(gt=0)] | None = None
    broker_source: str | None = None
    intake_date: date | None = None
    is_renewal: bool = False

    # Renewal maturity date (migration 039). Operator-populated per-deal at
    # renewal-onboarding time. Drives the upcoming-renewals calendar
    # (``list_upcoming_renewals`` in ``aegis.merchants.repository``); never
    # used to gate a broker-side enforcement action — funder partners own
    # regulator-facing renewal disclosures (see CLAUDE.md mission statement).
    maturity_date: date | None = None

    # Document-on-file flags (migration 061 — Feature 2). Capture the
    # operator's confirmation that each conditionally-required document
    # has been collected. Read by the document-completeness checker
    # (``aegis.merchants.document_completeness.check_completeness``) at
    # submit-to-funder time against the top matched funder's
    # ``conditional_requirements`` string list. Defaults match the DB
    # DEFAULTs so a row that pre-dates migration 061 reads safely as
    # "operator hasn't checked the box yet" rather than silently
    # passing the gate.
    voided_check_on_file: bool = False
    drivers_license_on_file: bool = False
    bank_statements_months: Annotated[int, Field(ge=0)] = 0

    # Operator-curated funder pick after reviewing matches. The UI button
    # to set this is deferred (Phase 7 audit decision); column lives here
    # so the future button is a no-migration patch.
    preferred_funder_id: UUID | None = None

    # Operator-curated free-form notes. The dossier textarea POSTs to
    # /ui/merchants/{id}/notes which prepends a timestamped line to this
    # value (append-only via the UI). NULL = no notes ever entered;
    # ``""`` would mean "operator cleared the notes" (not currently
    # reachable via the UI but the column allows it for SQL-direct
    # edits). Migration 058.
    notes: str | None = None

    # Feature D — merchant context fields (migration 064). Four free-text
    # columns injected into the Bedrock extraction prompt as the
    # "MERCHANT CONTEXT" block so the LLM can disambiguate ambiguous
    # statement layouts with what we already know about the deal.
    #
    #   * ``deal_context``           — operator-written. Editable
    #     textarea on the dossier Context panel. Posted via
    #     ``POST /ui/merchants/{id}/deal-context``.
    #   * ``close_lead_description`` — Close Lead ``description`` field,
    #     auto-refreshed on every Close webhook for this lead AND on
    #     operator "Refresh Close fields" click.
    #   * ``close_notes_summary``    — concatenated bodies of the most
    #     recent 5 Close Note activities for the lead. Auto-refreshed.
    #   * ``close_call_transcripts`` — concatenated note text of the
    #     most recent 3 Close Call activities. Auto-refreshed.
    #
    # The three Close-derived fields are PII-bearing (note bodies and
    # call transcripts often quote transaction descriptions, name
    # owners, etc.). Acceptable in the database per CLAUDE.md — never
    # logged (logger masks the column names) and never echoed into
    # audit-row ``details`` (refresh audits store counts only).
    deal_context: str | None = None
    close_lead_description: str | None = None
    close_notes_summary: str | None = None
    close_call_transcripts: str | None = None

    # ------------------------------------------------------------------
    # Web-presence reputation scan (migration 067). Populated by
    # ``aegis.web_presence.scanner.scan_web_presence`` on first score,
    # or refreshed by the operator via the dossier "Refresh" button.
    # Soft signal only — risk_flags surface as
    # ``FunderMatch.soft_concerns`` entries; never gate a match.
    # ``web_presence_scanned_at is None`` is the "needs first scan"
    # signal the scorer checks before invoking the scanner.
    # ------------------------------------------------------------------
    web_presence_summary: str | None = None
    web_presence_flags: list[str] = Field(default_factory=list)
    web_presence_scanned_at: datetime | None = None

    # ------------------------------------------------------------------
    # UCC filings + previous-default search (migration 068). Populated
    # by ``aegis.business_intel.ucc_checker.check_ucc_and_defaults``
    # on first score, or refreshed via the dossier ``Refresh`` button.
    # Soft signal only - both lists surface as
    # ``FunderMatch.soft_concerns`` strings; never gate a match.
    # ``ucc_checked_at is None`` is the "needs first check" signal
    # the scorer checks before invoking the checker.
    # ------------------------------------------------------------------
    ucc_filings: list[str] = Field(default_factory=list)
    ucc_default_indicators: list[str] = Field(default_factory=list)
    ucc_checked_at: datetime | None = None

    # ------------------------------------------------------------------
    # OFAC SDN + Consolidated sanctions screening (migration 083).
    # Populated by ``aegis.compliance.ofac.ensure_ofac_check`` on first
    # score, or refreshed via the dossier ``Refresh`` button. HARD GATE:
    # ``ofac_is_clear=False`` suppresses the funder-matching grid and
    # renders a red banner. ``ofac_checked_at is None`` is the
    # "needs first check" signal.
    # ------------------------------------------------------------------
    ofac_checked_at: datetime | None = None
    ofac_is_clear: bool | None = None
    ofac_match_detail: list[str] = Field(default_factory=list)
    ofac_cache_date: datetime | None = None

    # ------------------------------------------------------------------
    # Federal bankruptcy check (migration 084). Populated by
    # ``aegis.business_intel.bankruptcy_checker.check_bankruptcy``
    # via CourtListener v4 REST. ``bankruptcy_checked_at IS NULL`` is
    # the "needs first check" signal ``ensure_bankruptcy_check``
    # reads. ``bankruptcy_active`` AND ``bankruptcy_chapter == "7"``
    # is a hard gate at the dossier; Ch.11 is amber, Ch.13 yellow,
    # discharged/recent surfaces as informational only. The
    # ``bankruptcy_cases`` JSONB carries per-docket detail for the
    # dossier drill-down.
    # ------------------------------------------------------------------
    bankruptcy_checked_at: datetime | None = None
    bankruptcy_active: bool | None = None
    bankruptcy_recent: bool | None = None
    bankruptcy_chapter: str | None = None
    bankruptcy_cases: list[dict[str, Any]] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Migration 087 — application data parsed from the Close Lead
    # ``description`` FINANCIAL block (operator-typed at intake). Drives
    # the dossier "Application data from Close" panel (what the merchant
    # told us) AND two existing scoring detectors that compare stated
    # against bank-measured values:
    #
    #   * ``monthly_revenue``      — fed to
    #     ``detect_stated_vs_measured_revenue_divergence`` (severity 60).
    #     Named ``monthly_revenue`` (NOT ``stated_monthly_revenue``)
    #     because the detector hard-codes this attribute name; renaming
    #     would silently disable the detector.
    #   * ``stated_daily_payment`` — fed to
    #     ``detect_impossible_payment_load`` (severity 85). Same naming
    #     hard-dep — the detector reads this attribute name verbatim.
    #
    # Every other field surfaces only on the dossier today; the *stated*
    # vs *measured* drift checks (positions, balance, bank name) are
    # follow-on detectors. The fields are populated NOW so the dossier
    # surface lights up immediately and the detector hooks are ready.
    #
    # All fields default to ``None`` / empty list. The detectors
    # short-circuit when the field is ``None`` or non-positive, so a
    # legacy merchant (pre-087) reads as "merchant didn't tell us" and
    # never trips a divergence check on absence of stated data.
    # ------------------------------------------------------------------
    monthly_revenue: Money | None = None
    avg_monthly_cc_sales: Money | None = None
    stated_monthly_deposits: Annotated[int, Field(ge=0)] | None = None
    stated_mca_positions: Annotated[int, Field(ge=0)] | None = None
    stated_current_lenders: list[str] = Field(default_factory=list)
    stated_mca_balance: Money | None = None
    stated_daily_payment: Money | None = None
    stated_bank: str | None = None
    use_of_funds: str | None = None

    # ------------------------------------------------------------------
    # Migration 089 — Close Lead description-parsed staging blob.
    # Populated by ``aegis.close.description_extractor.extract_from_description``
    # when the structured FINANCIAL-block parser returns an empty dict.
    # The dossier surfaces this as an editable preview card; the
    # operator confirms (promotes to ``stated_*`` columns) or discards.
    # Scoring NEVER reads this column — only confirmed ``stated_*`` data
    # drives decisions, per CLAUDE.md's extraction-assists-not-replaces
    # rule. Shape documented in migration 089 + the
    # ``ExtractedFieldsPayload`` model.
    # ------------------------------------------------------------------
    stated_extracted_pending: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Migration 086 — enhanced UCC flow. ``ucc_portal_url`` is the
    # state SOS / UCC search URL the dossier surfaces for one-click
    # operator verification; populated from ``UCC_STATE_PORTALS`` at
    # write time based on ``state``. ``ucc_operator_verified`` flips
    # to True after the operator confirms findings via the dossier
    # button; ``ucc_verified_at`` captures the click timestamp.
    # ------------------------------------------------------------------
    ucc_portal_url: str | None = None
    ucc_operator_verified: bool = False
    ucc_verified_at: datetime | None = None

    # ------------------------------------------------------------------
    # Secretary of State entity check (migration 085, Phase C). Populated
    # by ``aegis.business_intel.sos_checker.SOSChecker`` — local SQLite
    # cache first, Bedrock fallback for uncovered states. 30-day TTL via
    # ``ensure_sos_check``. ``sos_checked_at is None`` is the "needs
    # first check" signal; all other fields land per the registry's
    # response shape (entity_name may differ from business_name; status
    # tokens are state-specific; is_active is the operator-facing
    # boolean derived from status).
    # ------------------------------------------------------------------
    sos_checked_at: datetime | None = None
    sos_status: str | None = None
    sos_entity_name: str | None = None
    sos_formation_date: str | None = None
    sos_is_active: bool | None = None
    sos_data_source: str | None = None
    sos_state_checked: str | None = None

    # Phase 7B funder-submission tracking (Pydantic-only — no Supabase
    # column yet, so these reset on a Supabase round-trip; audit_log is
    # the durable record. Persistence moves to a real submissions table
    # in Phase 7C.).
    submitted_to_funder_ids: list[UUID] = Field(default_factory=list)
    last_submitted_at: datetime | None = None

    # Idempotency for Close CRM sync. Populated when the Close inbound
    # webhook (/webhooks/close) upserts a merchant for an Opportunity
    # transitioning to "Docs In — Pre-UW".
    #
    # The legacy zoho_deal_id / zoho_lead_id columns still exist on the
    # DB as zoho_deal_id_archived / zoho_lead_id_archived (renamed by
    # migration 026 to preserve historical linkage). No AEGIS code reads
    # them; the operator queries SQL directly when an audit needs to
    # cross-reference a Zoho-era deal.
    close_lead_id: str | None = None

    # Captured from the Close webhook event's ``object_id`` field when
    # the Opportunity transitions to "Docs In — Pre-UW" — the same
    # trigger that owns the merchant upsert. Drives the outbound
    # offer-sync (``aegis.close.sync.push_offer_to_opportunity``) to
    # the right Opportunity row so the Suggested Max Advance /
    # Recommended Factor Rate / etc. custom fields land where the
    # underwriter is looking. Migration 054.
    close_opportunity_id: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Migration 065 — operator-initiated soft-delete. NULL = active
    # (the only state every existing pre-065 row carries). NOT NULL =
    # the operator clicked Delete on the dossier; row is hidden from
    # every operator-visible read (``MerchantRepository.get`` /
    # ``list_all`` / ``find_by_*`` / ``count_total``) but underlying
    # documents / transactions / analyses / decisions / audit rows are
    # preserved forever. Set only by
    # ``MerchantRepository.soft_delete``; never edited via the
    # generic edit form.
    deleted_at: datetime | None = None

    @property
    def is_provisional(self) -> bool:
        return self.status == "provisional"

    @property
    def needs_manual_naming(self) -> bool:
        return self.status == "needs_manual_naming"

    @property
    def is_finalized(self) -> bool:
        return self.status == "finalized"


# ---------------------------------------------------------------------------
# Merchant operator notes — migration 066.
#
# Feature C — operator notes panel redesign (2026-06-18). Replaces the
# single-text-column append-only ``merchants.notes`` (migration 058) with a
# normalized one-row-per-note table. Each row is a single timestamped note
# card on the dossier; the panel renders newest-first.
#
# PII: ``body`` is operator-curated free text. Loggers MUST mask the field;
# the paired audit row carries only the length, never the body bytes.
# ---------------------------------------------------------------------------


MERCHANT_NOTE_MAX_CHARS: int = 4000


class MerchantNoteRow(_StrictModel):
    """One operator note about a merchant.

    Append-only via the dossier route — the UI cannot edit a row's
    ``body`` after insert. Display contract: ``list_notes`` returns
    newest-first; the dossier template renders each row as a card with
    the ``created_at`` timestamp + ``actor`` label.
    """

    id: UUID = Field(default_factory=uuid4)
    merchant_id: UUID
    body: str = Field(min_length=1, max_length=MERCHANT_NOTE_MAX_CHARS)
    actor: str = Field(min_length=1)
    created_at: datetime | None = None


__all__ = [
    "MERCHANT_NOTE_MAX_CHARS",
    "EntityType",
    "IndustryRiskTier",
    "MerchantNoteRow",
    "MerchantRow",
    "MerchantStatus",
]
