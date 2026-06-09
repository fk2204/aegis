"""Merchant-scope shadow-signal audit-trail helper (U22 — migration 044).

U15 (commit ``88f1e9b``) wired the U12 cross-statement detector into the
upload worker, populating ``PipelineResult.cross_statement_patterns:
list[Pattern]`` in memory. The U15 agent flagged the persistence
decision as a follow-up:

    "Persisting cross_statement_patterns (decision between
     pattern_analysis.shadow_patterns vs a new merchants.shadow_signals
     channel) — left for a follow-up."

This module closes that loop with a NEW merchant-keyed
``merchants_shadow_signals`` table (migration 044). The rationale lives
in the migration header — TL;DR: pattern_analysis.shadow_patterns is
document-scope (one row inside an analyses.pattern_analysis JSONB binds
to ONE document). Cross-statement signals are cross-document; pushing
them into pattern_analysis would either double-write the signal into
BOTH documents or silently drop one half.

Two implementations of the ``MerchantShadowSignalRepository`` Protocol
mirror ``compliance/transmission.py`` and
``compliance/render_events.py``:

  * ``InMemoryMerchantShadowSignalRepository`` — list-backed; used by
    tests and the in-memory backend.
  * ``SupabaseMerchantShadowSignalRepository`` — writes one row per
    ``record()`` call. Insert failure raises
    ``MerchantShadowSignalWriteError`` so callers can decide whether to
    abort the upload (the U22 worker hook chooses to log + carry on so
    a Supabase blip doesn't fail the parse — see ``workers.py``).

Audit-log coupling
------------------
``record_shadow_signal(...)`` writes the shadow-signal row AND a paired
``audit_log`` row with ``action='shadow_signal_detected'``, subject is
the merchant. Per CLAUDE.md PII rules: the audit row carries the signal
CODE + severity + source_document_id ONLY — NEVER the holder string
that may live in ``detail`` (the U12 detector's related_account_suspected
detail format is ``holder={raw}:existing_last4={set}:new_last4={one}``,
which embeds PII). The audit_log row stays code-only so the durable
trail is PII-clean even if a future change accidentally routes the
holder string into a different field.

Shadow contract
---------------
Per CLAUDE.md "Decision-boundary changes — deliberate + shadow-first"
+ the U12 invariant: every signal written by ``record_shadow_signal``
through the U15 worker hook MUST carry ``signal_severity=0``. The
column exists at SMALLINT so a future operator-validated flip via
env-var doesn't need a schema change, but no caller in this commit
writes severity > 0.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from aegis.audit import AuditLog
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


class MerchantShadowSignalWriteError(RuntimeError):
    """Raised when a shadow-signal row could not be persisted.

    Mirrors ``DisclosureRenderEventWriteError`` / ``AuditWriteError``
    semantics: a write failure halts the calling operation rather than
    silently continuing. The U22 worker hook chooses to swallow this
    via try/except so a Supabase blip never fails the upload — the
    parse + persist already succeeded by the time the hook fires, and
    the cross-statement Pattern list is informational shadow data per
    the U12 contract. See ``aegis.workers._persist_cross_statement_signals``
    for the swallow rationale.
    """


class MerchantShadowSignalRecord(BaseModel):
    """One shadow-signal row. Pydantic so callers cannot pass loose dicts."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: UUID
    merchant_id: UUID
    signal_code: str
    signal_severity: int
    detail: str | None
    source_document_id: UUID | None
    source_ids: list[UUID]
    metadata: dict[str, Any] | None
    detected_at: datetime
    detected_by: str | None


class MerchantShadowSignalRepository(Protocol):
    """Append-only interface. Implementations must raise on write failure."""

    def record(
        self,
        *,
        merchant_id: UUID,
        signal_code: str,
        signal_severity: int,
        detail: str | None,
        source_document_id: UUID | None,
        source_ids: list[UUID],
        metadata: dict[str, Any] | None,
        detected_by: str | None,
        detected_at: datetime | None = None,
    ) -> MerchantShadowSignalRecord: ...

    def list_by_merchant(
        self,
        *,
        merchant_id: UUID,
        limit: int = 50,
    ) -> list[MerchantShadowSignalRecord]:
        """Return shadow signals for one merchant, newest first.

        Drives the dossier "Merchant-level shadow signals" section
        rendered by ``merchant_detail_dossier.html.j2``. Bounded
        ``limit`` keeps the read predictable even when a merchant
        accumulates many shadow signals over time.
        """

    def list_by_code(
        self,
        *,
        signal_code: str,
        limit: int = 50,
    ) -> list[MerchantShadowSignalRecord]:
        """Return shadow signals matching ``signal_code``, newest first.

        Drives the future operator triage roll-up ("show me every
        duplicate_pdf_upload across all merchants in the last week").
        """

    def list_in_window(
        self,
        *,
        from_date: date,
        to_date: date,
        signal_code: str | None = None,
        merchant_id: UUID | None = None,
        limit: int = 500,
    ) -> list[MerchantShadowSignalRecord]:
        """Return shadow signals whose ``detected_at`` falls in the window.

        Drives the U24 ``/ui/shadow-signals`` cross-merchant view + the
        ``/ui/triage`` aggregator. ``to_date`` is inclusive — implementations
        expand it to ``to_date + 1 day at 00:00`` (or equivalent) so a row
        stamped at ``23:59:59`` on ``to_date`` is included. Sorted
        newest-first.

        ``signal_code`` filters to a single code when set; ``None`` returns
        all codes. ``merchant_id`` filters to a single merchant when set;
        ``None`` returns all merchants. Both apply server-side on the
        Supabase backend.
        """


def _validate_severity(severity: int) -> int:
    """Defensive bounds check. SMALLINT range is well below int max but
    a negative severity is a caller bug — the U12 detector emits 0 and
    a future operator-validated flip would emit 1..100.
    """
    if severity < 0:
        raise ValueError(f"signal_severity must be >= 0, got {severity!r}")
    return severity


def _normalize_code(code: str) -> str:
    """Trim + reject empty. Signal codes are matched literal-equal by
    the dossier humanizer and the triage roll-up, so a stray whitespace
    would silently desync the surfaces."""
    cleaned = (code or "").strip()
    if not cleaned:
        raise ValueError("signal_code must not be empty")
    if len(cleaned) > 64:
        raise ValueError(f"signal_code exceeds 64 chars (got {len(cleaned)})")
    return cleaned


class InMemoryMerchantShadowSignalRepository:
    """List-backed implementation. Used by tests and the memory backend."""

    def __init__(self) -> None:
        self.rows: list[MerchantShadowSignalRecord] = []

    def record(
        self,
        *,
        merchant_id: UUID,
        signal_code: str,
        signal_severity: int,
        detail: str | None,
        source_document_id: UUID | None,
        source_ids: list[UUID],
        metadata: dict[str, Any] | None,
        detected_by: str | None,
        detected_at: datetime | None = None,
    ) -> MerchantShadowSignalRecord:
        record = MerchantShadowSignalRecord(
            id=uuid4(),
            merchant_id=merchant_id,
            signal_code=_normalize_code(signal_code),
            signal_severity=_validate_severity(signal_severity),
            detail=detail,
            source_document_id=source_document_id,
            source_ids=list(source_ids),
            metadata=metadata,
            detected_at=detected_at or datetime.now(UTC),
            detected_by=detected_by,
        )
        self.rows.append(record)
        return record

    def list_by_merchant(
        self,
        *,
        merchant_id: UUID,
        limit: int = 50,
    ) -> list[MerchantShadowSignalRecord]:
        matches = [r for r in self.rows if r.merchant_id == merchant_id]
        matches.sort(key=lambda r: r.detected_at, reverse=True)
        return matches[: max(0, limit)]

    def list_by_code(
        self,
        *,
        signal_code: str,
        limit: int = 50,
    ) -> list[MerchantShadowSignalRecord]:
        norm = _normalize_code(signal_code)
        matches = [r for r in self.rows if r.signal_code == norm]
        matches.sort(key=lambda r: r.detected_at, reverse=True)
        return matches[: max(0, limit)]

    def list_in_window(
        self,
        *,
        from_date: date,
        to_date: date,
        signal_code: str | None = None,
        merchant_id: UUID | None = None,
        limit: int = 500,
    ) -> list[MerchantShadowSignalRecord]:
        norm = _normalize_code(signal_code) if signal_code is not None else None
        # Inclusive window — every wall-clock instant on ``to_date`` is
        # in-range, so the comparison is against ``to_date`` end-of-day.
        upper = datetime.combine(to_date, time.max, tzinfo=UTC)
        lower = datetime.combine(from_date, time.min, tzinfo=UTC)
        matches: list[MerchantShadowSignalRecord] = []
        for r in self.rows:
            ts = r.detected_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts < lower or ts > upper:
                continue
            if norm is not None and r.signal_code != norm:
                continue
            if merchant_id is not None and r.merchant_id != merchant_id:
                continue
            matches.append(r)
        matches.sort(key=lambda r: r.detected_at, reverse=True)
        return matches[: max(0, limit)]


class SupabaseMerchantShadowSignalRepository:
    """Persistence backed by Postgres ``merchants_shadow_signals`` table.

    Mirrors the in-memory contract; the table has no STORED retention
    column because shadow signals are operator-review history that
    outlives the documents that produced them.
    """

    def record(
        self,
        *,
        merchant_id: UUID,
        signal_code: str,
        signal_severity: int,
        detail: str | None,
        source_document_id: UUID | None,
        source_ids: list[UUID],
        metadata: dict[str, Any] | None,
        detected_by: str | None,
        detected_at: datetime | None = None,
    ) -> MerchantShadowSignalRecord:
        norm_code = _normalize_code(signal_code)
        severity = _validate_severity(signal_severity)
        ts = detected_at or datetime.now(UTC)

        payload: dict[str, Any] = {
            "merchant_id": str(merchant_id),
            "signal_code": norm_code,
            "signal_severity": severity,
            "detail": detail,
            "source_document_id": (
                str(source_document_id) if source_document_id is not None else None
            ),
            "source_ids": [str(sid) for sid in source_ids],
            "metadata": metadata,
            "detected_at": ts.isoformat(),
            "detected_by": detected_by,
        }

        try:
            result = (
                get_supabase()
                .table("merchants_shadow_signals")
                .insert(payload)
                .execute()
            )
        except Exception as exc:
            _log.error(
                "merchants.shadow_signal.write_failed code=%s merchant_id=%s",
                norm_code,
                merchant_id,
            )
            raise MerchantShadowSignalWriteError(
                f"failed to record merchant shadow signal code={norm_code}"
            ) from exc

        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise MerchantShadowSignalWriteError(
                "supabase insert returned no row for merchant shadow signal"
            )
        return _row_to_record(rows[0])

    def list_by_merchant(
        self,
        *,
        merchant_id: UUID,
        limit: int = 50,
    ) -> list[MerchantShadowSignalRecord]:
        try:
            result = (
                get_supabase()
                .table("merchants_shadow_signals")
                .select("*")
                .eq("merchant_id", str(merchant_id))
                .order("detected_at", desc=True)
                .limit(max(1, limit))
                .execute()
            )
        except Exception:
            _log.warning(
                "merchants.shadow_signal.list_by_merchant_failed merchant_id=%s",
                merchant_id,
            )
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return [_row_to_record(r) for r in rows]

    def list_by_code(
        self,
        *,
        signal_code: str,
        limit: int = 50,
    ) -> list[MerchantShadowSignalRecord]:
        norm = _normalize_code(signal_code)
        try:
            result = (
                get_supabase()
                .table("merchants_shadow_signals")
                .select("*")
                .eq("signal_code", norm)
                .order("detected_at", desc=True)
                .limit(max(1, limit))
                .execute()
            )
        except Exception:
            _log.warning(
                "merchants.shadow_signal.list_by_code_failed code=%s",
                norm,
            )
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return [_row_to_record(r) for r in rows]

    def list_in_window(
        self,
        *,
        from_date: date,
        to_date: date,
        signal_code: str | None = None,
        merchant_id: UUID | None = None,
        limit: int = 500,
    ) -> list[MerchantShadowSignalRecord]:
        norm = _normalize_code(signal_code) if signal_code is not None else None
        # Inclusive window — Postgres TIMESTAMPTZ comparison against
        # ``to_date`` alone would exclude same-day rows after 00:00, so
        # the upper bound is end-of-day ISO. Mirrors the U16 repository.
        lower = datetime.combine(from_date, time.min, tzinfo=UTC).isoformat()
        upper = datetime.combine(to_date, time.max, tzinfo=UTC).isoformat()
        try:
            builder = (
                get_supabase()
                .table("merchants_shadow_signals")
                .select("*")
                .gte("detected_at", lower)
                .lte("detected_at", upper)
                .order("detected_at", desc=True)
                .limit(max(1, limit))
            )
            if norm is not None:
                builder = builder.eq("signal_code", norm)
            if merchant_id is not None:
                builder = builder.eq("merchant_id", str(merchant_id))
            result = builder.execute()
        except Exception:
            _log.warning(
                "merchants.shadow_signal.list_in_window_failed "
                "from=%s to=%s code=%s merchant_id=%s",
                from_date,
                to_date,
                norm,
                merchant_id,
            )
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return [_row_to_record(r) for r in rows]


def _row_to_record(row: dict[str, Any]) -> MerchantShadowSignalRecord:
    """Map a Supabase row dict back into the Pydantic record."""
    detected_at_raw = row["detected_at"]
    detected_at = (
        detected_at_raw
        if isinstance(detected_at_raw, datetime)
        else datetime.fromisoformat(str(detected_at_raw).replace("Z", "+00:00"))
    )
    raw_source_ids = row.get("source_ids") or []
    source_ids = [
        UUID(sid) if not isinstance(sid, UUID) else sid for sid in raw_source_ids
    ]
    return MerchantShadowSignalRecord(
        id=UUID(row["id"]) if not isinstance(row["id"], UUID) else row["id"],
        merchant_id=(
            UUID(row["merchant_id"])
            if not isinstance(row["merchant_id"], UUID)
            else row["merchant_id"]
        ),
        signal_code=row["signal_code"],
        signal_severity=int(row["signal_severity"]),
        detail=row.get("detail"),
        source_document_id=(
            UUID(row["source_document_id"])
            if row.get("source_document_id")
            and not isinstance(row["source_document_id"], UUID)
            else row.get("source_document_id")
        ),
        source_ids=source_ids,
        metadata=row.get("metadata"),
        detected_at=detected_at,
        detected_by=row.get("detected_by"),
    )


def record_shadow_signal(
    repo: MerchantShadowSignalRepository,
    audit: AuditLog,
    *,
    merchant_id: UUID,
    signal_code: str,
    signal_severity: int,
    detail: str | None,
    source_document_id: UUID | None,
    source_ids: list[UUID],
    metadata: dict[str, Any] | None,
    detected_by: str | None,
    actor: str = "worker",
    actor_email: str | None = None,
    detected_at: datetime | None = None,
) -> MerchantShadowSignalRecord:
    """Write a shadow-signal row AND a paired ``audit_log`` row.

    PII discipline: the audit ``details`` payload carries the signal
    CODE + severity + source_document_id ONLY. The Pattern.detail
    string (which for ``related_account_suspected`` embeds the raw
    statement-literal ``account_holder`` per the U12 detector) is
    recorded on the shadow-signal row itself but is NOT duplicated into
    the audit ``details`` dict — see migration 044 header for the full
    rationale.

    Per CLAUDE.md "audit-write failures FAIL the operation" — the
    shadow-signal row is written FIRST so a row-write failure surfaces
    before the audit row commits; if the audit_log write subsequently
    raises, the caller sees the audit failure and aborts (matching the
    rest of the compliance write surface). The U22 worker hook wraps
    THIS call in a try/except so neither failure mode aborts the upload
    — the parse + persist already succeeded by then, and the
    cross-statement Pattern list is informational shadow data.
    """
    record = repo.record(
        merchant_id=merchant_id,
        signal_code=signal_code,
        signal_severity=signal_severity,
        detail=detail,
        source_document_id=source_document_id,
        source_ids=source_ids,
        metadata=metadata,
        detected_by=detected_by,
        detected_at=detected_at,
    )
    # PII canary contract: code + severity + source_document_id only.
    # NEVER duplicate ``detail`` here — for related_account_suspected
    # that string carries the raw account_holder. The audit_log row
    # stays code-only so a future audit-log replay can never leak the
    # holder through this path.
    audit_details: dict[str, Any] = {
        "code": record.signal_code,
        "severity": record.signal_severity,
        "source_document_id": (
            str(record.source_document_id)
            if record.source_document_id is not None
            else None
        ),
    }
    audit.record(
        actor=actor,
        actor_email=actor_email,
        action="shadow_signal_detected",
        subject_type="merchant",
        subject_id=merchant_id,
        details=audit_details,
    )
    return record


__all__ = [
    "InMemoryMerchantShadowSignalRepository",
    "MerchantShadowSignalRecord",
    "MerchantShadowSignalRepository",
    "MerchantShadowSignalWriteError",
    "SupabaseMerchantShadowSignalRepository",
    "record_shadow_signal",
]
