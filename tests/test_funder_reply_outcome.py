"""Tests for the manual outcome-capture path on ``funder_replies``.

Covers ``aegis.funders.replies.record_outcome`` (migration 071 columns):

* Pydantic invariant — outcome=declined / no_response rejects offer fields.
* In-memory repo round-trip — outcome fields persist with the right types
  (Decimal preserved end-to-end; term_days as int).
* ``outcome_recorded_at`` auto-stamped by ``record_outcome``.
* ``outcome_recorded_by`` faithfully copied from the payload (the route
  derives it from the operator email).
* Audit row ``funder_reply.outcome_recorded`` lands with the right
  ``subject_type`` / ``subject_id`` / ``details`` shape.
* Audit-write failure propagates as ``AuditWriteError`` so the calling
  surface aborts rather than silently log-and-continuing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.audit import AuditWriteError, InMemoryAuditLog
from aegis.funders.replies import (
    FunderReplyOutcomePayload,
    InMemoryFunderReplyRepository,
    record_outcome,
)

NOW = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


def _payload(
    *,
    deal_id: UUID | None = None,
    funder_id: UUID | None = None,
    outcome: str = "approved",
    outcome_amount: Decimal | None = Decimal("50000.00"),
    outcome_factor_rate: Decimal | None = Decimal("1.3000"),
    outcome_term_days: int | None = 120,
    outcome_notes: str | None = "Phone confirmation from underwriter",
    outcome_recorded_by: str = "filip@commerafunding.com",
) -> FunderReplyOutcomePayload:
    return FunderReplyOutcomePayload(
        deal_id=deal_id or uuid4(),
        funder_id=funder_id or uuid4(),
        outcome=outcome,
        outcome_amount=outcome_amount,
        outcome_factor_rate=outcome_factor_rate,
        outcome_term_days=outcome_term_days,
        outcome_notes=outcome_notes,
        outcome_recorded_by=outcome_recorded_by,
    )


# ---------------------------------------------------------------------------
# Pydantic invariants
# ---------------------------------------------------------------------------


def test_declined_outcome_rejects_offer_fields() -> None:
    """A declined outcome with offer fields must fail at Pydantic validation
    (mirrors the DB CHECK constraint from migration 071)."""
    with pytest.raises(ValueError, match="outcome='declined'"):
        FunderReplyOutcomePayload(
            deal_id=uuid4(),
            funder_id=uuid4(),
            outcome="declined",
            outcome_amount=Decimal("50000.00"),
            outcome_recorded_by="filip@commerafunding.com",
        )


def test_no_response_outcome_rejects_offer_fields() -> None:
    """no_response cannot carry offer fields either — funder didn't reply,
    so amount/factor/term make no sense."""
    with pytest.raises(ValueError, match="outcome='no_response'"):
        FunderReplyOutcomePayload(
            deal_id=uuid4(),
            funder_id=uuid4(),
            outcome="no_response",
            outcome_factor_rate=Decimal("1.300"),
            outcome_recorded_by="filip@commerafunding.com",
        )


def test_approved_outcome_accepts_offer_fields() -> None:
    """Approved outcomes carry the full offer triple."""
    p = _payload(outcome="approved")
    assert p.outcome == "approved"
    assert p.outcome_amount == Decimal("50000.00")
    assert p.outcome_factor_rate == Decimal("1.3000")
    assert p.outcome_term_days == 120


def test_countered_outcome_accepts_offer_fields() -> None:
    """Countered = funder responded with different terms; offer fields ok."""
    p = _payload(outcome="countered", outcome_amount=Decimal("30000.00"))
    assert p.outcome == "countered"
    assert p.outcome_amount == Decimal("30000.00")


def test_declined_outcome_with_none_offer_fields_validates() -> None:
    """Declined outcome with offer fields explicitly None is the canonical
    declined shape — must validate."""
    p = FunderReplyOutcomePayload(
        deal_id=uuid4(),
        funder_id=uuid4(),
        outcome="declined",
        outcome_recorded_by="filip@commerafunding.com",
        outcome_notes="Funder declined — credit too thin",
    )
    assert p.outcome == "declined"
    assert p.outcome_amount is None
    assert p.outcome_factor_rate is None
    assert p.outcome_term_days is None


# ---------------------------------------------------------------------------
# record_outcome — persistence + audit
# ---------------------------------------------------------------------------


def test_record_outcome_persists_all_fields() -> None:
    """Round-trip: every field on the payload lands on the in-memory row.

    Decimal preserved exactly (no float conversion); term_days stays as
    int. ``outcome_recorded_at`` matches the ``now`` parameter the
    route supplies.
    """
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    funder_id = uuid4()
    payload = _payload(
        deal_id=deal_id,
        funder_id=funder_id,
        outcome="approved",
        outcome_amount=Decimal("75000.00"),
        outcome_factor_rate=Decimal("1.2800"),
        outcome_term_days=90,
        outcome_notes="Approved at $75k",
    )
    reply_id = record_outcome(payload, repo=repo, audit=audit, now=NOW)

    rows = repo.replies()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == str(reply_id)
    assert row["deal_id"] == str(deal_id)
    assert row["funder_id"] == str(funder_id)
    assert row["outcome"] == "approved"
    # Decimal preserved end-to-end.
    assert row["outcome_amount"] == Decimal("75000.00")
    assert isinstance(row["outcome_amount"], Decimal)
    assert row["outcome_factor_rate"] == Decimal("1.2800")
    assert isinstance(row["outcome_factor_rate"], Decimal)
    assert row["outcome_term_days"] == 90
    assert isinstance(row["outcome_term_days"], int)
    assert row["outcome_notes"] == "Approved at $75k"
    assert row["outcome_recorded_at"] == NOW.isoformat()
    assert row["outcome_recorded_by"] == "filip@commerafunding.com"


def test_record_outcome_no_response_leaves_status_null() -> None:
    """A no_response row has NULL status (funder didn't reply — the
    legacy status enum has no value to express it). The outcome column
    carries the operator confirmation."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    payload = FunderReplyOutcomePayload(
        deal_id=uuid4(),
        funder_id=uuid4(),
        outcome="no_response",
        outcome_recorded_by="filip@commerafunding.com",
    )
    record_outcome(payload, repo=repo, audit=audit, now=NOW)

    row = repo.replies()[0]
    assert row["outcome"] == "no_response"
    assert row["status"] is None
    assert row["outcome_amount"] is None
    assert row["outcome_factor_rate"] is None
    assert row["outcome_term_days"] is None


def test_record_outcome_mirrors_outcome_to_status_for_compat() -> None:
    """For outcomes that have a status equivalent (approved/declined/countered),
    the status column is set so legacy readers see a sensible value."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    for outcome in ("approved", "declined", "countered"):
        payload = FunderReplyOutcomePayload(
            deal_id=uuid4(),
            funder_id=uuid4(),
            outcome=outcome,
            outcome_recorded_by="filip@commerafunding.com",
        )
        record_outcome(payload, repo=repo, audit=audit, now=NOW)

    rows = repo.replies()
    assert [r["status"] for r in rows] == ["approved", "declined", "countered"]
    assert [r["outcome"] for r in rows] == ["approved", "declined", "countered"]


def test_record_outcome_writes_audit_row_with_right_shape() -> None:
    """The audit row carries action=funder_reply.outcome_recorded,
    subject_type=deal, subject_id=deal_id, and a details payload that
    includes reply_id + funder_id + outcome + the offer fields."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    funder_id = uuid4()
    payload = _payload(deal_id=deal_id, funder_id=funder_id, outcome="approved")
    reply_id = record_outcome(payload, repo=repo, audit=audit, now=NOW)

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["action"] == "funder_reply.outcome_recorded"
    assert entry["subject_type"] == "deal"
    assert entry["subject_id"] == str(deal_id)
    assert entry["actor"] == "dashboard"
    assert entry["actor_email"] == "filip@commerafunding.com"
    details = entry["details"]
    assert details["reply_id"] == str(reply_id)
    assert details["funder_id"] == str(funder_id)
    assert details["outcome"] == "approved"
    assert details["outcome_amount"] == "50000.00"
    assert details["outcome_factor_rate"] == "1.3000"
    assert details["outcome_term_days"] == 120
    # Notes are NOT included verbatim — only char count, to keep PII
    # off the audit row (operator-typed text may include merchant detail).
    assert details["notes_chars"] == len("Phone confirmation from underwriter")
    assert "outcome_notes" not in details


def test_record_outcome_audit_failure_propagates_as_error() -> None:
    """Per CLAUDE.md Auditability: audit-write failure FAILS the operation.
    A raising audit log must propagate as ``AuditWriteError`` so the
    calling route aborts rather than silently log-and-continuing."""

    class _BoomAudit:
        def record(self, **kwargs: Any) -> None:
            raise AuditWriteError("simulated audit failure")

        def list_recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
            return []

        def list_for_subject(self, **kwargs: Any) -> list[dict[str, Any]]:
            return []

    repo = InMemoryFunderReplyRepository()
    audit = _BoomAudit()
    with pytest.raises(AuditWriteError, match="simulated audit failure"):
        record_outcome(_payload(), repo=repo, audit=audit, now=NOW)
