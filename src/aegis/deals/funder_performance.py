"""Per-funder performance roll-up for /ui/funders/{id}/performance.

Pure functions. The route loads every submission against the funder,
plus a ``suggested_amount_by_merchant`` map sourced from the most
recent decision per merchant, and hands the lists to
:func:`compute_funder_performance`. Returns a Pydantic-strict view
model the template renders directly.

Five metrics:

* ``total_submissions``        — every submission the funder ever
                                 received.
* ``approval_rate_pct``        — approved / decided * 100 (decided =
                                 not pending). ``None`` when nothing
                                 has been decided yet so the UI
                                 surfaces em-dash, not 0%.
* ``avg_days_to_response``     — mean of ``(responded_at -
                                 submitted_at).days`` across decided
                                 submissions.
* ``avg_offer_ratio_pct``      — mean of ``(offer_amount /
                                 suggested_amount)`` for approved
                                 submissions where both values are
                                 known. Surfaces operator's "are
                                 we leaving money on the table"
                                 question (>100% = funder offered
                                 more than AEGIS suggested; <100% =
                                 less).
* ``recent_decline_notes``     — the last 10 notes attached to
                                 ``declined`` submissions, newest
                                 first, with submitted_at timestamps.
                                 The operator scans these for
                                 patterns (specific concerns the
                                 funder kept flagging).

Money math via Decimal. ``None``-on-empty-denominator is the rule
across every percent / mean field.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.funder_note_submissions.models import FunderNoteSubmissionRow

_RECENT_DECLINE_NOTE_LIMIT: Final[int] = 10


class DeclineNote(BaseModel):
    """One row of the recent-decline-notes table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    submitted_at: datetime
    responded_at: datetime | None
    merchant_id: UUID
    note: str


class FunderPerformance(BaseModel):
    """The full per-funder performance view-model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    funder_id: UUID
    total_submissions: int
    approved: int
    declined: int
    countered: int
    pending: int
    approval_rate_pct: Decimal | None = Field(default=None)
    avg_days_to_response: Decimal | None = Field(default=None)
    avg_offer_ratio_pct: Decimal | None = Field(default=None)
    offer_ratio_sample_size: int = Field(
        default=0,
        description=(
            "Number of approved submissions that contributed to "
            "avg_offer_ratio_pct. Surfaced so the operator can judge "
            "whether the ratio is signal or noise."
        ),
    )
    recent_decline_notes: tuple[DeclineNote, ...]


def compute_funder_performance(
    *,
    funder_id: UUID,
    submissions: list[FunderNoteSubmissionRow],
    suggested_amount_by_merchant: dict[UUID, Decimal],
) -> FunderPerformance:
    """Roll the per-funder metrics from already-loaded data.

    ``submissions`` is every row keyed to ``funder_id`` (caller's
    job to filter). ``suggested_amount_by_merchant`` maps merchant
    UUID to the AEGIS-suggested-max-advance from the merchant's
    most recent decision; merchants without a decision are absent.

    The function is deterministic and has no I/O.
    """
    total = approved = declined = countered = 0
    response_days_samples: list[int] = []
    offer_ratio_samples: list[Decimal] = []
    decline_notes: list[DeclineNote] = []

    for s in submissions:
        total += 1
        if s.status == "approved":
            approved += 1
        elif s.status == "declined":
            declined += 1
        elif s.status == "countered":
            countered += 1
        if s.responded_at is not None:
            delta = (s.responded_at - s.submitted_at).days
            # Negative deltas (clock skew on a re-edit) clip to 0 so
            # the average isn't dragged below zero. The underlying
            # data is correct; only the displayed mean is bounded.
            response_days_samples.append(max(0, delta))
        if s.status == "approved" and s.offer_amount is not None:
            suggested = suggested_amount_by_merchant.get(s.merchant_id)
            if suggested is not None and suggested > 0:
                offer_ratio_samples.append(
                    (s.offer_amount / suggested * Decimal("100")).quantize(Decimal("0.1"))
                )
        if s.status == "declined" and s.notes and s.notes.strip():
            decline_notes.append(
                DeclineNote(
                    submitted_at=s.submitted_at,
                    responded_at=s.responded_at,
                    merchant_id=s.merchant_id,
                    note=s.notes.strip(),
                )
            )

    decided = approved + declined + countered
    approval_rate = (
        (Decimal(approved) / Decimal(decided) * Decimal("100")).quantize(Decimal("0.1"))
        if decided > 0
        else None
    )

    avg_days = (
        (Decimal(sum(response_days_samples)) / Decimal(len(response_days_samples))).quantize(
            Decimal("0.1")
        )
        if response_days_samples
        else None
    )

    avg_offer_ratio = (
        (sum(offer_ratio_samples) / Decimal(len(offer_ratio_samples))).quantize(Decimal("0.1"))
        if offer_ratio_samples
        else None
    )

    # Newest-first then cap at the limit so the operator sees the
    # most-recent reasons rather than the historical tail.
    decline_notes.sort(key=lambda n: n.submitted_at, reverse=True)
    recent_decline_notes = tuple(decline_notes[:_RECENT_DECLINE_NOTE_LIMIT])

    return FunderPerformance(
        funder_id=funder_id,
        total_submissions=total,
        approved=approved,
        declined=declined,
        countered=countered,
        pending=total - decided,
        approval_rate_pct=approval_rate,
        avg_days_to_response=avg_days,
        avg_offer_ratio_pct=avg_offer_ratio,
        offer_ratio_sample_size=len(offer_ratio_samples),
        recent_decline_notes=recent_decline_notes,
    )


__all__ = [
    "DeclineNote",
    "FunderPerformance",
    "compute_funder_performance",
]
