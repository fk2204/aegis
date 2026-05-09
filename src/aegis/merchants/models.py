"""Merchant data shape.

Mirrors the ``merchants`` Postgres table (migration 000). Pydantic-strict:
unknown fields are rejected at parse time so a Supabase schema drift can't
silently land on a Python field name that doesn't exist.

PII fields (``business_name``, ``owner_name``, ``email``, ``phone``)
must NEVER be logged in plaintext; the project logger masks them by name
+ value pattern.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

IndustryRiskTier = Literal["low", "moderate", "elevated", "high", "avoid"]


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

    # Idempotency for Zoho sync
    zoho_deal_id: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["IndustryRiskTier", "MerchantRow"]
