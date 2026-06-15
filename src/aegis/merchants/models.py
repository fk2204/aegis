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
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money

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

    @property
    def is_provisional(self) -> bool:
        return self.status == "provisional"

    @property
    def needs_manual_naming(self) -> bool:
        return self.status == "needs_manual_naming"

    @property
    def is_finalized(self) -> bool:
        return self.status == "finalized"


__all__ = ["EntityType", "IndustryRiskTier", "MerchantRow", "MerchantStatus"]
