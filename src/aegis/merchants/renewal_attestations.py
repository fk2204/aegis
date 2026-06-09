"""Funder renewal-disclosure attestation audit-trail helper (U6).

R3.2 (commit 6102bbc) shipped the operator-visibility renewal calendar;
migration 039 added ``merchants.maturity_date``. The remaining gap was
operator-side capture for "the funder has confirmed they sent the
required pre-maturity disclosure on date X." This module owns the write
path for the ``funder_renewal_attestations`` table (migration 040) — one
row per operator attestation that the funder partner has transmitted the
required notice for a given merchant + maturity_date.

Per CLAUDE.md SCOPE NOTE + ``.claude/rules/compliance.md`` SCOPE NOTE,
AEGIS does NOT own the regulator-facing disclosure obligation — funder
partners do. The rows in this table record OPERATOR CLAIMS that the
funder has fulfilled their obligation; they are not themselves a
regulator-facing audit artifact.

Two implementations of the ``RenewalAttestationRepository`` Protocol:

  * ``InMemoryRenewalAttestationRepository`` — list-backed, used by tests
    and the in-memory backend.
  * ``SupabaseRenewalAttestationRepository`` — writes one row per
    ``record()`` call to Postgres. Insert failure raises so the calling
    pipeline can refuse to mark the attestation as captured.

The shape mirrors the schema verbatim. PII discipline: ``funder_name`` is
a counterparty identifier (not merchant PII) and is OK in the row;
``notes`` is operator free-text and may contain PII at the operator's
discretion (e.g. "confirmed via email from compliance@funderA.com") but
the audit-log ``details`` written alongside the row carries NEITHER
``business_name`` NOR ``owner_name`` — per the U6 spec, audit details
strip merchant PII.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from aegis.audit import AuditLog
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


# Same lead-day lookup the renewal accessor uses. Kept here as the
# authoritative source for ``applicable_statute`` resolution so the
# attestation row records the exact statute the operator-visibility
# calendar consulted at attestation time.
_STATE_STATUTE: dict[str, str] = {
    "CA": "CA SB 362 § 22806",
    "NY": "NY 23 NYCRR § 600.17",
}


class RenewalAttestationWriteError(RuntimeError):
    """Raised when a renewal-attestation row could not be persisted.

    Mirrors ``AuditWriteError`` semantics: a write failure halts the
    calling operation rather than letting the operator believe the
    attestation landed. Audit discipline (CLAUDE.md) forbids silent
    log-and-continue on persistence failures.
    """


class RenewalAttestationConflictError(ValueError):
    """Raised when a duplicate (merchant_id, maturity_date, funder_name)
    attestation is attempted.

    Idempotency note: the U6 spec leaves the double-submit policy to the
    implementation. We chose 409-on-duplicate (return the existing row
    rather than silently inserting a second). Rationale: the audit
    discipline of CLAUDE.md plus operating-principle #1 (production writes
    require explicit operator approval per action) make "silently coalesce
    duplicate writes" a worse default than "tell the operator they
    already did this." See ``record_renewal_attestation`` for the call
    site that translates this into the HTTP response.
    """

    def __init__(
        self,
        *,
        merchant_id: UUID,
        maturity_date: date,
        funder_name: str,
        existing_id: UUID,
    ) -> None:
        self.merchant_id = merchant_id
        self.maturity_date = maturity_date
        self.funder_name = funder_name
        self.existing_id = existing_id
        super().__init__(
            f"renewal attestation already exists for merchant={merchant_id} "
            f"maturity={maturity_date.isoformat()} funder={funder_name!r} "
            f"(existing id={existing_id})"
        )


class RenewalAttestationRecord(BaseModel):
    """One operator attestation. Pydantic so callers cannot pass loose dicts."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: UUID
    merchant_id: UUID
    funder_name: str
    maturity_date: date
    disclosure_sent_at: date
    attested_by: str
    attested_at: datetime
    notes: str | None
    state: str
    applicable_statute: str | None
    metadata: dict[str, Any] | None


class RenewalAttestationRepository(Protocol):
    """Append-only interface. Implementations must raise on write failure."""

    def record(
        self,
        *,
        merchant_id: UUID,
        funder_name: str,
        maturity_date: date,
        disclosure_sent_at: date,
        attested_by: str,
        state: str,
        applicable_statute: str | None = None,
        notes: str | None = None,
        attested_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RenewalAttestationRecord: ...

    def find_for_renewal(
        self, *, merchant_id: UUID, maturity_date: date
    ) -> list[RenewalAttestationRecord]:
        """Return every attestation matching ``(merchant_id, maturity_date)``.

        Returned newest-first so the calling renewal-status accessor can
        treat the first row as "the latest funder claim." Empty list when
        no attestation has been captured for this (merchant, maturity)
        pair.
        """


def _normalize_state(state: str) -> str:
    """USPS 2-letter uppercase. Defensive — caller may pass mixed case."""
    upper = (state or "").upper()
    if len(upper) != 2:
        raise ValueError(f"state must be a 2-letter USPS code, got {state!r}")
    return upper


def _normalize_funder_name(name: str) -> str:
    """Trim + reject empty. Funder names are matched literal-equal."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("funder_name must not be empty")
    if len(cleaned) > 255:
        raise ValueError(f"funder_name exceeds 255 chars (got {len(cleaned)})")
    return cleaned


def resolve_applicable_statute(state: str) -> str | None:
    """Return the AEGIS-tracked statute for ``state``, or ``None``.

    Mirrors ``_STATE_DISCLOSURE_LEAD_DAYS`` in
    ``aegis.merchants.repository`` — CA + NY only today. Other states
    have no AEGIS-tracked renewal-disclosure deadline; an operator may
    still attest, but ``applicable_statute`` is recorded as ``None``.
    """
    return _STATE_STATUTE.get(_normalize_state(state))


class InMemoryRenewalAttestationRepository:
    """List-backed implementation. Used by tests and the memory backend."""

    def __init__(self) -> None:
        self.rows: list[RenewalAttestationRecord] = []

    def record(
        self,
        *,
        merchant_id: UUID,
        funder_name: str,
        maturity_date: date,
        disclosure_sent_at: date,
        attested_by: str,
        state: str,
        applicable_statute: str | None = None,
        notes: str | None = None,
        attested_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RenewalAttestationRecord:
        norm_state = _normalize_state(state)
        norm_funder = _normalize_funder_name(funder_name)
        # Conflict detection at the in-memory layer mirrors the Supabase
        # path so tests exercise the same branch.
        for existing in self.rows:
            if (
                existing.merchant_id == merchant_id
                and existing.maturity_date == maturity_date
                and existing.funder_name == norm_funder
            ):
                raise RenewalAttestationConflictError(
                    merchant_id=merchant_id,
                    maturity_date=maturity_date,
                    funder_name=norm_funder,
                    existing_id=existing.id,
                )
        record = RenewalAttestationRecord(
            id=uuid4(),
            merchant_id=merchant_id,
            funder_name=norm_funder,
            maturity_date=maturity_date,
            disclosure_sent_at=disclosure_sent_at,
            attested_by=attested_by,
            attested_at=attested_at or datetime.now(UTC),
            notes=(notes.strip() if notes is not None and notes.strip() else None),
            state=norm_state,
            applicable_statute=applicable_statute,
            metadata=metadata,
        )
        self.rows.append(record)
        return record

    def find_for_renewal(
        self, *, merchant_id: UUID, maturity_date: date
    ) -> list[RenewalAttestationRecord]:
        matches = [
            r
            for r in self.rows
            if r.merchant_id == merchant_id and r.maturity_date == maturity_date
        ]
        matches.sort(key=lambda r: r.attested_at, reverse=True)
        return matches


class SupabaseRenewalAttestationRepository:
    """Persistence backed by Postgres ``funder_renewal_attestations``."""

    def record(
        self,
        *,
        merchant_id: UUID,
        funder_name: str,
        maturity_date: date,
        disclosure_sent_at: date,
        attested_by: str,
        state: str,
        applicable_statute: str | None = None,
        notes: str | None = None,
        attested_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RenewalAttestationRecord:
        norm_state = _normalize_state(state)
        norm_funder = _normalize_funder_name(funder_name)
        ts = attested_at or datetime.now(UTC)

        # Conflict check before insert. The table has no UNIQUE constraint
        # on (merchant_id, maturity_date, funder_name) because the
        # operator may legitimately attest more than once for a renewal
        # in rare cases (e.g. funder retracted then re-sent the notice);
        # the route-level policy is "default to 409 on duplicate, let the
        # operator override via a future flag if needed." See
        # RenewalAttestationConflictError for the policy rationale.
        existing_rows = (
            get_supabase()
            .table("funder_renewal_attestations")
            .select("id")
            .eq("merchant_id", str(merchant_id))
            .eq("maturity_date", maturity_date.isoformat())
            .eq("funder_name", norm_funder)
            .limit(1)
            .execute()
        )
        if existing_rows.data:
            row = cast(dict[str, Any], existing_rows.data[0])
            raise RenewalAttestationConflictError(
                merchant_id=merchant_id,
                maturity_date=maturity_date,
                funder_name=norm_funder,
                existing_id=UUID(row["id"]),
            )

        payload: dict[str, Any] = {
            "merchant_id": str(merchant_id),
            "funder_name": norm_funder,
            "maturity_date": maturity_date.isoformat(),
            "disclosure_sent_at": disclosure_sent_at.isoformat(),
            "attested_by": attested_by,
            "attested_at": ts.isoformat(),
            "notes": notes.strip() if notes is not None and notes.strip() else None,
            "state": norm_state,
            "applicable_statute": applicable_statute,
            "metadata": metadata,
        }

        try:
            result = (
                get_supabase()
                .table("funder_renewal_attestations")
                .insert(payload)
                .execute()
            )
        except Exception as exc:
            _log.error(
                "renewal_attestation.write_failed merchant_id=%s maturity=%s",
                merchant_id,
                maturity_date.isoformat(),
            )
            raise RenewalAttestationWriteError(
                f"failed to record renewal attestation for merchant={merchant_id}"
            ) from exc

        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise RenewalAttestationWriteError(
                "supabase insert returned no row for renewal attestation"
            )
        row = rows[0]
        return _row_to_record(row)

    def find_for_renewal(
        self, *, merchant_id: UUID, maturity_date: date
    ) -> list[RenewalAttestationRecord]:
        try:
            result = (
                get_supabase()
                .table("funder_renewal_attestations")
                .select("*")
                .eq("merchant_id", str(merchant_id))
                .eq("maturity_date", maturity_date.isoformat())
                .order("attested_at", desc=True)
                .execute()
            )
        except Exception:
            _log.warning(
                "renewal_attestation.lookup_failed merchant_id=%s maturity=%s",
                merchant_id,
                maturity_date.isoformat(),
            )
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return [_row_to_record(r) for r in rows]


def _row_to_record(row: dict[str, Any]) -> RenewalAttestationRecord:
    def _parse_date(v: object) -> date:
        if isinstance(v, date) and not isinstance(v, datetime):
            return v
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, str):
            return date.fromisoformat(v)
        raise ValueError(f"cannot parse date from {v!r}")

    def _parse_dt(v: object) -> datetime:
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        raise ValueError(f"cannot parse datetime from {v!r}")

    return RenewalAttestationRecord(
        id=UUID(row["id"]),
        merchant_id=UUID(row["merchant_id"]),
        funder_name=row["funder_name"],
        maturity_date=_parse_date(row["maturity_date"]),
        disclosure_sent_at=_parse_date(row["disclosure_sent_at"]),
        attested_by=row["attested_by"],
        attested_at=_parse_dt(row["attested_at"]),
        notes=row.get("notes"),
        state=row["state"],
        applicable_statute=row.get("applicable_statute"),
        metadata=row.get("metadata"),
    )


def record_renewal_attestation(
    repo: RenewalAttestationRepository,
    audit: AuditLog,
    *,
    merchant_id: UUID,
    funder_name: str,
    maturity_date: date,
    disclosure_sent_at: date,
    attested_by: str,
    state: str,
    actor_email: str | None = None,
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RenewalAttestationRecord:
    """Persist one attestation + write the audit_log row in one call.

    PII discipline: the audit ``details`` payload carries the funder name
    + statute + dates ONLY — never the merchant ``business_name`` or
    ``owner_name``. The operator's free-form ``notes`` field is
    operator-discretion and may carry low-risk references (email
    addresses); it's recorded on the attestation row itself but is NOT
    duplicated into audit details so the audit log stays PII-clean.

    On a duplicate (merchant_id, maturity_date, funder_name) the repo
    raises ``RenewalAttestationConflictError``. The caller (the UI POST
    handler) translates that to an HTTP 409 — see the route docstring
    for the rationale.

    On audit-write failure the repository row IS already written (audit
    runs after persistence) — this is the same audit-discipline tradeoff
    the rest of the codebase makes: a row without an audit entry is
    visible-but-flagged via the missing audit, whereas an audit entry
    without a row would be a false claim of state change.
    """
    applicable_statute = resolve_applicable_statute(state)
    record = repo.record(
        merchant_id=merchant_id,
        funder_name=funder_name,
        maturity_date=maturity_date,
        disclosure_sent_at=disclosure_sent_at,
        attested_by=attested_by,
        state=state,
        applicable_statute=applicable_statute,
        notes=notes,
        metadata=metadata,
    )
    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="renewal_disclosure_attested",
        subject_type="merchant",
        subject_id=merchant_id,
        details={
            "funder_name": record.funder_name,
            "maturity_date": record.maturity_date.isoformat(),
            "disclosure_sent_at": record.disclosure_sent_at.isoformat(),
            "state": record.state,
            "applicable_statute": record.applicable_statute,
            "attestation_id": str(record.id),
        },
    )
    return record


__all__ = [
    "InMemoryRenewalAttestationRepository",
    "RenewalAttestationConflictError",
    "RenewalAttestationRecord",
    "RenewalAttestationRepository",
    "RenewalAttestationWriteError",
    "SupabaseRenewalAttestationRepository",
    "record_renewal_attestation",
    "resolve_applicable_statute",
]
