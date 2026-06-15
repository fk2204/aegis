"""FunderNoteSubmissionRow — durable record of one Close-Note submit click.

Mirrors ``migrations/057_funder_note_submissions.sql``. Pydantic-strict so a
Supabase column drift trips at parse time rather than corrupting the dossier
history block downstream.

Distinct from ``aegis.submissions`` (migration 013): that table captures the
CSV-bundle paper-trail (one row per matched funder per bundle, with
``document_id`` + ``csv_doc_hash``). This module captures the simpler
"Submit to Funder" button click that posts ONE plain-text Note to the Close
Lead activity feed — one row per click, framed against the top matched funder.

Status lifecycle:

    pending --> approved
            --> declined
            --> countered

Once a non-pending status is set the repository stamps ``responded_at`` in the
same write. The lifecycle is forward-only: ``approved``, ``declined`` and
``countered`` are all terminal from the row's perspective. A re-submission to
the same funder lands as a new row (no natural-key uniqueness on this table —
each click is its own audit record).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money

FunderNoteSubmissionStatus = Literal[
    "pending",
    "approved",
    "declined",
    "countered",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


# numeric(6,4) shapes matching ``submissions.proposed_factor`` /
# ``submissions.proposed_holdback`` — keeps rate math Decimal end-to-end.
_FactorRate = Annotated[
    Decimal,
    Field(max_digits=6, decimal_places=4, gt=Decimal("1"), le=Decimal("2")),
]
_HoldbackRate = Annotated[
    Decimal,
    Field(max_digits=6, decimal_places=4, ge=Decimal("0"), le=Decimal("1")),
]


class FunderNoteSubmissionRow(_StrictModel):
    """One Close-Note submission of one merchant to one funder.

    No natural-key uniqueness: a re-submission to the same funder is a
    new row so the dossier history block can show the full timeline of
    attempts and responses.
    """

    id: UUID = Field(default_factory=uuid4)
    merchant_id: UUID
    funder_id: UUID

    submitted_at: datetime
    # ``submitted_by`` is added against the operator spec for parity with
    # ``submissions.submitted_by`` — actor identity in-line avoids a join
    # to ``audit_log.actor_email`` on every dossier-history render.
    submitted_by: str = Field(min_length=1)

    status: FunderNoteSubmissionStatus = "pending"

    offer_amount: Money | None = None
    offer_factor: _FactorRate | None = None
    offer_holdback: _HoldbackRate | None = None

    funder_note: str | None = None
    responded_at: datetime | None = None
    notes: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = [
    "FunderNoteSubmissionRow",
    "FunderNoteSubmissionStatus",
]
