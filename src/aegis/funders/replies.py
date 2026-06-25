"""Funder reply ingestion + outcome stamping (mp Phase 10 / Stage 2D-main).

Captures every funder reply (inbound email webhook, or operator-paste
from the dashboard) tied to a deal. Each reply lands in the
``funder_replies`` table and, per the plan's refinement (5),
deterministically stamps the matching override's ``outcome`` field
exactly once.

Outcome-stamping rules (refinement 5):

1. Funder reply arrives with a matching OPEN override
   (``outcome IS NULL``):
     - Persist the funder_replies row.
     - Stamp the override: outcome + outcome_recorded_at set.
2. Funder reply arrives with a matching ALREADY-STAMPED override:
     - Persist the funder_replies row.
     - Do NOT overwrite the existing stamp (idempotency).
3. Funder reply arrives BEFORE the matching override exists:
     - Persist the funder_replies row now.
     - At override-creation time, the override module queries this
       module for the most-recent matching reply and stamps the
       override with its outcome (see ``stamp_override_from_replies``).

Status-to-outcome mapping (one place, never branch on raw status
elsewhere):

    funder_replies.status='approved'  -> override.outcome='funded'
    funder_replies.status='declined'  -> override.outcome='declined_by_funder'
    funder_replies.status='countered' -> NO outcome stamp yet
        (the counter requires operator acceptance/decline first;
         once they record an accept, the FOLLOW-UP reply will be
         the one that stamps the override).

The validation gate for ``status='approved'`` ties out the offered
terms: amount * factor == payback (+/- $0.01), and term_days *
daily_payment ~= payback (+/- $0.01) when daily_payment is present.
A failing reconcile is NOT silently fixed; the row persists with
parsed_confidence lowered to 0 and a flag surfaced so the operator
hand-corrects before stamping happens.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis.audit import AuditLog
from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.money import Money

_log = get_logger(__name__)


ReplyStatus = Literal["approved", "declined", "countered"]
IngestSource = Literal["webhook", "operator_paste"]

# Operator-recorded outcome on a funder submission. Distinct from
# ``ReplyStatus`` — the email-parse path can never produce a
# 'no_response' (by definition there's no email when the funder
# ghosted). Surfaced on the "Record outcome" modal on the dossier's
# § 5½ funder submissions table.
ReplyOutcome = Literal["approved", "declined", "countered", "no_response"]

# Outcomes for which numeric offer fields are meaningful. 'declined'
# and 'no_response' rows must have NULL amount / factor / term — the
# DB CHECK constraint (migration 071) enforces this; we mirror it in
# the write path so a bug in the form handler fails fast.
_OUTCOMES_WITH_OFFER: frozenset[str] = frozenset({"approved", "countered"})


# One place to maintain status -> override.outcome mapping. Adding a new
# status here requires updating the migration's CHECK constraint and the
# override.outcome CHECK in migration 017. A "countered" reply intentionally
# returns None — the counter requires operator acceptance before stamping.
STATUS_TO_OUTCOME: dict[ReplyStatus, str | None] = {
    "approved": "funded",
    "declined": "declined_by_funder",
    "countered": None,
}

# Reconciliation tolerance, $0.01 — matches the bank + processor parsers'
# math gates. Tightening here without updating the others creates drift;
# treat this as cross-module.
TOLERANCE = Decimal("0.01")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ReplyTerms(BaseModel):
    """Structured offer terms extracted from an approved reply.

    All money fields are ``Money`` (Decimal). Sparse — only the fields
    the parser confidently extracted are populated; everything else is
    None. The validator runs reconcile checks on whichever combination
    is present.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    amount: Money | None = None
    factor: Decimal | None = Field(default=None, ge=Decimal("1"), le=Decimal("2"))
    payback: Money | None = None
    term_days: int | None = Field(default=None, ge=1, le=730)
    daily_payment: Money | None = None
    holdback_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))

    def to_jsonable(self) -> dict[str, Any]:
        """Decimal-safe dict for jsonb storage. Sparse."""
        out: dict[str, Any] = {}
        for key in ("amount", "factor", "payback", "daily_payment", "holdback_pct"):
            v = getattr(self, key)
            if v is not None:
                out[key] = str(v)
        if self.term_days is not None:
            out["term_days"] = self.term_days
        return out


class FunderReplyPayload(BaseModel):
    """Inputs to ``ingest_reply``.

    Strict + frozen so a typo at the call site fails at construction
    time, before any DB write happens.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        frozen=True,
    )

    deal_id: UUID
    funder_id: UUID
    status: ReplyStatus
    raw_text: str = Field(min_length=1)
    ingested_via: IngestSource
    terms: ReplyTerms = Field(default_factory=ReplyTerms)
    parsed_confidence: int = Field(ge=0, le=100, default=0)
    received_at: datetime | None = None  # None -> DB default (NOW)


# Pydantic field annotations for the outcome columns. Mirror the DB
# shape from migration 071: numeric(14,2) for the offer amount,
# numeric(6,4) for the factor rate (range matches ReplyTerms.factor +
# the funder_note_submissions._FactorRate annotation).
_OutcomeAmount = Annotated[Decimal, Field(max_digits=14, decimal_places=2, gt=Decimal("0"))]
_OutcomeFactorRate = Annotated[
    Decimal,
    Field(max_digits=6, decimal_places=4, gt=Decimal("1"), le=Decimal("2")),
]
_OutcomeTermDays = Annotated[int, Field(ge=1, le=730)]


class FunderReplyOutcomePayload(BaseModel):
    """Operator-recorded outcome for a funder_note_submission.

    Strict + frozen so a typo at the call site fails at construction
    time, before any DB write happens. Distinct from
    ``FunderReplyPayload`` because the manual-outcome path captures
    operator intent (what the funder said per the operator) rather
    than parsing an email body — no ``raw_text`` / ``parsed_confidence``
    / math-reconcile gate.

    Field-by-field invariants (mirror the DB CHECK from migration 071):

      * ``outcome IN ('approved','declined','countered','no_response')``
        — enforced by the Literal type.
      * For ``outcome IN ('declined','no_response')`` the offer fields
        (``outcome_amount`` / ``outcome_factor_rate`` /
        ``outcome_term_days``) MUST be ``None``. A bug that sets them
        anyway raises at validation time so the write never reaches the
        DB CHECK.
      * ``outcome_recorded_by`` is the operator email — populated by the
        route from ``resolve_operator_email``. Required so the audit
        chain is unbroken.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        frozen=True,
    )

    deal_id: UUID
    funder_id: UUID
    outcome: ReplyOutcome
    outcome_amount: _OutcomeAmount | None = None
    outcome_factor_rate: _OutcomeFactorRate | None = None
    outcome_term_days: _OutcomeTermDays | None = None
    outcome_notes: str | None = Field(default=None, max_length=1000)
    outcome_recorded_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def _enforce_offer_field_invariant(self) -> FunderReplyOutcomePayload:
        # Mirror the DB CHECK from migration 071: declined / no_response
        # rows have no offer fields. Fail fast at the Pydantic layer
        # instead of bouncing off Postgres at insert time.
        if self.outcome not in _OUTCOMES_WITH_OFFER and (
            self.outcome_amount is not None
            or self.outcome_factor_rate is not None
            or self.outcome_term_days is not None
        ):
            raise ValueError(
                f"outcome={self.outcome!r} cannot carry offer fields "
                f"(amount / factor_rate / term_days must be None)"
            )
        return self


# ---------------------------------------------------------------------------
# Validation (deterministic; no LLM in this layer)
# ---------------------------------------------------------------------------


@dataclass
class ReplyValidationResult:
    """Math-reconcile gate outcome.

    ``passed`` is True iff ``failures`` is empty. ``warnings`` surface
    informational gaps that don't block persistence (e.g. missing
    optional fields). Same shape as the bank/processor validators.
    """

    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_reply(payload: FunderReplyPayload) -> ReplyValidationResult:
    """Reconcile the structured terms against themselves.

    For ``status='approved'``:
      - If amount + factor + payback all present: amount * factor must
        equal payback within $0.01.
      - If term_days + daily_payment + payback all present:
        term_days * daily_payment must approximate payback within $0.01.

    For ``declined`` / ``countered``: no math gate; structured terms
    are advisory.
    """
    failures: list[str] = []
    warnings: list[str] = []

    if payload.status != "approved":
        return ReplyValidationResult(passed=True)

    t = payload.terms

    if t.amount is not None and t.factor is not None and t.payback is not None:
        derived = (t.amount * t.factor).quantize(Decimal("0.01"))
        gap = abs(derived - t.payback)
        if gap > TOLERANCE:
            failures.append(
                f"amount_factor_payback_mismatch: {t.amount} * {t.factor} = "
                f"{derived} but payback = {t.payback} (gap {gap})"
            )
    elif payload.status == "approved":
        warnings.append("missing_terms_for_reconcile: amount/factor/payback incomplete")

    if t.term_days is not None and t.daily_payment is not None and t.payback is not None:
        derived = (Decimal(t.term_days) * t.daily_payment).quantize(Decimal("0.01"))
        gap = abs(derived - t.payback)
        if gap > TOLERANCE:
            failures.append(
                f"term_payment_payback_mismatch: {t.term_days} * {t.daily_payment} = "
                f"{derived} but payback = {t.payback} (gap {gap})"
            )

    return ReplyValidationResult(passed=not failures, failures=failures, warnings=warnings)


# ---------------------------------------------------------------------------
# Repository protocol + in-memory + Supabase impls
# ---------------------------------------------------------------------------


class FunderReplyRepository(Protocol):
    """Write + query interface for funder_replies + override stamping.

    Outcome-stamping matches by ``deal_id`` alone — overrides are
    per-deal (the operator's stance on AEGIS's recommendation), so
    whichever funder reply arrives first stamps the deal-level
    outcome. Subsequent replies persist but don't overwrite (refinement 5).
    """

    def insert_reply(self, payload: FunderReplyPayload, *, source_email_sha256: str) -> UUID:
        """Persist one funder_replies row; return its UUID."""

    def latest_open_override_for(self, deal_id: UUID) -> dict[str, Any] | None:
        """Return the most-recent UNSTAMPED override row for this deal
        (outcome IS NULL), or None. Used by ingest_reply to apply
        outcome stamps."""

    def latest_reply_for_deal(self, deal_id: UUID) -> dict[str, Any] | None:
        """Return the most-recent funder_replies row for this deal,
        or None. Used by override creation to back-stamp when the
        reply arrived first."""

    def stamp_override_outcome(
        self,
        override_id: UUID,
        *,
        outcome: str,
        stamped_at: datetime,
    ) -> bool:
        """Set override.outcome + outcome_recorded_at iff outcome is
        still NULL. Returns True if stamping occurred, False if the
        override was already stamped (idempotency)."""

    def insert_outcome(
        self,
        payload: FunderReplyOutcomePayload,
        *,
        recorded_at: datetime,
    ) -> UUID:
        """Persist a manual operator-recorded outcome row into
        ``funder_replies`` (migration 071 columns). Returns the new
        row id. Raises ``FunderReplyError`` on persistence failure so
        the calling route can surface the error to the operator
        rather than silently audit-logging an outcome that didn't
        land in the DB.
        """


class InMemoryFunderReplyRepository:
    """List-backed repository for tests + memory storage backend."""

    def __init__(self) -> None:
        self._replies: list[dict[str, Any]] = []
        # Overrides "table" — populated by tests or by the override
        # module's own InMemoryOverrideRepository. We use plain dicts
        # so the test fixtures can construct overrides without
        # importing the override-side Pydantic model.
        self._overrides: list[dict[str, Any]] = []

    # -- reply storage -------------------------------------------------------

    def insert_reply(self, payload: FunderReplyPayload, *, source_email_sha256: str) -> UUID:
        row_id = uuid4()
        row = {
            "id": str(row_id),
            "deal_id": str(payload.deal_id),
            "funder_id": str(payload.funder_id),
            "status": payload.status,
            "terms_json": payload.terms.to_jsonable(),
            "source_email_sha256": source_email_sha256,
            "parsed_confidence": payload.parsed_confidence,
            "raw_text": payload.raw_text,
            "ingested_via": payload.ingested_via,
            "received_at": (payload.received_at.isoformat() if payload.received_at else None),
        }
        self._replies.append(row)
        return row_id

    def replies(self) -> list[dict[str, Any]]:
        return list(self._replies)

    # -- override stamping ---------------------------------------------------

    def add_override(self, row: dict[str, Any]) -> None:
        """Test/fixture helper: seed an override into the in-memory store."""
        self._overrides.append(dict(row))

    def overrides(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._overrides]

    def latest_open_override_for(self, deal_id: UUID) -> dict[str, Any] | None:
        candidates = [
            r for r in self._overrides if r["deal_id"] == str(deal_id) and r.get("outcome") is None
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return dict(candidates[0])

    def latest_reply_for_deal(self, deal_id: UUID) -> dict[str, Any] | None:
        candidates = [r for r in self._replies if r["deal_id"] == str(deal_id)]
        if not candidates:
            return None
        candidates.sort(key=lambda r: r.get("received_at") or "", reverse=True)
        return dict(candidates[0])

    def stamp_override_outcome(
        self,
        override_id: UUID,
        *,
        outcome: str,
        stamped_at: datetime,
    ) -> bool:
        for row in self._overrides:
            if row["id"] == str(override_id):
                if row.get("outcome") is not None:
                    return False  # already stamped — refinement (5) rule
                row["outcome"] = outcome
                row["outcome_recorded_at"] = stamped_at.isoformat()
                return True
        return False

    def insert_outcome(
        self,
        payload: FunderReplyOutcomePayload,
        *,
        recorded_at: datetime,
    ) -> UUID:
        row_id = uuid4()
        # Mirror the SupabaseFunderReplyRepository.insert_outcome shape —
        # rows are dicts, Decimals stored as Decimal (the SQL layer in
        # production casts to numeric; the in-memory layer keeps them
        # native so portfolio analytics can do Decimal math without a
        # conversion hop).
        self._replies.append(
            {
                "id": str(row_id),
                "deal_id": str(payload.deal_id),
                "funder_id": str(payload.funder_id),
                # status is NULL for manual no_response outcome rows.
                # For approved/declined/countered we mirror the outcome
                # into status too so any read that still reaches for
                # ``status`` (e.g. ``_compute_funder_table`` before the
                # outcome wire-through lands) sees a sensible value.
                "status": payload.outcome if payload.outcome != "no_response" else None,
                "terms_json": {},
                "source_email_sha256": None,
                "parsed_confidence": None,
                "raw_text": None,
                "ingested_via": "operator_paste",
                "received_at": recorded_at.isoformat(),
                "outcome": payload.outcome,
                "outcome_amount": payload.outcome_amount,
                "outcome_factor_rate": payload.outcome_factor_rate,
                "outcome_term_days": payload.outcome_term_days,
                "outcome_notes": payload.outcome_notes,
                "outcome_recorded_at": recorded_at.isoformat(),
                "outcome_recorded_by": payload.outcome_recorded_by,
            }
        )
        return row_id


class SupabaseFunderReplyRepository:
    """Production repository backed by Postgres via supabase-py."""

    def insert_reply(self, payload: FunderReplyPayload, *, source_email_sha256: str) -> UUID:
        row_id = uuid4()
        body: dict[str, Any] = {
            "id": str(row_id),
            "deal_id": str(payload.deal_id),
            "funder_id": str(payload.funder_id),
            "status": payload.status,
            "terms_json": payload.terms.to_jsonable(),
            "source_email_sha256": source_email_sha256,
            "parsed_confidence": payload.parsed_confidence,
            "raw_text": payload.raw_text,
            "ingested_via": payload.ingested_via,
        }
        if payload.received_at is not None:
            body["received_at"] = payload.received_at.isoformat()
        try:
            get_supabase().table("funder_replies").insert(body).execute()
        except Exception as exc:
            _log.error(
                "funder_replies.insert_failed deal_id=%s funder_id=%s",
                payload.deal_id,
                payload.funder_id,
            )
            raise FunderReplyError(
                f"failed to insert funder reply for deal {payload.deal_id}"
            ) from exc
        return row_id

    def latest_open_override_for(self, deal_id: UUID) -> dict[str, Any] | None:
        try:
            result = (
                get_supabase()
                .table("overrides")
                .select("*")
                .eq("deal_id", str(deal_id))
                .is_("outcome", "null")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
        except Exception:
            _log.warning(
                "funder_replies.latest_open_override_query_failed deal_id=%s",
                deal_id,
            )
            return None
        data = result.data or []
        if not data:
            return None
        first = data[0]
        return dict(first) if isinstance(first, dict) else None

    def latest_reply_for_deal(self, deal_id: UUID) -> dict[str, Any] | None:
        try:
            result = (
                get_supabase()
                .table("funder_replies")
                .select("*")
                .eq("deal_id", str(deal_id))
                .order("received_at", desc=True)
                .limit(1)
                .execute()
            )
        except Exception:
            _log.warning("funder_replies.latest_reply_query_failed deal_id=%s", deal_id)
            return None
        data = result.data or []
        if not data:
            return None
        first = data[0]
        return dict(first) if isinstance(first, dict) else None

    def stamp_override_outcome(
        self,
        override_id: UUID,
        *,
        outcome: str,
        stamped_at: datetime,
    ) -> bool:
        # Conditional update: WHERE outcome IS NULL guards idempotency
        # at the DB layer so concurrent webhook + paste don't double-
        # stamp. Returning row count tells us whether the stamp landed.
        try:
            result = (
                get_supabase()
                .table("overrides")
                .update(
                    {
                        "outcome": outcome,
                        "outcome_recorded_at": stamped_at.isoformat(),
                    }
                )
                .eq("id", str(override_id))
                .is_("outcome", "null")
                .execute()
            )
        except Exception as exc:
            _log.error(
                "funder_replies.stamp_failed override_id=%s outcome=%s",
                override_id,
                outcome,
            )
            raise FunderReplyError(f"failed to stamp override {override_id}") from exc
        return bool(result.data)

    def insert_outcome(
        self,
        payload: FunderReplyOutcomePayload,
        *,
        recorded_at: datetime,
    ) -> UUID:
        row_id = uuid4()
        # Decimals serialized as str so Postgres receives exact text on
        # the numeric(14,2) / numeric(6,4) columns — never a binary-float
        # round-trip. Mirrors the funder_note_submissions repo posture.
        body: dict[str, Any] = {
            "id": str(row_id),
            "deal_id": str(payload.deal_id),
            "funder_id": str(payload.funder_id),
            "ingested_via": "operator_paste",
            "received_at": recorded_at.isoformat(),
            "outcome": payload.outcome,
            "outcome_amount": (
                str(payload.outcome_amount) if payload.outcome_amount is not None else None
            ),
            "outcome_factor_rate": (
                str(payload.outcome_factor_rate)
                if payload.outcome_factor_rate is not None
                else None
            ),
            "outcome_term_days": payload.outcome_term_days,
            "outcome_notes": payload.outcome_notes,
            "outcome_recorded_at": recorded_at.isoformat(),
            "outcome_recorded_by": payload.outcome_recorded_by,
        }
        # Mirror the outcome into status for approved/declined/countered
        # so legacy readers that still look at status see a sensible
        # value. no_response leaves status NULL — the DB CHECK on row
        # existence (mig 071 funder_replies_reply_or_outcome_check) is
        # satisfied by the non-NULL outcome alone.
        if payload.outcome != "no_response":
            body["status"] = payload.outcome
        try:
            get_supabase().table("funder_replies").insert(body).execute()
        except Exception as exc:
            _log.error(
                "funder_replies.insert_outcome_failed deal_id=%s funder_id=%s outcome=%s",
                payload.deal_id,
                payload.funder_id,
                payload.outcome,
            )
            raise FunderReplyError(
                f"failed to insert outcome row for deal {payload.deal_id}"
            ) from exc
        return row_id


class FunderReplyError(RuntimeError):
    """Raised when a funder_replies write fails after validation passed."""


# ---------------------------------------------------------------------------
# Ingestion + stamping (the public surface)
# ---------------------------------------------------------------------------


@dataclass
class IngestionResult:
    """Outcome of one ``ingest_reply`` call.

    ``reply_id`` is always populated (the reply landed). ``stamped_override_id``
    is set iff the reply found an open override and stamped it.
    ``validation_passed`` False means the reply persisted with a
    lowered ``parsed_confidence=0`` so the operator hand-corrects
    before stamping happens on a later attempt.
    """

    reply_id: UUID
    validation: ReplyValidationResult
    stamped_override_id: UUID | None = None


def ingest_reply(
    payload: FunderReplyPayload,
    *,
    repo: FunderReplyRepository,
    audit: AuditLog,
    now: datetime | None = None,
) -> IngestionResult:
    """Persist a funder reply and (idempotently) stamp matching override.

    Order:
      1. Validate (deterministic math gate; declined/countered skip math).
      2. Insert funder_replies row. Failure → ``FunderReplyError`` (caller
         retries; nothing else has happened yet).
      3. If status maps to an outcome (approved/declined): look up the
         most-recent OPEN override for (deal_id, funder_id). If found,
         stamp it. If no open override exists, persist the reply alone
         — the override-creation path will back-stamp when it lands.
      4. Audit ``funder_reply.ingested`` with structured details so the
         dashboard activity feed reflects the inbound capture.

    Idempotency:
      - Reply rows are append-only here (one per inbound message).
      - Override stamps are conditional ``WHERE outcome IS NULL`` at
        the repository layer — concurrent webhook + paste calls don't
        double-stamp.
    """
    if now is None:
        now = datetime.now().astimezone()
    source_hash = hashlib.sha256(payload.raw_text.encode("utf-8")).hexdigest()

    # 1) Validate.
    validation = validate_reply(payload)
    confidence = payload.parsed_confidence if validation.passed else 0
    # Re-pack with the (possibly lowered) confidence. Frozen Pydantic,
    # so we build a new payload — don't mutate.
    adjusted = payload.model_copy(update={"parsed_confidence": confidence})

    # 2) Insert.
    reply_id = repo.insert_reply(adjusted, source_email_sha256=source_hash)

    # 3) Maybe stamp.
    stamped_override_id: UUID | None = None
    target_outcome = STATUS_TO_OUTCOME[payload.status]
    if target_outcome is not None and validation.passed:
        open_override = repo.latest_open_override_for(payload.deal_id)
        if open_override is not None:
            override_id = UUID(open_override["id"])
            stamped = repo.stamp_override_outcome(
                override_id, outcome=target_outcome, stamped_at=now
            )
            if stamped:
                stamped_override_id = override_id

    # 4) Audit.
    audit.record(
        actor="api",
        action="funder_reply.ingested",
        subject_type="deal",
        subject_id=payload.deal_id,
        details={
            "reply_id": str(reply_id),
            "funder_id": str(payload.funder_id),
            "status": payload.status,
            "ingested_via": payload.ingested_via,
            "validation_passed": validation.passed,
            "failure_count": len(validation.failures),
            "parsed_confidence": confidence,
            "stamped_override_id": (str(stamped_override_id) if stamped_override_id else None),
            "source_email_sha256": source_hash,
        },
    )
    if not validation.passed:
        _log.warning(
            "funder_reply.validation_failed deal_id=%s funder_id=%s failures=%s",
            payload.deal_id,
            payload.funder_id,
            validation.failures,
        )

    return IngestionResult(
        reply_id=reply_id,
        validation=validation,
        stamped_override_id=stamped_override_id,
    )


def record_outcome(
    payload: FunderReplyOutcomePayload,
    *,
    repo: FunderReplyRepository,
    audit: AuditLog,
    now: datetime | None = None,
) -> UUID:
    """Persist a manual operator-recorded outcome and audit it.

    Order:
      1. Insert the funder_replies row (writes the new outcome columns
         from migration 071).
      2. Write the ``funder_reply.outcome_recorded`` audit row. Per
         CLAUDE.md Auditability: an audit-write failure FAILS the
         operation. The audit row is written AFTER the insert because
         it needs the reply_id; if the audit row write fails, the
         resulting state has a recorded outcome with no audit trail —
         which is exactly what we want to fail loudly rather than
         silently log-and-continue. The caller surfaces the
         ``AuditWriteError`` to the operator.

    Returns the new reply_id so the route can include it in the HTMX
    response and the audit details payload.

    Unlike ``ingest_reply`` this path does NOT touch the overrides
    table. The override-stamping flow is exclusive to the email-parse
    path (refinement 5 — funder emails stamp overrides; operator
    captures of an outcome describe the funder_note_submission row,
    not the AEGIS-vs-operator override).
    """
    if now is None:
        now = datetime.now().astimezone()
    reply_id = repo.insert_outcome(payload, recorded_at=now)
    audit.record(
        actor="dashboard",
        actor_email=payload.outcome_recorded_by,
        action="funder_reply.outcome_recorded",
        subject_type="deal",
        subject_id=payload.deal_id,
        details={
            "reply_id": str(reply_id),
            "funder_id": str(payload.funder_id),
            "outcome": payload.outcome,
            "outcome_amount": (
                str(payload.outcome_amount) if payload.outcome_amount is not None else None
            ),
            "outcome_factor_rate": (
                str(payload.outcome_factor_rate)
                if payload.outcome_factor_rate is not None
                else None
            ),
            "outcome_term_days": payload.outcome_term_days,
            "notes_chars": len(payload.outcome_notes) if payload.outcome_notes else 0,
        },
    )
    return reply_id


def stamp_override_from_replies(
    *,
    override_id: UUID,
    deal_id: UUID,
    repo: FunderReplyRepository,
    audit: AuditLog,
    now: datetime | None = None,
) -> UUID | None:
    """Back-stamp an override that was created AFTER its funder reply.

    Looks up the most-recent funder reply for ``deal_id``; if its
    status maps to an outcome (approved/declined), stamps the
    override exactly once. Returns the override_id on stamp, None on
    no-op.

    Per refinement (5): this is the symmetric counterpart to
    ``ingest_reply``'s in-line stamping. Either side that arrives
    second performs the stamp; the override module calls this at
    override creation time.
    """
    if now is None:
        now = datetime.now().astimezone()
    reply = repo.latest_reply_for_deal(deal_id)
    if reply is None:
        return None
    status = reply.get("status")
    if status not in STATUS_TO_OUTCOME:
        return None
    outcome = STATUS_TO_OUTCOME[status]
    if outcome is None:
        return None
    stamped = repo.stamp_override_outcome(override_id, outcome=outcome, stamped_at=now)
    if not stamped:
        return None
    audit.record(
        actor="api",
        action="override.outcome_back_stamped",
        subject_type="deal",
        subject_id=deal_id,
        details={
            "override_id": str(override_id),
            "funder_id": reply.get("funder_id"),
            "outcome": outcome,
            "reply_id": reply.get("id"),
        },
    )
    return override_id


# ---------------------------------------------------------------------------
# JSON parsing helper for the operator-paste path
# ---------------------------------------------------------------------------


def parse_terms_from_json(data: dict[str, Any]) -> ReplyTerms:
    """Build ``ReplyTerms`` from a JSON-ish dict.

    The operator-paste endpoint accepts a structured form OR a raw
    JSON blob (e.g. what the LLM extractor returns). Numeric values
    cast safely to Decimal via str(...) — never float.
    """
    out: dict[str, Any] = {}
    for key in ("amount", "factor", "payback", "daily_payment", "holdback_pct"):
        if key in data and data[key] is not None:
            out[key] = str(data[key])
    if "term_days" in data and data["term_days"] is not None:
        out["term_days"] = int(data["term_days"])
    return ReplyTerms.model_validate(out)


def parse_terms_from_blob(raw: str) -> ReplyTerms:
    """Parse a JSON string into ReplyTerms. Empty / non-JSON → empty terms."""
    if not raw.strip():
        return ReplyTerms()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return ReplyTerms()
    if not isinstance(loaded, dict):
        return ReplyTerms()
    return parse_terms_from_json(loaded)


__all__ = [
    "STATUS_TO_OUTCOME",
    "TOLERANCE",
    "FunderReplyError",
    "FunderReplyOutcomePayload",
    "FunderReplyPayload",
    "FunderReplyRepository",
    "InMemoryFunderReplyRepository",
    "IngestSource",
    "IngestionResult",
    "ReplyOutcome",
    "ReplyStatus",
    "ReplyTerms",
    "ReplyValidationResult",
    "SupabaseFunderReplyRepository",
    "ingest_reply",
    "parse_terms_from_blob",
    "parse_terms_from_json",
    "record_outcome",
    "stamp_override_from_replies",
    "validate_reply",
]
