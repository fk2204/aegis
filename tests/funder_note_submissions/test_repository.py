"""InMemoryFunderNoteSubmissionRepository tests.

Covers the contract that both the in-memory and Supabase backends must
honour:
  * ``create`` returns a row with ``status='pending'``, ``submitted_at``
    populated, and ``responded_at`` None.
  * ``list_for_merchant`` newest-first; empty list for unknown merchant.
  * ``update_status`` ``pending -> approved`` populates offer terms and
    stamps ``responded_at``.
  * ``update_status`` accepts any valid status (no transition graph).
  * ``update_status`` raises ``FunderNoteSubmissionNotFoundError`` on
    unknown ``submission_id``.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.funder_note_submissions.repository import (
    FunderNoteSubmissionNotFoundError,
    InMemoryFunderNoteSubmissionRepository,
)


def test_create_returns_pending_row_with_submitted_at_and_no_responded_at() -> None:
    repo = InMemoryFunderNoteSubmissionRepository()
    merchant_id = uuid4()
    funder_id = uuid4()
    row = repo.create(
        merchant_id=merchant_id,
        funder_id=funder_id,
        funder_note="Submitted to Quick Capital for review.",
        submitted_by="filip@commerafunding.com",
    )
    assert row.status == "pending"
    assert row.submitted_at is not None
    assert row.responded_at is None
    assert row.merchant_id == merchant_id
    assert row.funder_id == funder_id
    assert row.submitted_by == "filip@commerafunding.com"
    assert row.created_at is not None
    assert row.updated_at is not None


def test_list_for_merchant_newest_first() -> None:
    repo = InMemoryFunderNoteSubmissionRepository()
    merchant_id = uuid4()
    first = repo.create(
        merchant_id=merchant_id,
        funder_id=uuid4(),
        funder_note="first",
        submitted_by="op@commerafunding.com",
    )
    second = repo.create(
        merchant_id=merchant_id,
        funder_id=uuid4(),
        funder_note="second",
        submitted_by="op@commerafunding.com",
    )
    third = repo.create(
        merchant_id=merchant_id,
        funder_id=uuid4(),
        funder_note="third",
        submitted_by="op@commerafunding.com",
    )

    listed = repo.list_for_merchant(merchant_id)
    assert [r.id for r in listed] == [third.id, second.id, first.id]


def test_list_for_merchant_returns_empty_list_for_unseen_merchant() -> None:
    repo = InMemoryFunderNoteSubmissionRepository()
    repo.create(
        merchant_id=uuid4(),
        funder_id=uuid4(),
        funder_note="unrelated",
        submitted_by="op@commerafunding.com",
    )
    assert repo.list_for_merchant(uuid4()) == []


def test_list_for_merchant_honours_limit() -> None:
    repo = InMemoryFunderNoteSubmissionRepository()
    merchant_id = uuid4()
    for _ in range(5):
        repo.create(
            merchant_id=merchant_id,
            funder_id=uuid4(),
            funder_note="x",
            submitted_by="op@commerafunding.com",
        )
    assert len(repo.list_for_merchant(merchant_id, limit=3)) == 3


def test_update_status_pending_to_approved_populates_offer_and_responded_at() -> None:
    repo = InMemoryFunderNoteSubmissionRepository()
    row = repo.create(
        merchant_id=uuid4(),
        funder_id=uuid4(),
        funder_note="awaiting",
        submitted_by="op@commerafunding.com",
    )
    updated = repo.update_status(
        row.id,
        status="approved",
        offer_amount=Decimal("75000.00"),
        offer_factor=Decimal("1.3500"),
        offer_holdback=Decimal("0.1500"),
        notes="Funder approved 75k @ 1.35 / 15%.",
    )
    assert updated.status == "approved"
    assert updated.offer_amount == Decimal("75000.00")
    assert updated.offer_factor == Decimal("1.3500")
    assert updated.offer_holdback == Decimal("0.1500")
    assert updated.notes == "Funder approved 75k @ 1.35 / 15%."
    assert updated.responded_at is not None


@pytest.mark.parametrize("status", ["approved", "declined", "countered"])
def test_update_status_accepts_any_non_pending_status(status: str) -> None:
    repo = InMemoryFunderNoteSubmissionRepository()
    row = repo.create(
        merchant_id=uuid4(),
        funder_id=uuid4(),
        funder_note="awaiting",
        submitted_by="op@commerafunding.com",
    )
    # mypy: status is constrained to the Literal at the call site; the
    # parametrize values match the Literal members exactly.
    updated = repo.update_status(row.id, status=status)  # type: ignore[arg-type]
    assert updated.status == status
    assert updated.responded_at is not None


def test_update_status_does_not_overwrite_responded_at_on_correction() -> None:
    repo = InMemoryFunderNoteSubmissionRepository()
    row = repo.create(
        merchant_id=uuid4(),
        funder_id=uuid4(),
        funder_note="awaiting",
        submitted_by="op@commerafunding.com",
    )
    first_response = repo.update_status(row.id, status="declined")
    assert first_response.responded_at is not None
    original_responded_at = first_response.responded_at

    # Operator corrects the typo (declined -> countered). The original
    # response timestamp must remain so the dossier history reading
    # stays accurate.
    corrected = repo.update_status(row.id, status="countered")
    assert corrected.status == "countered"
    assert corrected.responded_at == original_responded_at


def test_update_status_raises_for_unknown_submission_id() -> None:
    repo = InMemoryFunderNoteSubmissionRepository()
    with pytest.raises(FunderNoteSubmissionNotFoundError):
        repo.update_status(uuid4(), status="approved")
