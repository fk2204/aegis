"""FunderNoteSubmissionRepository Protocol + in-memory + Supabase impls.

Mirrors the two-impl pattern of ``aegis.submissions.repository``. Decimal
columns are sent as ``str`` so the Postgres ``numeric(14,2)`` /
``numeric(6,4)`` types receive exact text — never a binary-float
round-trip. Insert / update failures raise
``FunderNoteSubmissionWriteError`` so the calling pipeline can refuse to
mark the submission as recorded (mirrors ``AuditWriteError`` /
``SubmissionWriteError`` semantics from CLAUDE.md Auditability).

Status policy: ``update_status`` accepts any
``FunderNoteSubmissionStatus`` and overwrites in place. Unlike the
``submissions`` table (which enforces a forward-only transition graph
through ``valid_status_transition``), funder responses on the Close
Note path arrive out-of-order and operators correct typos by re-posting
the same form — we trade lifecycle rigour for operator ergonomics on
this surface. The ``pending -> non-pending`` boundary still stamps
``responded_at = NOW()`` exactly once: once a row leaves pending, later
non-pending updates leave ``responded_at`` intact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID

from aegis.db import get_supabase
from aegis.funder_note_submissions.models import (
    FunderNoteSubmissionRow,
    FunderNoteSubmissionStatus,
)
from aegis.logger import get_logger

_log = get_logger(__name__)


class FunderNoteSubmissionNotFoundError(KeyError):
    """Raised when a funder_note_submission id has no row."""


class FunderNoteSubmissionWriteError(RuntimeError):
    """Raised when a funder_note_submission row could not be persisted."""


class FunderNoteSubmissionRepository(Protocol):
    def create(
        self,
        *,
        merchant_id: UUID,
        funder_id: UUID,
        funder_note: str,
        submitted_by: str,
    ) -> FunderNoteSubmissionRow: ...

    def list_for_merchant(
        self,
        merchant_id: UUID,
        *,
        limit: int = 50,
    ) -> list[FunderNoteSubmissionRow]: ...

    def list_in_window(
        self,
        *,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[FunderNoteSubmissionRow]:
        """Return submissions whose ``submitted_at`` falls in
        ``[from_dt, to_dt]`` inclusive, newest first.

        Powers the Sprint 3 portfolio dashboard (per-funder approval
        rate, monthly volume, industry breakdown).
        """

    def list_for_funder(
        self,
        funder_id: UUID,
        *,
        limit: int = 500,
    ) -> list[FunderNoteSubmissionRow]:
        """Return every submission against ``funder_id``, newest first.

        Powers the Sprint 3 funder-performance page.
        """

    def get(self, submission_id: UUID) -> FunderNoteSubmissionRow:
        """Fetch a single submission by id. Raises
        ``FunderNoteSubmissionNotFoundError`` when absent."""

    def update_status(
        self,
        submission_id: UUID,
        *,
        status: FunderNoteSubmissionStatus,
        offer_amount: Decimal | None = None,
        offer_factor: Decimal | None = None,
        offer_holdback: Decimal | None = None,
        notes: str | None = None,
    ) -> FunderNoteSubmissionRow: ...


class InMemoryFunderNoteSubmissionRepository:
    """Dict-backed funder_note_submission store. Tests + offline."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, FunderNoteSubmissionRow] = {}

    def create(
        self,
        *,
        merchant_id: UUID,
        funder_id: UUID,
        funder_note: str,
        submitted_by: str,
    ) -> FunderNoteSubmissionRow:
        now = datetime.now(UTC)
        row = FunderNoteSubmissionRow(
            merchant_id=merchant_id,
            funder_id=funder_id,
            submitted_at=now,
            submitted_by=submitted_by,
            funder_note=funder_note,
            created_at=now,
            updated_at=now,
        )
        self._by_id[row.id] = row
        return row

    def list_for_merchant(
        self,
        merchant_id: UUID,
        *,
        limit: int = 50,
    ) -> list[FunderNoteSubmissionRow]:
        rows = [r for r in self._by_id.values() if r.merchant_id == merchant_id]
        rows.sort(key=lambda r: r.submitted_at, reverse=True)
        return rows[:limit]

    def list_in_window(
        self,
        *,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[FunderNoteSubmissionRow]:
        rows = [r for r in self._by_id.values() if from_dt <= r.submitted_at <= to_dt]
        rows.sort(key=lambda r: r.submitted_at, reverse=True)
        return rows

    def list_for_funder(
        self,
        funder_id: UUID,
        *,
        limit: int = 500,
    ) -> list[FunderNoteSubmissionRow]:
        rows = [r for r in self._by_id.values() if r.funder_id == funder_id]
        rows.sort(key=lambda r: r.submitted_at, reverse=True)
        return rows[:limit]

    def get(self, submission_id: UUID) -> FunderNoteSubmissionRow:
        try:
            return self._by_id[submission_id]
        except KeyError as exc:
            raise FunderNoteSubmissionNotFoundError(str(submission_id)) from exc

    def update_status(
        self,
        submission_id: UUID,
        *,
        status: FunderNoteSubmissionStatus,
        offer_amount: Decimal | None = None,
        offer_factor: Decimal | None = None,
        offer_holdback: Decimal | None = None,
        notes: str | None = None,
    ) -> FunderNoteSubmissionRow:
        try:
            current = self._by_id[submission_id]
        except KeyError as exc:
            raise FunderNoteSubmissionNotFoundError(str(submission_id)) from exc

        now = datetime.now(UTC)
        update: dict[str, object] = {"status": status, "updated_at": now}
        if offer_amount is not None:
            update["offer_amount"] = offer_amount
        if offer_factor is not None:
            update["offer_factor"] = offer_factor
        if offer_holdback is not None:
            update["offer_holdback"] = offer_holdback
        if notes is not None:
            update["notes"] = notes

        # Stamp responded_at on the first pending -> non-pending edge.
        # Subsequent non-pending edits (operator correction) leave the
        # original response timestamp intact so the dossier history
        # surface keeps the "responded 4h after submission" reading.
        if status != "pending" and current.responded_at is None:
            update["responded_at"] = now

        new_row = current.model_copy(update=update)
        self._by_id[submission_id] = new_row
        return new_row


class SupabaseFunderNoteSubmissionRepository:
    """Persistence backed by Postgres ``funder_note_submissions`` (mig 057)."""

    def create(
        self,
        *,
        merchant_id: UUID,
        funder_id: UUID,
        funder_note: str,
        submitted_by: str,
    ) -> FunderNoteSubmissionRow:
        now = datetime.now(UTC)
        row = FunderNoteSubmissionRow(
            merchant_id=merchant_id,
            funder_id=funder_id,
            submitted_at=now,
            submitted_by=submitted_by,
            funder_note=funder_note,
        )
        payload = _row_to_payload(row)
        try:
            result = get_supabase().table("funder_note_submissions").insert(payload).execute()
        except Exception as exc:
            _log.error(
                "funder_note_submissions.write_failed merchant_id=%s funder_id=%s",
                merchant_id,
                funder_id,
            )
            raise FunderNoteSubmissionWriteError(
                f"failed to insert funder_note_submission for merchant_id={merchant_id}"
            ) from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise FunderNoteSubmissionWriteError(
                "supabase insert returned no row for funder_note_submission"
            )
        return _row_from_dict(rows[0])

    def list_for_merchant(
        self,
        merchant_id: UUID,
        *,
        limit: int = 50,
    ) -> list[FunderNoteSubmissionRow]:
        result = (
            get_supabase()
            .table("funder_note_submissions")
            .select("*")
            .eq("merchant_id", str(merchant_id))
            .order("submitted_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [_row_from_dict(cast(dict[str, Any], r)) for r in (result.data or [])]

    def list_in_window(
        self,
        *,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[FunderNoteSubmissionRow]:
        result = (
            get_supabase()
            .table("funder_note_submissions")
            .select("*")
            .gte("submitted_at", from_dt.isoformat())
            .lte("submitted_at", to_dt.isoformat())
            .order("submitted_at", desc=True)
            .execute()
        )
        return [_row_from_dict(cast(dict[str, Any], r)) for r in (result.data or [])]

    def list_for_funder(
        self,
        funder_id: UUID,
        *,
        limit: int = 500,
    ) -> list[FunderNoteSubmissionRow]:
        result = (
            get_supabase()
            .table("funder_note_submissions")
            .select("*")
            .eq("funder_id", str(funder_id))
            .order("submitted_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [_row_from_dict(cast(dict[str, Any], r)) for r in (result.data or [])]

    def get(self, submission_id: UUID) -> FunderNoteSubmissionRow:
        result = (
            get_supabase()
            .table("funder_note_submissions")
            .select("*")
            .eq("id", str(submission_id))
            .limit(1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise FunderNoteSubmissionNotFoundError(str(submission_id))
        return _row_from_dict(rows[0])

    def update_status(
        self,
        submission_id: UUID,
        *,
        status: FunderNoteSubmissionStatus,
        offer_amount: Decimal | None = None,
        offer_factor: Decimal | None = None,
        offer_holdback: Decimal | None = None,
        notes: str | None = None,
    ) -> FunderNoteSubmissionRow:
        existing = (
            get_supabase()
            .table("funder_note_submissions")
            .select("*")
            .eq("id", str(submission_id))
            .limit(1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], existing.data or [])
        if not rows:
            raise FunderNoteSubmissionNotFoundError(str(submission_id))
        current = _row_from_dict(rows[0])

        now = datetime.now(UTC)
        update: dict[str, Any] = {"status": status}
        if offer_amount is not None:
            if not isinstance(offer_amount, Decimal):
                raise TypeError("offer_amount must be Decimal (never float — money math)")
            update["offer_amount"] = str(offer_amount)
        if offer_factor is not None:
            if not isinstance(offer_factor, Decimal):
                raise TypeError("offer_factor must be Decimal (never float — rate math)")
            update["offer_factor"] = str(offer_factor)
        if offer_holdback is not None:
            if not isinstance(offer_holdback, Decimal):
                raise TypeError("offer_holdback must be Decimal (never float — rate math)")
            update["offer_holdback"] = str(offer_holdback)
        if notes is not None:
            update["notes"] = notes
        if status != "pending" and current.responded_at is None:
            update["responded_at"] = now.isoformat()

        try:
            result = (
                get_supabase()
                .table("funder_note_submissions")
                .update(update)
                .eq("id", str(submission_id))
                .execute()
            )
        except Exception as exc:
            _log.error(
                "funder_note_submissions.update_failed id=%s status=%s",
                submission_id,
                status,
            )
            raise FunderNoteSubmissionWriteError(
                f"failed to update funder_note_submission {submission_id}"
            ) from exc
        updated_rows = cast(list[dict[str, Any]], result.data or [])
        if not updated_rows:
            raise FunderNoteSubmissionWriteError(
                f"supabase update returned no row for funder_note_submission {submission_id}"
            )
        return _row_from_dict(updated_rows[0])


# ---------------------------------------------------------------------------
# Row encoders / decoders
# ---------------------------------------------------------------------------


def _row_to_payload(r: FunderNoteSubmissionRow) -> dict[str, Any]:
    """Encode for ``supabase.table('funder_note_submissions').insert``."""

    def _dec_or_none(v: Decimal | None) -> str | None:
        return str(v) if v is not None else None

    def _dt_or_none(v: datetime | None) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "id": str(r.id),
        "merchant_id": str(r.merchant_id),
        "funder_id": str(r.funder_id),
        "submitted_at": r.submitted_at.isoformat(),
        "submitted_by": r.submitted_by,
        "status": r.status,
        "offer_amount": _dec_or_none(r.offer_amount),
        "offer_factor": _dec_or_none(r.offer_factor),
        "offer_holdback": _dec_or_none(r.offer_holdback),
        "funder_note": r.funder_note,
        "responded_at": _dt_or_none(r.responded_at),
        "notes": r.notes,
    }


def _row_from_dict(row: dict[str, Any]) -> FunderNoteSubmissionRow:
    """Decode a Postgres row dict to a FunderNoteSubmissionRow."""

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

    submitted_at = _dt("submitted_at")
    if submitted_at is None:
        raise FunderNoteSubmissionWriteError("supabase row missing required submitted_at column")

    return FunderNoteSubmissionRow(
        id=UUID(row["id"]),
        merchant_id=UUID(row["merchant_id"]),
        funder_id=UUID(row["funder_id"]),
        submitted_at=submitted_at,
        submitted_by=row["submitted_by"],
        status=row.get("status") or "pending",
        offer_amount=_dec("offer_amount"),
        offer_factor=_dec("offer_factor"),
        offer_holdback=_dec("offer_holdback"),
        funder_note=row.get("funder_note"),
        responded_at=_dt("responded_at"),
        notes=row.get("notes"),
        created_at=_dt("created_at"),
        updated_at=_dt("updated_at"),
    )


__all__ = [
    "FunderNoteSubmissionNotFoundError",
    "FunderNoteSubmissionRepository",
    "FunderNoteSubmissionWriteError",
    "InMemoryFunderNoteSubmissionRepository",
    "SupabaseFunderNoteSubmissionRepository",
]
