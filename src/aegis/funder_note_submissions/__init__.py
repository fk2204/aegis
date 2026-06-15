"""Funder-note-submissions package — durable record of Close-Note submit clicks.

Tracks every "Submit to Funder" button click made from the dossier
(``POST /ui/merchants/{merchant_id}/submit-to-funder``) so the dossier
history block can render the full timeline of attempts + funder
responses without re-parsing audit_log JSON.

Distinct from :mod:`aegis.submissions` (migration 013): that module
captures the CSV-bundle paper-trail (one row per matched funder per
bundle, with ``document_id`` + ``csv_doc_hash``). This module captures
the simpler activity-feed analogue — one row per Close Note POST,
framed against the top matched funder, mutated in place when the funder
responds with terms.

The merchants router writes one row per click via
:meth:`FunderNoteSubmissionRepository.create` immediately after the
Close Note POST succeeds and before the ``deal.funder_note_posted``
audit row is written, so the audit row's ``funder_note_submission_id``
detail can point at a row that's already durable.
"""

from aegis.funder_note_submissions.models import (
    FunderNoteSubmissionRow,
    FunderNoteSubmissionStatus,
)
from aegis.funder_note_submissions.repository import (
    FunderNoteSubmissionNotFoundError,
    FunderNoteSubmissionRepository,
    FunderNoteSubmissionWriteError,
    InMemoryFunderNoteSubmissionRepository,
    SupabaseFunderNoteSubmissionRepository,
)

__all__ = [
    "FunderNoteSubmissionNotFoundError",
    "FunderNoteSubmissionRepository",
    "FunderNoteSubmissionRow",
    "FunderNoteSubmissionStatus",
    "FunderNoteSubmissionWriteError",
    "InMemoryFunderNoteSubmissionRepository",
    "SupabaseFunderNoteSubmissionRepository",
]
