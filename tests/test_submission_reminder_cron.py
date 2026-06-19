"""Tests for ``run_submission_reminder_cron`` (workers.py, 17:00 UTC daily).

The cron walks ``funder_note_submissions`` for ``status='pending'`` rows
older than 24h and posts one Close task per row prompting the operator
to log the funder response. Dedupe key is a
``close.task.submission_reminder`` audit row keyed on submission_id —
once a reminder fires it NEVER fires again for that submission.

Skip conditions covered here:
  * Pending <24h old → not in the candidate set at all.
  * Non-pending status (approved/declined/countered) → never selected.
  * Merchant has no ``close_lead_id`` → silent skip.
  * Prior ``close.task.submission_reminder`` audit row → silent skip
    (the duplicate guarantee — the operator does NOT get re-pinged on
    a submission they've already been pinged about).

Stub strategy mirrors ``tests/merchants/test_close_context.py``: a
``_StubCloseClient`` dataclass that records every ``create_task`` call
so the test inspects arg shape without needing httpx MockTransport. We
don't care about HTTP-level wire shape here — the cron's contract is
"called close_client.create_task with these arguments and wrote an
audit row," which the stub captures end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseError
from aegis.funder_note_submissions.models import (
    FunderNoteSubmissionRow,
    FunderNoteSubmissionStatus,
)
from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.workers import run_submission_reminder_pass


@dataclass
class _StubCloseClient:
    """Captures every ``create_task`` call args + returns a scripted id.

    Mirrors the stub shape in ``tests/merchants/test_close_context.py``
    so the cron exercises the same protocol surface CloseClient exposes.
    """

    response_id: str = "task_default"
    raise_error: CloseError | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create_task(
        self,
        lead_id: str,
        text: str,
        due_date: date | None = None,
        assigned_to: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "lead_id": lead_id,
                "text": text,
                "due_date": due_date,
                "assigned_to": assigned_to,
            }
        )
        if self.raise_error is not None:
            raise self.raise_error
        return {"id": self.response_id, "lead_id": lead_id, "text": text}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def funders() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def submissions() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def close_client() -> _StubCloseClient:
    return _StubCloseClient(response_id="task_close_abc")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 6, 19, 17, 0, 0, tzinfo=UTC)


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    business_name: str = "Stale Sub Co",
    close_lead_id: str | None = "lead_default",
) -> MerchantRow:
    return repo.upsert(
        MerchantRow(
            business_name=business_name,
            owner_name="Owner",
            state="CA",
            close_lead_id=close_lead_id,
        )
    )


def _seed_funder(
    repo: InMemoryFunderRepository,
    *,
    name: str = "Reliant Funder LLC",
) -> FunderRow:
    return repo.upsert(FunderRow(name=name))


def _seed_submission(
    repo: InMemoryFunderNoteSubmissionRepository,
    *,
    merchant_id: UUID,
    funder_id: UUID,
    submitted_at: datetime,
    status: FunderNoteSubmissionStatus = "pending",
    funder_note: str = "Submitted via dossier",
) -> FunderNoteSubmissionRow:
    row = FunderNoteSubmissionRow(
        merchant_id=merchant_id,
        funder_id=funder_id,
        submitted_at=submitted_at,
        submitted_by="operator@aegis.test",
        status=status,
        funder_note=funder_note,
    )
    # The InMemory repo's ``create`` stamps now() — bypass it so we can
    # set ``submitted_at`` to an arbitrary point in the past for the
    # staleness window test. Tests are the only caller that needs this.
    repo._by_id[row.id] = row
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pending_older_than_24h_creates_close_task_and_audits(
    audit: InMemoryAuditLog,
    submissions: InMemoryFunderNoteSubmissionRepository,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    close_client: _StubCloseClient,
) -> None:
    merchant = _seed_merchant(merchants, business_name="Acme Coffee LLC", close_lead_id="lead_acme")
    funder = _seed_funder(funders, name="Reliant Funder LLC")
    submission = _seed_submission(
        submissions,
        merchant_id=merchant.id,
        funder_id=funder.id,
        submitted_at=_NOW - timedelta(hours=30),
    )

    summary = run_submission_reminder_pass(
        audit=audit,
        submissions=submissions,
        merchants=merchants,
        funders=funders,
        close_client=close_client,  # type: ignore[arg-type]
        now=_NOW,
    )

    assert summary["considered"] == 1
    assert summary["created"] == 1
    assert summary["skipped_no_lead"] == 0
    assert summary["skipped_dup"] == 0
    assert summary["failed"] == 0

    assert len(close_client.calls) == 1
    call = close_client.calls[0]
    assert call["lead_id"] == "lead_acme"
    assert "Log funder response" in call["text"]
    assert "Acme Coffee LLC" in call["text"]
    assert "Reliant Funder LLC" in call["text"]
    assert "approved, declined, or countered" in call["text"]
    assert call["due_date"] == _NOW.date()

    audited = [e for e in audit.entries if e["action"] == "close.task.submission_reminder"]
    assert len(audited) == 1
    details = audited[0]["details"]
    assert details["submission_id"] == str(submission.id)
    assert details["funder_id"] == str(funder.id)
    assert details["close_lead_id"] == "lead_acme"
    assert details["close_task_id"] == "task_close_abc"
    # PII guarantee — names live in the Close task text, NOT the audit row.
    assert "business_name" not in details
    assert "funder_name" not in details
    assert audited[0]["subject_type"] == "funder_note_submission"
    assert audited[0]["subject_id"] == str(submission.id)


def test_pending_within_24h_is_not_reminded(
    audit: InMemoryAuditLog,
    submissions: InMemoryFunderNoteSubmissionRepository,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    close_client: _StubCloseClient,
) -> None:
    merchant = _seed_merchant(merchants)
    funder = _seed_funder(funders)
    _seed_submission(
        submissions,
        merchant_id=merchant.id,
        funder_id=funder.id,
        submitted_at=_NOW - timedelta(hours=12),
    )

    summary = run_submission_reminder_pass(
        audit=audit,
        submissions=submissions,
        merchants=merchants,
        funders=funders,
        close_client=close_client,  # type: ignore[arg-type]
        now=_NOW,
    )

    assert summary["considered"] == 0
    assert summary["created"] == 0
    assert close_client.calls == []
    assert not any(e["action"] == "close.task.submission_reminder" for e in audit.entries)


def test_prior_reminder_audit_blocks_duplicate(
    audit: InMemoryAuditLog,
    submissions: InMemoryFunderNoteSubmissionRepository,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    close_client: _StubCloseClient,
) -> None:
    """The dedupe rule: any prior ``close.task.submission_reminder`` audit
    row for this submission_id means we skip forever, no matter how stale
    the row gets."""
    merchant = _seed_merchant(merchants)
    funder = _seed_funder(funders)
    submission = _seed_submission(
        submissions,
        merchant_id=merchant.id,
        funder_id=funder.id,
        submitted_at=_NOW - timedelta(days=5),
    )
    # Pre-seed the audit row a previous cron run would have written.
    audit.record(
        actor="cron.submission_reminder",
        action="close.task.submission_reminder",
        subject_type="funder_note_submission",
        subject_id=submission.id,
        details={
            "submission_id": str(submission.id),
            "funder_id": str(funder.id),
            "close_lead_id": merchant.close_lead_id,
            "close_task_id": "task_prior_run",
        },
    )

    summary = run_submission_reminder_pass(
        audit=audit,
        submissions=submissions,
        merchants=merchants,
        funders=funders,
        close_client=close_client,  # type: ignore[arg-type]
        now=_NOW,
    )

    assert summary["considered"] == 1
    assert summary["created"] == 0
    assert summary["skipped_dup"] == 1
    assert close_client.calls == []
    # Still only the pre-seeded audit row — no second one written.
    assert len([e for e in audit.entries if e["action"] == "close.task.submission_reminder"]) == 1


def test_merchant_missing_close_lead_id_is_skipped_silently(
    audit: InMemoryAuditLog,
    submissions: InMemoryFunderNoteSubmissionRepository,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    close_client: _StubCloseClient,
) -> None:
    merchant = _seed_merchant(merchants, close_lead_id=None)
    funder = _seed_funder(funders)
    _seed_submission(
        submissions,
        merchant_id=merchant.id,
        funder_id=funder.id,
        submitted_at=_NOW - timedelta(hours=48),
    )

    summary = run_submission_reminder_pass(
        audit=audit,
        submissions=submissions,
        merchants=merchants,
        funders=funders,
        close_client=close_client,  # type: ignore[arg-type]
        now=_NOW,
    )

    assert summary["considered"] == 1
    assert summary["created"] == 0
    assert summary["skipped_no_lead"] == 1
    assert summary["failed"] == 0
    assert close_client.calls == []
    # No audit row, success or failure — the skip is truly silent.
    assert not any(e["action"] == "close.task.submission_reminder" for e in audit.entries)
    assert not any(e["action"] == "close.task.submission_reminder_failed" for e in audit.entries)


@pytest.mark.parametrize("status", ["approved", "declined", "countered"])
def test_non_pending_statuses_are_never_selected(
    audit: InMemoryAuditLog,
    submissions: InMemoryFunderNoteSubmissionRepository,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    close_client: _StubCloseClient,
    status: FunderNoteSubmissionStatus,
) -> None:
    merchant = _seed_merchant(merchants)
    funder = _seed_funder(funders)
    _seed_submission(
        submissions,
        merchant_id=merchant.id,
        funder_id=funder.id,
        submitted_at=_NOW - timedelta(days=7),
        status=status,
    )

    summary = run_submission_reminder_pass(
        audit=audit,
        submissions=submissions,
        merchants=merchants,
        funders=funders,
        close_client=close_client,  # type: ignore[arg-type]
        now=_NOW,
    )

    assert summary["considered"] == 0
    assert summary["created"] == 0
    assert close_client.calls == []


def test_two_stale_pending_one_already_reminded_only_unreminded_gets_task(
    audit: InMemoryAuditLog,
    submissions: InMemoryFunderNoteSubmissionRepository,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    close_client: _StubCloseClient,
) -> None:
    merchant = _seed_merchant(merchants, close_lead_id="lead_shared")
    funder_a = _seed_funder(funders, name="Funder Alpha")
    funder_b = _seed_funder(funders, name="Funder Bravo")

    already_reminded = _seed_submission(
        submissions,
        merchant_id=merchant.id,
        funder_id=funder_a.id,
        submitted_at=_NOW - timedelta(days=3),
    )
    fresh_stale = _seed_submission(
        submissions,
        merchant_id=merchant.id,
        funder_id=funder_b.id,
        submitted_at=_NOW - timedelta(hours=26),
    )
    # Pre-seed the audit row blocking the first one only.
    audit.record(
        actor="cron.submission_reminder",
        action="close.task.submission_reminder",
        subject_type="funder_note_submission",
        subject_id=already_reminded.id,
        details={
            "submission_id": str(already_reminded.id),
            "funder_id": str(funder_a.id),
            "close_lead_id": merchant.close_lead_id,
            "close_task_id": "task_prior",
        },
    )

    summary = run_submission_reminder_pass(
        audit=audit,
        submissions=submissions,
        merchants=merchants,
        funders=funders,
        close_client=close_client,  # type: ignore[arg-type]
        now=_NOW,
    )

    assert summary["considered"] == 2
    assert summary["created"] == 1
    assert summary["skipped_dup"] == 1
    assert len(close_client.calls) == 1
    # The new task is for the un-reminded submission only.
    assert "Funder Bravo" in close_client.calls[0]["text"]
    assert "Funder Alpha" not in close_client.calls[0]["text"]

    new_audit_rows = [
        e
        for e in audit.entries
        if e["action"] == "close.task.submission_reminder"
        and (e["details"] or {}).get("submission_id") == str(fresh_stale.id)
    ]
    assert len(new_audit_rows) == 1


def test_close_task_failure_audits_failed_action_and_does_not_raise(
    audit: InMemoryAuditLog,
    submissions: InMemoryFunderNoteSubmissionRepository,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
) -> None:
    """A Close 5xx on the task POST is audited as ``_failed`` but the
    overall cron pass keeps walking the candidate list — one bad Lead
    must not abort the rest of the run."""
    merchant = _seed_merchant(merchants)
    funder = _seed_funder(funders)
    _seed_submission(
        submissions,
        merchant_id=merchant.id,
        funder_id=funder.id,
        submitted_at=_NOW - timedelta(days=2),
    )
    failing = _StubCloseClient(raise_error=CloseError("task POST 500", status_code=500))

    summary = run_submission_reminder_pass(
        audit=audit,
        submissions=submissions,
        merchants=merchants,
        funders=funders,
        close_client=failing,  # type: ignore[arg-type]
        now=_NOW,
    )

    assert summary["failed"] == 1
    assert summary["created"] == 0
    failures = [e for e in audit.entries if e["action"] == "close.task.submission_reminder_failed"]
    assert len(failures) == 1
    assert failures[0]["details"]["status_code"] == 500
    # The success row must not have been written.
    assert not any(e["action"] == "close.task.submission_reminder" for e in audit.entries)


def test_concurrent_merchant_delete_skips_gracefully(
    audit: InMemoryAuditLog,
    submissions: InMemoryFunderNoteSubmissionRepository,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    close_client: _StubCloseClient,
) -> None:
    """If the merchant disappears between ``list_pending_older_than`` and
    ``merchants.get``, the cron logs + skips that row instead of crashing
    the whole pass."""
    ghost_merchant_id = uuid4()
    funder = _seed_funder(funders)
    _seed_submission(
        submissions,
        merchant_id=ghost_merchant_id,
        funder_id=funder.id,
        submitted_at=_NOW - timedelta(hours=48),
    )

    summary = run_submission_reminder_pass(
        audit=audit,
        submissions=submissions,
        merchants=merchants,
        funders=funders,
        close_client=close_client,  # type: ignore[arg-type]
        now=_NOW,
    )

    assert summary["considered"] == 1
    assert summary["created"] == 0
    assert summary["skipped_missing_merchant"] == 1
    assert close_client.calls == []
