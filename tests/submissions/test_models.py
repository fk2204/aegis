"""SubmissionRow round-trip + lifecycle validation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegis.submissions.models import (
    SubmissionRow,
    valid_status_transition,
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


def test_submission_row_roundtrip() -> None:
    sub = _make_submission()
    data = sub.model_dump(mode="json")
    rebuilt = SubmissionRow.model_validate(data)
    assert rebuilt == sub


def test_csv_doc_hash_must_be_64_chars() -> None:
    with pytest.raises(ValidationError):
        _make_submission(csv_doc_hash="too-short")


def test_proposed_factor_must_be_between_1_and_2() -> None:
    with pytest.raises(ValidationError):
        _make_submission(proposed_factor=Decimal("0.95"))
    with pytest.raises(ValidationError):
        _make_submission(proposed_factor=Decimal("2.1"))


def test_proposed_holdback_must_be_0_to_1() -> None:
    with pytest.raises(ValidationError):
        _make_submission(proposed_holdback=Decimal("1.50"))


def test_funded_fields_all_or_nothing() -> None:
    """funded_amount alone (without factor_rate + funded_at) is invalid."""
    with pytest.raises(ValidationError, match="set together"):
        _make_submission(
            funded_amount=Decimal("48000.00"),
            status="funded",
            # factor_rate + funded_at missing
        )


def test_funded_fields_only_valid_when_status_funded() -> None:
    """Cannot stash funded_* fields while status is still 'submitted'."""
    with pytest.raises(ValidationError, match="status='funded'"):
        _make_submission(
            funded_amount=Decimal("48000.00"),
            factor_rate=Decimal("1.3000"),
            funded_at=datetime(2026, 5, 20, tzinfo=UTC),
            status="submitted",
        )


def test_funded_happy_path() -> None:
    """All three funded_* set + status='funded' validates."""
    sub = _make_submission(
        funded_amount=Decimal("48000.00"),
        factor_rate=Decimal("1.3000"),
        funded_at=datetime(2026, 5, 20, tzinfo=UTC),
        status="funded",
    )
    assert sub.status == "funded"
    assert sub.funded_amount == Decimal("48000.00")
    assert sub.factor_rate == Decimal("1.3000")


def test_status_transitions_from_submitted() -> None:
    assert valid_status_transition("submitted", "funder_declined") is True
    assert valid_status_transition("submitted", "funder_approved") is True
    assert valid_status_transition("submitted", "withdrawn") is True
    # Skipping the approval step is rejected: must go submitted -> approved -> funded.
    assert valid_status_transition("submitted", "funded") is False


def test_status_transitions_from_funder_approved() -> None:
    assert valid_status_transition("funder_approved", "funded") is True
    assert valid_status_transition("funder_approved", "withdrawn") is True
    # Cannot un-approve.
    assert valid_status_transition("funder_approved", "funder_declined") is False
    assert valid_status_transition("funder_approved", "submitted") is False


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for terminal in ("funder_declined", "funded", "withdrawn"):
        for target in (
            "submitted",
            "funder_declined",
            "funder_approved",
            "funded",
            "withdrawn",
        ):
            assert valid_status_transition(terminal, target) is False


def test_self_transition_not_allowed() -> None:
    """Updating status to its current value is a no-op call — reject it."""
    assert valid_status_transition("submitted", "submitted") is False
    assert valid_status_transition("funder_approved", "funder_approved") is False
