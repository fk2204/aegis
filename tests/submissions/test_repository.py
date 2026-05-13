"""InMemorySubmissionRepository tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.submissions.models import (
    SubmissionRow,
    SubmissionStatusTransitionError,
)
from aegis.submissions.repository import (
    InMemorySubmissionRepository,
    SubmissionConflictError,
    SubmissionNotFoundError,
)


def _make_submission(**overrides: object) -> SubmissionRow:
    base: dict[str, object] = {
        "merchant_id": uuid4(),
        "document_id": uuid4(),
        "funder_id": uuid4(),
        "submitted_at": datetime(2026, 5, 13, tzinfo=UTC),
        "submitted_by": "filip@commerafunding.com",
        "csv_doc_hash": "a" * 64,
        "csv_filename": "acme__quick-capital.csv",
        "proposed_amount": Decimal("50000.00"),
        "proposed_factor": Decimal("1.3000"),
        "proposed_holdback": Decimal("0.1200"),
    }
    base.update(overrides)
    return SubmissionRow(**base)


def test_create_and_get() -> None:
    repo = InMemorySubmissionRepository()
    sub = _make_submission()
    created = repo.create(sub)
    assert created.created_at is not None
    assert repo.get(created.id) == created


def test_get_raises_for_missing() -> None:
    repo = InMemorySubmissionRepository()
    with pytest.raises(SubmissionNotFoundError):
        repo.get(uuid4())


def test_create_enforces_natural_key_uniqueness() -> None:
    repo = InMemorySubmissionRepository()
    merchant_id, document_id, funder_id = uuid4(), uuid4(), uuid4()
    sub1 = _make_submission(
        merchant_id=merchant_id, document_id=document_id, funder_id=funder_id
    )
    repo.create(sub1)
    sub2 = _make_submission(
        merchant_id=merchant_id, document_id=document_id, funder_id=funder_id
    )
    with pytest.raises(SubmissionConflictError):
        repo.create(sub2)


def test_find_by_deal_and_funder() -> None:
    repo = InMemorySubmissionRepository()
    m, d, f = uuid4(), uuid4(), uuid4()
    created = repo.create(
        _make_submission(merchant_id=m, document_id=d, funder_id=f)
    )
    assert repo.find_by_deal_and_funder(
        merchant_id=m, document_id=d, funder_id=f
    ) == created
    assert repo.find_by_deal_and_funder(
        merchant_id=m, document_id=d, funder_id=uuid4()
    ) is None


def test_list_for_merchant_and_funder() -> None:
    repo = InMemorySubmissionRepository()
    m = uuid4()
    f = uuid4()
    repo.create(_make_submission(merchant_id=m, funder_id=f))
    repo.create(_make_submission(merchant_id=m, funder_id=uuid4()))
    repo.create(_make_submission(merchant_id=uuid4(), funder_id=f))

    by_merchant = repo.list_for_merchant(m)
    assert len(by_merchant) == 2
    assert all(r.merchant_id == m for r in by_merchant)

    by_funder = repo.list_for_funder(f)
    assert len(by_funder) == 2
    assert all(r.funder_id == f for r in by_funder)


def test_transition_submitted_to_funder_approved() -> None:
    repo = InMemorySubmissionRepository()
    sub = repo.create(_make_submission())
    after = repo.transition_status(
        sub.id, target="funder_approved", funder_response_note="LGTM"
    )
    assert after.status == "funder_approved"
    assert after.funder_response_at is not None
    assert after.funder_response_note == "LGTM"


def test_transition_to_funded_requires_funded_terms() -> None:
    repo = InMemorySubmissionRepository()
    sub = repo.create(_make_submission())
    repo.transition_status(sub.id, target="funder_approved")
    with pytest.raises(SubmissionStatusTransitionError, match="funded_amount"):
        repo.transition_status(sub.id, target="funded")


def test_funded_happy_path_through_repo() -> None:
    repo = InMemorySubmissionRepository()
    sub = repo.create(_make_submission())
    repo.transition_status(sub.id, target="funder_approved")
    funded = repo.transition_status(
        sub.id,
        target="funded",
        funded_amount=Decimal("48000.00"),
        factor_rate=Decimal("1.3000"),
    )
    assert funded.status == "funded"
    assert funded.funded_amount == Decimal("48000.00")
    assert funded.factor_rate == Decimal("1.3000")
    assert funded.funded_at is not None


def test_invalid_transition_rejected() -> None:
    repo = InMemorySubmissionRepository()
    sub = repo.create(_make_submission())
    # Skipping approval — submitted -> funded directly should fail.
    with pytest.raises(SubmissionStatusTransitionError):
        repo.transition_status(
            sub.id,
            target="funded",
            funded_amount=Decimal("48000.00"),
            factor_rate=Decimal("1.3000"),
        )


def test_no_transition_out_of_terminal_state() -> None:
    repo = InMemorySubmissionRepository()
    sub = repo.create(_make_submission())
    repo.transition_status(sub.id, target="funder_declined")
    with pytest.raises(SubmissionStatusTransitionError):
        repo.transition_status(sub.id, target="funder_approved")
