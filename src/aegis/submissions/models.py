"""SubmissionRow — durable record of a per-funder CSV submission.

Mirrors ``migrations/013_submissions_table.sql``. Pydantic-strict so a
Supabase column drift trips at parse time rather than corrupting a
P&L roll-up downstream.

Status lifecycle:

    submitted --> funder_declined
              --> funder_approved --> funded
              --> withdrawn

``valid_status_transition`` is the single source of truth used by both
the in-memory repo and (future) Supabase repo.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis.money import Money

SubmissionStatus = Literal[
    "submitted",
    "funder_declined",
    "funder_approved",
    "funded",
    "withdrawn",
]


# Allowed forward transitions. Anything not in this map is rejected.
# Note: terminal states (funded, funder_declined, withdrawn) have no
# outgoing edges — once funded, edits to ``funded_amount`` etc. land via
# a separate ``record_funded_correction`` repo call (Phase 7C scope).
_TRANSITIONS: dict[SubmissionStatus, frozenset[SubmissionStatus]] = {
    "submitted": frozenset(
        {"funder_declined", "funder_approved", "withdrawn"}
    ),
    "funder_approved": frozenset({"funded", "withdrawn"}),
    "funder_declined": frozenset(),
    "funded": frozenset(),
    "withdrawn": frozenset(),
}


def valid_status_transition(
    current: SubmissionStatus, target: SubmissionStatus
) -> bool:
    """Return True if ``current -> target`` is an allowed transition.

    A self-transition (``current == target``) is always False: callers
    should not "update" status to its current value; use a different
    method for noting the same status with a new note.
    """
    return target in _TRANSITIONS.get(current, frozenset())


class SubmissionStatusTransitionError(ValueError):
    """Raised when ``transition_status`` would violate the lifecycle."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


# Pydantic types matching the numeric(6,4) columns. ``factor`` and
# ``holdback`` are not money but rate scalars; reuse the same Decimal
# discipline so float never sneaks in.
_FactorRate = Annotated[
    Decimal,
    Field(max_digits=6, decimal_places=4, gt=Decimal("1"), le=Decimal("2")),
]
_HoldbackRate = Annotated[
    Decimal,
    Field(max_digits=6, decimal_places=4, ge=Decimal("0"), le=Decimal("1")),
]


class SubmissionRow(_StrictModel):
    """One submission of one deal to one funder.

    Natural key: ``(merchant_id, document_id, funder_id)`` — see
    ``uq_submissions_deal_funder`` in migration 013.

    ``csv_doc_hash`` records the sha256 hex of the exact bytes that went
    out, parallel to ``disclosure_transmission_log.disclosure_doc_hash``.
    Lets a regulator question "what did you send?" be answered from this
    row without disk recovery.
    """

    id: UUID = Field(default_factory=uuid4)
    merchant_id: UUID
    document_id: UUID
    funder_id: UUID

    # Submission act
    submitted_at: datetime
    submitted_by: str = Field(min_length=1)
    csv_doc_hash: str = Field(min_length=64, max_length=64)
    csv_filename: str = Field(min_length=1)

    # Proposed terms snapshot
    proposed_amount: Money
    proposed_factor: _FactorRate
    proposed_holdback: _HoldbackRate

    # Lifecycle
    status: SubmissionStatus = "submitted"
    funder_response_at: datetime | None = None
    funder_response_note: str | None = None

    # Funded leg (all-or-nothing — see model_validator below)
    funded_amount: Money | None = None
    factor_rate: _FactorRate | None = None
    funded_at: datetime | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _enforce_funded_fields_together(self) -> SubmissionRow:
        """All three funded_* fields are either all set or none — and only
        when status='funded'. Matches the ``submissions_funded_fields_together``
        check constraint in migration 013.
        """
        has_amount = self.funded_amount is not None
        has_rate = self.factor_rate is not None
        has_at = self.funded_at is not None
        any_set = has_amount or has_rate or has_at
        all_set = has_amount and has_rate and has_at

        if any_set and not all_set:
            raise ValueError(
                "funded_amount, factor_rate, and funded_at must be set together"
            )
        if all_set and self.status != "funded":
            raise ValueError(
                "funded_* fields are only valid when status='funded'"
            )
        return self


__all__ = [
    "SubmissionRow",
    "SubmissionStatus",
    "SubmissionStatusTransitionError",
    "valid_status_transition",
]
