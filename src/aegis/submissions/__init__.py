"""Submissions package — durable record of CSV submissions forwarded to funders.

Pydantic model + in-memory repository only. The Supabase implementation
is intentionally deferred until Phase 7C proper — the schema migration
(``migrations/013_submissions_table.sql``) is checked in for review but
not applied.

Today the per-funder submission CSV is produced by
``aegis.scoring.submission_package.build_submission_files`` and zipped
into the operator's response. The transient tracking on
``MerchantRow.submitted_to_funder_ids`` resets on every Supabase round-
trip (see ``aegis/merchants/models.py`` lines 81-83); ``SubmissionRow``
replaces that with a durable, queryable record once the table ships.
"""

from aegis.submissions.models import (
    SubmissionRow,
    SubmissionStatus,
    SubmissionStatusTransitionError,
    valid_status_transition,
)
from aegis.submissions.repository import (
    InMemorySubmissionRepository,
    SubmissionConflictError,
    SubmissionNotFoundError,
    SubmissionRepository,
)

__all__ = [
    "InMemorySubmissionRepository",
    "SubmissionConflictError",
    "SubmissionNotFoundError",
    "SubmissionRepository",
    "SubmissionRow",
    "SubmissionStatus",
    "SubmissionStatusTransitionError",
    "valid_status_transition",
]
