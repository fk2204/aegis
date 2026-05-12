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
    """

    id: UUID = Field(default_factory=uuid4)
    business_name: str = Field(min_length=1)
    dba: str | None = None
    owner_name: str = Field(min_length=1)
    state: str = Field(min_length=2, max_length=2, description="USPS state code")
    industry_naics: str | None = None
    industry_risk_tier: IndustryRiskTier | None = None
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

    # Operator-curated funder pick after reviewing matches. The UI button
    # to set this is deferred (Phase 7 audit decision); column lives here
    # so the future button is a no-migration patch.
    preferred_funder_id: UUID | None = None

    # Idempotency for Zoho sync.
    # zoho_lead_id is populated after the first push_merchant_to_lead call.
    zoho_deal_id: str | None = None
    zoho_lead_id: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["EntityType", "IndustryRiskTier", "MerchantRow"]
