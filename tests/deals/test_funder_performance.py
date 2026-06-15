"""Tests for ``aegis.deals.funder_performance.compute_funder_performance``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.deals.funder_performance import compute_funder_performance
from aegis.funder_note_submissions.models import (
    FunderNoteSubmissionRow,
    FunderNoteSubmissionStatus,
)

_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _sub(
    *,
    funder_id: UUID,
    merchant_id: UUID | None = None,
    status: FunderNoteSubmissionStatus = "pending",
    submitted_at: datetime | None = None,
    responded_at: datetime | None = None,
    offer_amount: Decimal | None = None,
    notes: str | None = None,
) -> FunderNoteSubmissionRow:
    submitted = submitted_at or _NOW
    return FunderNoteSubmissionRow(
        merchant_id=merchant_id or uuid4(),
        funder_id=funder_id,
        submitted_at=submitted,
        submitted_by="dashboard",
        status=status,
        funder_note="x",
        offer_amount=offer_amount,
        responded_at=responded_at,
        notes=notes,
        created_at=submitted,
        updated_at=submitted,
    )


def test_volume_and_status_counts() -> None:
    f = uuid4()
    subs = [
        _sub(funder_id=f, status="approved"),
        _sub(funder_id=f, status="approved"),
        _sub(funder_id=f, status="declined"),
        _sub(funder_id=f, status="countered"),
        _sub(funder_id=f, status="pending"),
    ]
    out = compute_funder_performance(
        funder_id=f,
        submissions=subs,
        suggested_amount_by_merchant={},
    )
    assert out.total_submissions == 5
    assert out.approved == 2
    assert out.declined == 1
    assert out.countered == 1
    assert out.pending == 1


def test_approval_rate_decided_denominator_only() -> None:
    """Approval rate denominator is approved+declined+countered.
    Pending submissions don't dilute the rate."""
    f = uuid4()
    subs = [
        _sub(funder_id=f, status="approved"),
        _sub(funder_id=f, status="declined"),
        _sub(funder_id=f, status="pending"),
    ]
    out = compute_funder_performance(funder_id=f, submissions=subs, suggested_amount_by_merchant={})
    assert out.approval_rate_pct == Decimal("50.0")


def test_approval_rate_none_when_nothing_decided() -> None:
    """All-pending funder surfaces None for approval_rate so UI
    renders em-dash, not 0%."""
    f = uuid4()
    out = compute_funder_performance(
        funder_id=f,
        submissions=[_sub(funder_id=f, status="pending") for _ in range(3)],
        suggested_amount_by_merchant={},
    )
    assert out.approval_rate_pct is None


def test_avg_days_to_response_averages_responded_subs() -> None:
    """Mean of (responded_at - submitted_at).days across decided
    submissions. Pending rows don't contribute."""
    f = uuid4()
    submitted_at = _NOW - timedelta(days=10)
    subs = [
        _sub(
            funder_id=f,
            status="approved",
            submitted_at=submitted_at,
            responded_at=submitted_at + timedelta(days=2),
        ),
        _sub(
            funder_id=f,
            status="declined",
            submitted_at=submitted_at,
            responded_at=submitted_at + timedelta(days=4),
        ),
        _sub(funder_id=f, status="pending"),
    ]
    out = compute_funder_performance(funder_id=f, submissions=subs, suggested_amount_by_merchant={})
    # (2 + 4) / 2 = 3.0
    assert out.avg_days_to_response == Decimal("3.0")


def test_avg_offer_ratio_computes_against_suggested_amount() -> None:
    """For approved submissions where offer_amount AND suggested are
    known, take the mean ratio. A funder offering 90% of AEGIS's
    suggested produces 90.0%."""
    f = uuid4()
    m1 = uuid4()
    m2 = uuid4()
    subs = [
        _sub(
            funder_id=f,
            merchant_id=m1,
            status="approved",
            offer_amount=Decimal("90000.00"),
        ),
        _sub(
            funder_id=f,
            merchant_id=m2,
            status="approved",
            offer_amount=Decimal("50000.00"),
        ),
    ]
    out = compute_funder_performance(
        funder_id=f,
        submissions=subs,
        suggested_amount_by_merchant={
            m1: Decimal("100000.00"),
            m2: Decimal("100000.00"),
        },
    )
    # 90 + 50 / 2 = 70.0%
    assert out.avg_offer_ratio_pct == Decimal("70.0")
    assert out.offer_ratio_sample_size == 2


def test_offer_ratio_none_when_no_samples() -> None:
    f = uuid4()
    out = compute_funder_performance(
        funder_id=f,
        submissions=[_sub(funder_id=f, status="pending")],
        suggested_amount_by_merchant={},
    )
    assert out.avg_offer_ratio_pct is None
    assert out.offer_ratio_sample_size == 0


def test_recent_decline_notes_collected_newest_first() -> None:
    """Decline notes surface newest-first, truncated to 10 entries."""
    f = uuid4()
    base = _NOW - timedelta(days=20)
    subs = []
    for i in range(15):
        subs.append(
            _sub(
                funder_id=f,
                status="declined",
                submitted_at=base + timedelta(days=i),
                responded_at=base + timedelta(days=i, hours=4),
                notes=f"decline reason {i}",
            )
        )
    out = compute_funder_performance(funder_id=f, submissions=subs, suggested_amount_by_merchant={})
    # Cap at 10, newest first
    assert len(out.recent_decline_notes) == 10
    assert out.recent_decline_notes[0].note == "decline reason 14"
    assert out.recent_decline_notes[9].note == "decline reason 5"


def test_decline_notes_excludes_blank_notes_and_non_declined() -> None:
    """Approved/countered submissions don't contribute to decline
    notes even with notes attached. Blank/whitespace notes on a
    declined submission are filtered out — operator wants substance,
    not empty rows."""
    f = uuid4()
    subs = [
        _sub(funder_id=f, status="approved", notes="ignore: not declined"),
        _sub(funder_id=f, status="declined", notes="   "),
        _sub(funder_id=f, status="declined", notes="real concern"),
    ]
    out = compute_funder_performance(funder_id=f, submissions=subs, suggested_amount_by_merchant={})
    notes = [n.note for n in out.recent_decline_notes]
    assert notes == ["real concern"]


def test_negative_response_delta_clips_to_zero() -> None:
    """A responded_at before submitted_at (clock skew from a re-edit)
    clips to 0 days rather than dragging the average negative."""
    f = uuid4()
    submitted_at = _NOW
    subs = [
        _sub(
            funder_id=f,
            status="approved",
            submitted_at=submitted_at,
            responded_at=submitted_at - timedelta(hours=2),
        ),
    ]
    out = compute_funder_performance(funder_id=f, submissions=subs, suggested_amount_by_merchant={})
    assert out.avg_days_to_response == Decimal("0.0")
