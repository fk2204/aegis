"""SubmissionRepository Protocol + in-memory + Supabase implementations.

Migration 013 was design-only at module-creation time. U20 lights up
the actual write + read path: the durable submissions table replaces
audit_log JSON parsing as the source for the portfolio funder-approval
table (mirrors the U17 closure for tier counts).

The Supabase implementation mirrors the
``SupabaseDisclosureTransmissionRepository`` pattern in
``aegis/compliance/transmission.py`` — payload helpers convert Decimal
to ``str`` (so the Postgres ``numeric(14,2)`` columns receive the exact
decimal text, never a float coercion), and row-decoders rehydrate
Decimal / datetime back through ``_row_to_submission``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID

from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.submissions.models import (
    SubmissionRow,
    SubmissionStatus,
    SubmissionStatusTransitionError,
    valid_status_transition,
)

_log = get_logger(__name__)


class SubmissionNotFoundError(KeyError):
    """Raised when a submission id has no row."""


class SubmissionConflictError(ValueError):
    """Raised when the (merchant, document, funder) natural key already exists."""


class SubmissionWriteError(RuntimeError):
    """Raised when a submission row could not be persisted.

    Mirrors ``AuditWriteError`` / ``DisclosureTransmissionWriteError``
    semantics: a write failure halts the calling operation rather than
    letting it ship a "submitted" audit row with no durable record.
    """


# Statuses that count as a decided funder reply for portfolio approval-rate math.
# ``submitted`` is the open / waiting bucket; ``withdrawn`` is operator-cancelled,
# not a funder decision.
DECIDED_STATUSES: frozenset[SubmissionStatus] = frozenset(
    {"funder_declined", "funder_approved", "funded"}
)


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

    def list_in_window(
        self,
        *,
        from_date: date,
        to_date: date,
    ) -> list[SubmissionRow]:
        """Return submissions whose ``submitted_at`` falls in
        ``[from_date, to_date]`` inclusive. Newest first.

        Powers the portfolio funder-approval table (U20). The route
        passes the date range the operator selected; the implementation
        bounds the query at the DB layer.
        """

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

    def list_in_window(
        self,
        *,
        from_date: date,
        to_date: date,
    ) -> list[SubmissionRow]:
        if to_date < from_date:
            raise ValueError(
                f"to_date {to_date} is earlier than from_date {from_date}"
            )
        rows = [
            r
            for r in self._by_id.values()
            if from_date <= r.submitted_at.date() <= to_date
        ]
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


class SupabaseSubmissionRepository:
    """Persistence backed by Postgres ``submissions`` (migration 013).

    Mirrors the in-memory contract. Decimal columns are sent as ``str``
    so the Postgres ``numeric(14,2)`` / ``numeric(6,4)`` types receive
    exact text — no binary-float round-trip. Insert / update failures
    raise ``SubmissionWriteError`` so the calling pipeline can refuse
    to mark the submission as recorded.
    """

    def get(self, submission_id: UUID) -> SubmissionRow:
        result = (
            get_supabase()
            .table("submissions")
            .select("*")
            .eq("id", str(submission_id))
            .limit(1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise SubmissionNotFoundError(str(submission_id))
        return _row_to_submission(rows[0])

    def find_by_deal_and_funder(
        self,
        *,
        merchant_id: UUID,
        document_id: UUID,
        funder_id: UUID,
    ) -> SubmissionRow | None:
        result = (
            get_supabase()
            .table("submissions")
            .select("*")
            .eq("merchant_id", str(merchant_id))
            .eq("document_id", str(document_id))
            .eq("funder_id", str(funder_id))
            .limit(1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            return None
        return _row_to_submission(rows[0])

    def list_for_merchant(self, merchant_id: UUID) -> list[SubmissionRow]:
        result = (
            get_supabase()
            .table("submissions")
            .select("*")
            .eq("merchant_id", str(merchant_id))
            .order("submitted_at", desc=True)
            .limit(500)
            .execute()
        )
        return [
            _row_to_submission(cast(dict[str, Any], r))
            for r in (result.data or [])
        ]

    def list_for_funder(self, funder_id: UUID) -> list[SubmissionRow]:
        result = (
            get_supabase()
            .table("submissions")
            .select("*")
            .eq("funder_id", str(funder_id))
            .order("submitted_at", desc=True)
            .limit(500)
            .execute()
        )
        return [
            _row_to_submission(cast(dict[str, Any], r))
            for r in (result.data or [])
        ]

    def list_in_window(
        self,
        *,
        from_date: date,
        to_date: date,
    ) -> list[SubmissionRow]:
        if to_date < from_date:
            raise ValueError(
                f"to_date {to_date} is earlier than from_date {from_date}"
            )
        result = (
            get_supabase()
            .table("submissions")
            .select("*")
            .gte("submitted_at", from_date.isoformat())
            .lte("submitted_at", to_date.isoformat() + "T23:59:59Z")
            .order("submitted_at", desc=True)
            .limit(5000)
            .execute()
        )
        return [
            _row_to_submission(cast(dict[str, Any], r))
            for r in (result.data or [])
        ]

    def create(self, submission: SubmissionRow) -> SubmissionRow:
        # Defensive natural-key check — Postgres ``uq_submissions_deal_funder``
        # is the source of truth, but probing first lets us raise
        # ``SubmissionConflictError`` cleanly instead of swallowing a
        # supabase 23505 exception.
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
        payload = _submission_to_payload(submission)
        try:
            result = (
                get_supabase()
                .table("submissions")
                .insert(payload)
                .execute()
            )
        except Exception as exc:
            _log.error(
                "submissions.write_failed merchant_id=%s document_id=%s funder_id=%s",
                submission.merchant_id,
                submission.document_id,
                submission.funder_id,
            )
            raise SubmissionWriteError(
                f"failed to insert submission for merchant_id={submission.merchant_id}"
            ) from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise SubmissionWriteError(
                "supabase insert returned no row for submission"
            )
        return _row_to_submission(rows[0])

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
        update: dict[str, Any] = {
            "status": target,
            "updated_at": now.isoformat(),
        }
        if target in {"funder_declined", "funder_approved", "withdrawn"}:
            update["funder_response_at"] = now.isoformat()
            if funder_response_note is not None:
                update["funder_response_note"] = funder_response_note
        elif target == "funded":
            if funded_amount is None or factor_rate is None:
                raise SubmissionStatusTransitionError(
                    "transition to 'funded' requires funded_amount and factor_rate"
                )
            if not isinstance(funded_amount, Decimal):
                raise TypeError(
                    "funded_amount must be Decimal (never float — money math)"
                )
            if not isinstance(factor_rate, Decimal):
                raise TypeError(
                    "factor_rate must be Decimal (never float — rate math)"
                )
            update["funded_amount"] = str(funded_amount)
            update["factor_rate"] = str(factor_rate)
            update["funded_at"] = now.isoformat()

        try:
            result = (
                get_supabase()
                .table("submissions")
                .update(update)
                .eq("id", str(submission_id))
                .execute()
            )
        except Exception as exc:
            _log.error(
                "submissions.transition_failed id=%s target=%s",
                submission_id,
                target,
            )
            raise SubmissionWriteError(
                f"failed to transition submission {submission_id} to {target}"
            ) from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise SubmissionWriteError(
                f"supabase update returned no row for submission {submission_id}"
            )
        return _row_to_submission(rows[0])


# ---------------------------------------------------------------------------
# Row encoders / decoders
# ---------------------------------------------------------------------------


def _submission_to_payload(s: SubmissionRow) -> dict[str, Any]:
    """Encode a SubmissionRow for ``supabase.table('submissions').insert``.

    Decimal → str so Postgres ``numeric`` columns receive exact text.
    datetime → ISO-8601 string. UUID → str.
    """

    def _dec_or_none(v: Decimal | None) -> str | None:
        return str(v) if v is not None else None

    def _dt_or_none(v: datetime | None) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "id": str(s.id),
        "merchant_id": str(s.merchant_id),
        "document_id": str(s.document_id),
        "funder_id": str(s.funder_id),
        "submitted_at": s.submitted_at.isoformat(),
        "submitted_by": s.submitted_by,
        "csv_doc_hash": s.csv_doc_hash,
        "csv_filename": s.csv_filename,
        "proposed_amount": str(s.proposed_amount),
        "proposed_factor": str(s.proposed_factor),
        "proposed_holdback": str(s.proposed_holdback),
        "status": s.status,
        "funder_response_at": _dt_or_none(s.funder_response_at),
        "funder_response_note": s.funder_response_note,
        "funded_amount": _dec_or_none(s.funded_amount),
        "factor_rate": _dec_or_none(s.factor_rate),
        "funded_at": _dt_or_none(s.funded_at),
    }


def _row_to_submission(row: dict[str, Any]) -> SubmissionRow:
    """Decode a Postgres row dict back to a SubmissionRow.

    Pydantic-strict on the model side ensures a column drift trips at
    parse time rather than corrupting a downstream roll-up.
    """

    def _dec(key: str) -> Decimal | None:
        v = row.get(key)
        return Decimal(str(v)) if v is not None else None

    def _dt(key: str) -> datetime | None:
        v = row.get(key)
        if v is None:
            return None
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    proposed_amount = _dec("proposed_amount")
    proposed_factor = _dec("proposed_factor")
    proposed_holdback = _dec("proposed_holdback")
    submitted_at = _dt("submitted_at")
    if (
        proposed_amount is None
        or proposed_factor is None
        or proposed_holdback is None
        or submitted_at is None
    ):
        raise SubmissionWriteError(
            "supabase row missing required submission columns"
        )

    return SubmissionRow(
        id=UUID(row["id"]),
        merchant_id=UUID(row["merchant_id"]),
        document_id=UUID(row["document_id"]),
        funder_id=UUID(row["funder_id"]),
        submitted_at=submitted_at,
        submitted_by=row["submitted_by"],
        csv_doc_hash=row["csv_doc_hash"],
        csv_filename=row["csv_filename"],
        proposed_amount=proposed_amount,
        proposed_factor=proposed_factor,
        proposed_holdback=proposed_holdback,
        status=row.get("status") or "submitted",
        funder_response_at=_dt("funder_response_at"),
        funder_response_note=row.get("funder_response_note"),
        funded_amount=_dec("funded_amount"),
        factor_rate=_dec("factor_rate"),
        funded_at=_dt("funded_at"),
        created_at=_dt("created_at"),
        updated_at=_dt("updated_at"),
    )


__all__ = [
    "DECIDED_STATUSES",
    "InMemorySubmissionRepository",
    "SubmissionConflictError",
    "SubmissionNotFoundError",
    "SubmissionRepository",
    "SubmissionWriteError",
    "SupabaseSubmissionRepository",
]
