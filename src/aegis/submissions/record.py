"""``record_submission`` — single entry point for persisting a submission row.

Mirrors ``aegis.compliance.transmission.record_disclosure_transmission``:
one helper that writes the durable row, then writes an audit_log row
referencing it, then returns the persisted record. Both writes must
succeed — per CLAUDE.md ``Auditability``, audit-write failures FAIL
the operation rather than silently log-and-continue.

The existing ``deal.submit_to_funders`` audit row written by the
dashboard handler is preserved unchanged (it carries the multi-funder
metadata: attachment hash, dossier hash, score, tier). This helper adds
a per-funder ``deal.submission_persisted`` audit row carrying the
``submission_id`` so the audit trail points at the durable row.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from aegis.submissions.models import SubmissionRow
from aegis.submissions.repository import (
    SubmissionConflictError,
    SubmissionRepository,
    SubmissionWriteError,
)

if TYPE_CHECKING:
    from aegis.audit import AuditLog


def sha256_hex(content: bytes) -> str:
    """Lowercase hex sha256 of the input bytes.

    Exported for callers that already hash CSV bytes elsewhere (the
    dashboard submission handler computes ``attachment_sha256`` on the
    full ZIP; per-funder CSVs need their own per-file digest matching
    the ``csv_doc_hash`` column).
    """
    return hashlib.sha256(content).hexdigest()


def record_submission(
    repo: SubmissionRepository,
    audit: AuditLog,
    *,
    merchant_id: UUID,
    document_id: UUID,
    funder_id: UUID,
    submitted_by: str,
    csv_bytes: bytes,
    csv_filename: str,
    proposed_amount: Decimal,
    proposed_factor: Decimal,
    proposed_holdback: Decimal,
    submitted_at: datetime | None = None,
    actor_email: str | None = None,
) -> SubmissionRow:
    """Persist one submission row + write a ``deal.submission_persisted``
    audit row pointing at it.

    Decimal-discipline: ``proposed_amount`` / ``proposed_factor`` /
    ``proposed_holdback`` MUST be ``Decimal`` — a float here would corrupt
    P&L roll-ups downstream. ``TypeError`` if not.

    Idempotency: a re-call with the same ``(merchant_id, document_id,
    funder_id)`` raises ``SubmissionConflictError``. The dashboard
    handler catches and treats as "already submitted" — operator-visible
    re-submission belongs in a separate UPDATE flow (Phase 7C scope).

    Audit-write failure: propagates ``AuditWriteError``. The DB row is
    NOT rolled back (we already inserted it); the caller sees the audit
    failure and treats it as a 500. Per CLAUDE.md, an unaudited write
    is acceptable to surface but never to silently swallow.
    """
    for name, value in (
        ("proposed_amount", proposed_amount),
        ("proposed_factor", proposed_factor),
        ("proposed_holdback", proposed_holdback),
    ):
        if not isinstance(value, Decimal):
            raise TypeError(
                f"{name} must be Decimal (never float — money/rate math); "
                f"got {type(value).__name__}"
            )

    submission = SubmissionRow(
        merchant_id=merchant_id,
        document_id=document_id,
        funder_id=funder_id,
        submitted_at=submitted_at or datetime.now(UTC),
        submitted_by=submitted_by,
        csv_doc_hash=sha256_hex(csv_bytes),
        csv_filename=csv_filename,
        proposed_amount=proposed_amount,
        proposed_factor=proposed_factor,
        proposed_holdback=proposed_holdback,
    )

    try:
        persisted = repo.create(submission)
    except SubmissionConflictError:
        raise
    except SubmissionWriteError:
        raise
    except Exception as exc:  # pragma: no cover — defensive
        raise SubmissionWriteError(
            f"unexpected failure persisting submission for "
            f"merchant_id={merchant_id} funder_id={funder_id}"
        ) from exc

    # Per CLAUDE.md: audit-write failures FAIL the operation. We don't
    # try/except — AuditWriteError must propagate to the caller.
    # ``details`` carries id references only — never CSV bytes / PII.
    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="deal.submission_persisted",
        subject_type="merchant",
        subject_id=merchant_id,
        details={
            "submission_id": str(persisted.id),
            "document_id": str(document_id),
            "funder_id": str(funder_id),
            "csv_doc_hash": persisted.csv_doc_hash,
            "csv_filename": persisted.csv_filename,
        },
    )
    return persisted


__all__ = [
    "record_submission",
    "sha256_hex",
]
