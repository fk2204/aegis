"""U20 — SubmissionRepository contract + record_submission helper tests.

Covers:

  * Round-trip through ``InMemorySubmissionRepository.create`` /
    ``.get`` / ``.find_by_deal_and_funder``.
  * ``list_for_merchant`` filter narrows to the named merchant.
  * ``list_in_window`` date filter narrows to the inclusive
    ``[from_date, to_date]`` window.
  * ``record_submission`` writes one durable row AND one
    ``deal.submission_persisted`` audit row pointing at the
    submission id.
  * PII canary: the audit-row ``details`` payload contains only id
    references + the file digest — never CSV bytes / merchant name /
    business identifiers.
  * Decimal discipline: ``record_submission`` rejects float for
    ``proposed_amount`` / ``proposed_factor`` / ``proposed_holdback``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.submissions import (
    InMemorySubmissionRepository,
    SubmissionConflictError,
    SubmissionRow,
    record_submission,
)


def _make_submission(
    **overrides: object,
) -> SubmissionRow:
    base: dict[str, object] = {
        "merchant_id": uuid4(),
        "document_id": uuid4(),
        "funder_id": uuid4(),
        "submitted_at": datetime(2026, 5, 15, tzinfo=UTC),
        "submitted_by": "filip@commerafunding.com",
        "csv_doc_hash": "b" * 64,
        "csv_filename": "acme__quick-capital.csv",
        "proposed_amount": Decimal("50000.00"),
        "proposed_factor": Decimal("1.3000"),
        "proposed_holdback": Decimal("0.1200"),
    }
    base.update(overrides)
    return SubmissionRow(**base)


def test_round_trip_through_repository() -> None:
    """A SubmissionRow goes in via create() and comes back out via get()
    with created_at / updated_at stamped — and the natural-key lookup
    finds the same row."""
    repo = InMemorySubmissionRepository()
    sub = _make_submission()
    created = repo.create(sub)
    assert created.created_at is not None
    assert created.updated_at is not None
    assert repo.get(created.id) == created

    by_key = repo.find_by_deal_and_funder(
        merchant_id=sub.merchant_id,
        document_id=sub.document_id,
        funder_id=sub.funder_id,
    )
    assert by_key == created


def test_list_for_merchant_filters_to_named_merchant() -> None:
    """list_for_merchant returns only rows whose merchant_id matches.

    Mirrors the production query pattern — the dashboard's
    per-merchant deal panel lists only that merchant's submissions.
    """
    repo = InMemorySubmissionRepository()
    target = uuid4()
    other = uuid4()
    repo.create(_make_submission(merchant_id=target))
    repo.create(_make_submission(merchant_id=target))
    repo.create(_make_submission(merchant_id=other))

    rows = repo.list_for_merchant(target)
    assert len(rows) == 2
    assert all(r.merchant_id == target for r in rows)


def test_list_in_window_date_filter_is_inclusive() -> None:
    """list_in_window narrows to ``[from_date, to_date]`` inclusive."""
    repo = InMemorySubmissionRepository()
    # Three rows: one before the window, one inside, one after.
    repo.create(
        _make_submission(submitted_at=datetime(2026, 4, 25, tzinfo=UTC))
    )
    inside = repo.create(
        _make_submission(submitted_at=datetime(2026, 5, 15, tzinfo=UTC))
    )
    repo.create(
        _make_submission(submitted_at=datetime(2026, 6, 5, tzinfo=UTC))
    )

    rows = repo.list_in_window(
        from_date=date(2026, 5, 1),
        to_date=date(2026, 5, 31),
    )
    assert [r.id for r in rows] == [inside.id]


def test_list_in_window_inclusive_on_boundary_dates() -> None:
    """A row submitted exactly on ``from_date`` (midnight UTC) is
    inside the window. A row submitted exactly on ``to_date`` (any
    hour) is also inside — the supabase impl widens to 23:59:59.
    """
    repo = InMemorySubmissionRepository()
    repo.create(
        _make_submission(submitted_at=datetime(2026, 5, 1, tzinfo=UTC))
    )
    repo.create(
        _make_submission(submitted_at=datetime(2026, 5, 31, 23, 59, tzinfo=UTC))
    )
    rows = repo.list_in_window(
        from_date=date(2026, 5, 1),
        to_date=date(2026, 5, 31),
    )
    assert len(rows) == 2


def test_list_in_window_rejects_reversed_range() -> None:
    """to_date earlier than from_date raises rather than silently
    returning an empty list. The route handler catches and 400s; an
    empty result would mask the operator's input mistake."""
    repo = InMemorySubmissionRepository()
    with pytest.raises(ValueError, match="earlier than from_date"):
        repo.list_in_window(
            from_date=date(2026, 5, 31),
            to_date=date(2026, 5, 1),
        )


def test_record_submission_writes_audit_row_with_submission_id() -> None:
    """record_submission writes a durable row AND a
    ``deal.submission_persisted`` audit row carrying the submission id.
    The audit row's subject_type is 'merchant' and subject_id is the
    merchant_id (matches the convention used by the existing
    ``deal.submit_to_funders`` row)."""
    repo = InMemorySubmissionRepository()
    audit = InMemoryAuditLog()

    merchant_id = uuid4()
    document_id = uuid4()
    funder_id = uuid4()
    csv_bytes = b"funder_csv_payload"

    persisted = record_submission(
        repo,
        audit,
        merchant_id=merchant_id,
        document_id=document_id,
        funder_id=funder_id,
        submitted_by="filip@commerafunding.com",
        csv_bytes=csv_bytes,
        csv_filename="acme__quick-capital.csv",
        proposed_amount=Decimal("50000.00"),
        proposed_factor=Decimal("1.3000"),
        proposed_holdback=Decimal("0.1200"),
        actor_email="filip@commerafunding.com",
    )

    # Durable row present.
    assert repo.get(persisted.id) == persisted

    # Audit row pointing at the submission.
    assert len(audit.entries) == 1
    row = audit.entries[0]
    assert row["action"] == "deal.submission_persisted"
    assert row["subject_type"] == "merchant"
    assert row["subject_id"] == str(merchant_id)
    details = row["details"]
    assert details["submission_id"] == str(persisted.id)
    assert details["document_id"] == str(document_id)
    assert details["funder_id"] == str(funder_id)
    # csv_doc_hash is the sha256 of csv_bytes — verify it matches.
    import hashlib

    assert details["csv_doc_hash"] == hashlib.sha256(csv_bytes).hexdigest()


def test_record_submission_audit_payload_pii_canary() -> None:
    """PII canary — the audit-row ``details`` payload must contain ONLY
    id references + the file digest + the filename. No CSV bytes, no
    merchant business name, no operator email outside the actor_email
    column, no transaction text.

    Mirrors the discipline ``record_disclosure_transmission`` follows:
    audit metadata is for cross-reference, never a covert PII store.
    """
    repo = InMemorySubmissionRepository()
    audit = InMemoryAuditLog()
    csv_bytes = (
        b"ACME PAINTING LLC,Jane Doe,1234567890,jane@acme.com,42 Main St"
    )

    record_submission(
        repo,
        audit,
        merchant_id=uuid4(),
        document_id=uuid4(),
        funder_id=uuid4(),
        submitted_by="filip@commerafunding.com",
        csv_bytes=csv_bytes,
        csv_filename="acme__quick-capital.csv",
        proposed_amount=Decimal("50000.00"),
        proposed_factor=Decimal("1.3000"),
        proposed_holdback=Decimal("0.1200"),
    )

    assert len(audit.entries) == 1
    details = audit.entries[0]["details"]
    # Allowed keys, exhaustive set.
    assert set(details.keys()) == {
        "submission_id",
        "document_id",
        "funder_id",
        "csv_doc_hash",
        "csv_filename",
    }
    # Spot-check: CSV bytes / names / emails are NOT in any value.
    serialized = repr(details)
    assert "ACME PAINTING" not in serialized
    assert "Jane Doe" not in serialized
    assert "1234567890" not in serialized
    assert "jane@acme.com" not in serialized
    assert "42 Main St" not in serialized


def test_record_submission_rejects_float_money() -> None:
    """Float for proposed_amount / proposed_factor / proposed_holdback
    raises TypeError before the row is persisted. Money math is
    Decimal-only per CLAUDE.md."""
    repo = InMemorySubmissionRepository()
    audit = InMemoryAuditLog()
    # mypy: explicit Any so the **common unpack accepts the float-arg
    # negative-test branches below without spurious arg-type errors.
    common: dict[str, Any] = {
        "merchant_id": uuid4(),
        "document_id": uuid4(),
        "funder_id": uuid4(),
        "submitted_by": "test",
        "csv_bytes": b"data",
        "csv_filename": "x.csv",
    }
    with pytest.raises(TypeError, match="proposed_amount"):
        record_submission(
            repo,
            audit,
            proposed_amount=50000.00,  # type: ignore[arg-type]
            proposed_factor=Decimal("1.3000"),
            proposed_holdback=Decimal("0.1200"),
            **common,
        )
    with pytest.raises(TypeError, match="proposed_factor"):
        record_submission(
            repo,
            audit,
            proposed_amount=Decimal("50000.00"),
            proposed_factor=1.3,  # type: ignore[arg-type]
            proposed_holdback=Decimal("0.1200"),
            **common,
        )
    with pytest.raises(TypeError, match="proposed_holdback"):
        record_submission(
            repo,
            audit,
            proposed_amount=Decimal("50000.00"),
            proposed_factor=Decimal("1.3000"),
            proposed_holdback=0.12,  # type: ignore[arg-type]
            **common,
        )
    # Nothing landed in the repo or audit log on the rejected paths.
    assert audit.entries == []


def test_record_submission_natural_key_conflict_propagates() -> None:
    """A re-call with the same (merchant, document, funder) tuple raises
    SubmissionConflictError. The dashboard handler catches and logs;
    silently writing two rows would inflate the funder approval-rate
    denominator."""
    repo = InMemorySubmissionRepository()
    audit = InMemoryAuditLog()

    merchant_id = uuid4()
    document_id = uuid4()
    funder_id = uuid4()
    kwargs = {
        "merchant_id": merchant_id,
        "document_id": document_id,
        "funder_id": funder_id,
        "submitted_by": "test",
        "csv_bytes": b"data",
        "csv_filename": "x.csv",
        "proposed_amount": Decimal("50000.00"),
        "proposed_factor": Decimal("1.3000"),
        "proposed_holdback": Decimal("0.1200"),
    }
    record_submission(repo, audit, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(SubmissionConflictError):
        record_submission(repo, audit, **kwargs)  # type: ignore[arg-type]
    # Only one durable row + one audit row landed.
    assert len(repo.list_for_merchant(merchant_id)) == 1
    assert len(audit.entries) == 1
