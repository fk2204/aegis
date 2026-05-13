"""SubmissionRepository Protocol + in-memory implementation.

The Supabase-backed implementation is intentionally NOT included here —
migration 013 is checked in as DESIGN ONLY (not applied to production
Supabase). When Phase 7C lands the table, a ``SupabaseSubmissionRepository``
will be added next to this module, mirroring the funders / merchants
pattern. This in-memory impl is enough for tests today and for the
operator-facing dashboard once it switches off the
``MerchantRow.submitted_to_funder_ids`` transient field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from aegis.submissions.models import (
    SubmissionRow,
    SubmissionStatus,
    SubmissionStatusTransitionError,
    valid_status_transition,
)


class SubmissionNotFoundError(KeyError):
    """Raised when a submission id has no row."""


class SubmissionConflictError(ValueError):
    """Raised when the (merchant, document, funder) natural key already exists."""


class SubmissionRepository(Protocol):
    def get(self, submission_id: UUID) -> SubmissionRow: ...
    def find_by_deal_and_funder(
        self,
        *,
        merchant_id: UUID,
        document_id: UUID,
        funder_id: UUID,
    ) -> SubmissionRow | None: ...
    def list_for_merchant(self, merchant_id: UUID) -> list[SubmissionRow]: ...
    def list_for_funder(self, funder_id: UUID) -> list[SubmissionRow]: ...
    def create(self, submission: SubmissionRow) -> SubmissionRow: ...
    def transition_status(
        self,
        submission_id: UUID,
        *,
        target: SubmissionStatus,
        funder_response_note: str | None = None,
        funded_amount: object = None,
        factor_rate: object = None,
    ) -> SubmissionRow:
        """Move a submission to ``target`` status.

        Raises ``SubmissionStatusTransitionError`` if the transition is
        not allowed by the lifecycle map. When transitioning to
        ``funded``, ``funded_amount`` and ``factor_rate`` are required.
        """


class InMemorySubmissionRepository:
    """Dict-backed submission store. Tests + offline."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, SubmissionRow] = {}

    def get(self, submission_id: UUID) -> SubmissionRow:
        try:
            return self._by_id[submission_id]
        except KeyError as exc:
            raise SubmissionNotFoundError(str(submission_id)) from exc

    def find_by_deal_and_funder(
        self,
        *,
        merchant_id: UUID,
        document_id: UUID,
        funder_id: UUID,
    ) -> SubmissionRow | None:
        for row in self._by_id.values():
            if (
                row.merchant_id == merchant_id
                and row.document_id == document_id
                and row.funder_id == funder_id
            ):
                return row
        return None

    def list_for_merchant(self, merchant_id: UUID) -> list[SubmissionRow]:
        rows = [r for r in self._by_id.values() if r.merchant_id == merchant_id]
        rows.sort(key=lambda r: r.submitted_at, reverse=True)
        return rows

    def list_for_funder(self, funder_id: UUID) -> list[SubmissionRow]:
        rows = [r for r in self._by_id.values() if r.funder_id == funder_id]
        rows.sort(key=lambda r: r.submitted_at, reverse=True)
        return rows

    def create(self, submission: SubmissionRow) -> SubmissionRow:
        # Enforce the (merchant, document, funder) uniqueness from migration 013.
        existing = self.find_by_deal_and_funder(
            merchant_id=submission.merchant_id,
            document_id=submission.document_id,
            funder_id=submission.funder_id,
        )
        if existing is not None:
            raise SubmissionConflictError(
                f"submission for ({submission.merchant_id}, "
                f"{submission.document_id}, {submission.funder_id}) "
                f"already exists as {existing.id}"
            )
        now = datetime.now(UTC)
        stamped = submission.model_copy(update={"created_at": now, "updated_at": now})
        self._by_id[stamped.id] = stamped
        return stamped

    def transition_status(
        self,
        submission_id: UUID,
        *,
        target: SubmissionStatus,
        funder_response_note: str | None = None,
        funded_amount: object = None,
        factor_rate: object = None,
    ) -> SubmissionRow:
        current = self.get(submission_id)
        if not valid_status_transition(current.status, target):
            raise SubmissionStatusTransitionError(
                f"transition {current.status!r} -> {target!r} is not allowed"
            )

        now = datetime.now(UTC)
        update: dict[str, object] = {"status": target, "updated_at": now}

        if target in {"funder_declined", "funder_approved", "withdrawn"}:
            update["funder_response_at"] = now
            if funder_response_note is not None:
                update["funder_response_note"] = funder_response_note
        elif target == "funded":
            if funded_amount is None or factor_rate is None:
                raise SubmissionStatusTransitionError(
                    "transition to 'funded' requires funded_amount and factor_rate"
                )
            update["funded_amount"] = funded_amount
            update["factor_rate"] = factor_rate
            update["funded_at"] = now

        new_row = current.model_copy(update=update)
        self._by_id[submission_id] = new_row
        return new_row


__all__ = [
    "InMemorySubmissionRepository",
    "SubmissionConflictError",
    "SubmissionNotFoundError",
    "SubmissionRepository",
]
