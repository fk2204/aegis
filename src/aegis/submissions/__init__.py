"""Submissions package — durable record of CSV submissions forwarded to funders.

U20 (2026-06-09): the table is now the live source of truth for the
portfolio funder-approval panel (rewired off ``audit_log`` JSON parsing
the same way U17 closed the tier-counts gap). Both the in-memory and
Supabase repositories implement the full ``SubmissionRepository``
contract; ``record_submission`` is the single helper the dashboard
submit handler calls to persist one row per matched funder.

Today the per-funder submission CSV is produced by
``aegis.scoring.submission_package.build_submission_files`` and zipped
into the operator's response; the dashboard handler calls
``record_submission`` once per file so the durable row + audit pair
land atomically (per CLAUDE.md Auditability — audit failures FAIL
the operation).
"""

from aegis.submissions.models import (
    SubmissionRow,
    SubmissionStatus,
    SubmissionStatusTransitionError,
    valid_status_transition,
)
from aegis.submissions.record import record_submission, sha256_hex
from aegis.submissions.repository import (
    DECIDED_STATUSES,
    InMemorySubmissionRepository,
    SubmissionConflictError,
    SubmissionNotFoundError,
    SubmissionRepository,
    SubmissionWriteError,
    SupabaseSubmissionRepository,
)

__all__ = [
    "DECIDED_STATUSES",
    "InMemorySubmissionRepository",
    "SubmissionConflictError",
    "SubmissionNotFoundError",
    "SubmissionRepository",
    "SubmissionRow",
    "SubmissionStatus",
    "SubmissionStatusTransitionError",
    "SubmissionWriteError",
    "SupabaseSubmissionRepository",
    "record_submission",
    "sha256_hex",
    "valid_status_transition",
]
