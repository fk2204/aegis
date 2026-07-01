"""Stips module — Pydantic model + common-stips templates.

The templates are the operator-approved dropdown options on the
"add stip" form. Adding to the list is one-shot editing here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

StipType = Literal["document", "verification", "condition", "signature"]
StipStatus = Literal["outstanding", "received", "waived", "expired"]


class StipRow(BaseModel):
    """One stipulation on a merchant deal — the Pydantic mirror of the
    migration-104 row shape.

    ``description`` is operator-supplied and can be free-form; the
    UI's "add stip" flow pre-fills it from ``STIP_TEMPLATES`` but
    operators can type any string.

    ``funder_id`` is optional — some stips are AEGIS-owned (voided
    check, ID, bank statements) and apply regardless of which funder
    the deal goes to; others are funder-specific.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        frozen=False,
        str_strip_whitespace=True,
    )

    id: UUID
    merchant_id: UUID
    funder_id: UUID | None = None
    stip_type: StipType
    description: str = Field(min_length=1, max_length=500)
    status: StipStatus = "outstanding"
    due_date: date | None = None
    received_at: datetime | None = None
    waived_reason: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime
    created_by: UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)


# Operator-approved "common stips" templates surfaced as the add-form
# dropdown. Adding an entry requires only editing this list — nothing
# else consumes it. Ordered by frequency of use (voided check + bank
# statements land on almost every deal; equipment invoice is niche).
STIP_TEMPLATES: Final[list[tuple[StipType, str]]] = [
    ("document", "Voided check"),
    ("document", "3 months most recent bank statements"),
    ("signature", "Signed ISO agreement"),
    ("signature", "Signed merchant application"),
    ("document", "Tax returns (most recent 2 years)"),
    ("document", "Business license"),
    ("document", "Payroll records (3 months)"),
    ("document", "Accounts receivable aging report"),
    ("document", "Landlord letter / lease agreement"),
    ("document", "Equipment invoice or quote"),
    ("document", "Driver's license copy"),
    ("verification", "SOS good standing check"),
    ("verification", "OFAC clearance"),
    ("verification", "UCC filings search"),
    ("condition", "Confession of Judgment (COJ)"),
    ("condition", "Personal guarantee"),
]


__all__ = [
    "STIP_TEMPLATES",
    "StipRow",
    "StipStatus",
    "StipType",
]
