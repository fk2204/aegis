"""Operator-override capture (mp Phase 10 / Stage 2D-main).

Persists one row to the ``overrides`` table (migration 017 +
extension migration 072) each time the operator disagrees with
AEGIS's recommendation on a specific decision. The override is
pinned to a ``decision_id`` (when one exists) so the snapshot
table + override table together answer the regulator-defense
question "what did AEGIS recommend, what did the operator do, and
what happened?".

Two write paths coexist:

  * ``record_override`` — original route
    (``POST /ui/decisions/{decision_id}/override``). Requires a
    ``decision_id``; targets the dashboard decision-list flow.

  * ``record_dossier_override`` — Phase-10 dossier "override
    recommendation" button. Operator clicks from the merchant
    dossier, supplies the per-pattern false-positive checkbox
    selection, and the call:

      1. inserts the overrides row (migration 072 columns including
         ``merchant_id``, ``document_id``, ``pattern_false_positives``,
         optional ``decision_id``);
      2. flips ``documents.parse_status`` to reflect the operator's
         decision (``approve``→``proceed``, ``decline``→``decline``);
      3. writes a ``deal.operator_override`` audit row whose actor
         carries the operator email when available.

    Audit-write failure FAILS the operation (CLAUDE.md compliance:
    audit gap = regulator-defense gap).

The legacy write path also back-stamps the override's ``outcome``
if a matching funder reply already arrived (refinement (5)
symmetric case). The dossier write path does NOT back-stamp —
the dossier captures an operator's decision-of-record; outcomes
are populated later by funder reply ingestion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.audit import AuditLog, AuditWriteError
from aegis.db import get_supabase
from aegis.funders.replies import (
    FunderReplyRepository,
    stamp_override_from_replies,
)
from aegis.logger import get_logger
from aegis.storage import DocumentRepository, ParseStatus

_log = get_logger(__name__)


# Closed set of reason codes — matches the CHECK constraint in
# migration 017. Adding a new code requires updating BOTH this Literal
# and the migration's CHECK; the duplication is intentional so a typo
# fails at validation time AND at DB write time.
ReasonCode = Literal[
    "score_too_conservative",
    "score_too_aggressive",
    "funder_specific_fit",
    "merchant_context_external",
    "data_quality_concern",
    "pattern_false_positive",
    "pattern_false_negative",
    "gut",
]


# Closed set mirroring migration 017's operator_decision options.
# These are the parser-side recommendations that survive into the
# override row; downstream the operator can mark anything but for
# capture we keep the set narrow so the confusion matrix has clean
# axes.
OperatorDecision = Literal["approve", "decline", "refer"]


class OverridePayload(BaseModel):
    """Inputs to ``record_override``.

    Strict + frozen so a typo at the call site fails at construction
    time, before any DB write happens.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        frozen=True,
    )

    deal_id: UUID
    decision_id: UUID
    original_recommendation: OperatorDecision
    operator_decision: OperatorDecision
    reason_code: ReasonCode
    reason_detail: str | None = Field(default=None, max_length=2000)
    factors_disputed: dict[str, Any] = Field(default_factory=dict)
    pattern_false_positive: list[str] = Field(default_factory=list)
    operator_id: str = Field(min_length=1)


class OverrideError(RuntimeError):
    """Raised when the overrides write fails after validation passed."""


# Dossier-flow operator decision — strictly two values per migration 072's
# CHECK constraint. The legacy ``OperatorDecision`` accepts 'refer' for
# backwards-compat with the older route.
DossierOperatorDecision = Literal["approve", "decline"]


# Pre-override parser recommendation surfaced into the row's audit story.
# ``manual_review`` is included so a doc that came out of the parser at
# manual_review can still have an operator override captured (the dossier
# button is gated to proceed/decline by the UI, but the legacy code path
# accepts manual_review for completeness).
ParserRecommendation = Literal["proceed", "decline", "manual_review"]


class DossierOverridePayload(BaseModel):
    """Dossier "Override recommendation" form payload (migration 072).

    Distinct shape from the legacy ``OverridePayload``:
      * ``decision_id`` is optional — older documents without a
        ``decisions`` row can still be overridden.
      * ``merchant_id`` and ``document_id`` are explicit (the
        dossier route always has both).
      * ``operator_decision`` is the strict {approve, decline}
        literal that maps to migration 072's CHECK.
      * ``pattern_false_positives`` is the plural array (the
        modal's per-pattern checkbox set).

    Strict + frozen so a typo at the call site fails at Pydantic
    construction time, before any DB write happens.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        frozen=True,
    )

    merchant_id: UUID
    document_id: UUID
    decision_id: UUID | None = None
    original_recommendation: ParserRecommendation
    operator_decision: DossierOperatorDecision
    reason_code: ReasonCode
    reason_detail: str | None = Field(default=None, max_length=2000)
    pattern_false_positives: list[str] = Field(default_factory=list)
    operator_id: str = Field(min_length=1)
    operator_email: str | None = None


# ---------------------------------------------------------------------------
# Repository protocol + in-memory + Supabase impls
# ---------------------------------------------------------------------------


class OverrideRepository(Protocol):
    """Append-only write interface for the overrides table."""

    def insert_override(self, payload: OverridePayload) -> UUID:
        """Persist one overrides row; return its UUID."""

    def insert_dossier_override(self, payload: DossierOverridePayload) -> UUID:
        """Persist a dossier-flow overrides row (migration 072 shape); return its UUID.

        Carries the post-072 columns (merchant_id, document_id,
        pattern_false_positives, nullable decision_id). The legacy
        ``insert_override`` path is preserved for the
        ``/ui/decisions/{decision_id}/override`` route.
        """

    def list_for_summary(self) -> list[dict[str, Any]]:
        """Read every override row (id, reason_code, outcome).

        Powers the confusion-matrix endpoint
        (``/ui/overrides/summary``). Bounded by the table size — a
        future row-count growth past ~10k overrides argues for adding
        a date filter; today the operator's volume keeps this
        unbounded read predictable.
        """

    def rows(self) -> list[dict[str, Any]]:  # pragma: no cover — debugging aid
        """Return all rows (in-memory backends only)."""


class InMemoryOverrideRepository:
    """List-backed repository for tests + memory storage backend."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def insert_override(self, payload: OverridePayload) -> UUID:
        row_id = uuid4()
        now = datetime.now().astimezone().isoformat()
        row = {
            "id": str(row_id),
            "deal_id": str(payload.deal_id),
            "decision_id": str(payload.decision_id),
            "original_recommendation": payload.original_recommendation,
            "operator_decision": payload.operator_decision,
            "reason_code": payload.reason_code,
            "reason_detail": payload.reason_detail,
            "factors_disputed": payload.factors_disputed or None,
            "pattern_false_positive": list(payload.pattern_false_positive) or None,
            "operator_id": payload.operator_id,
            "created_at": now,
            "outcome": None,
            "outcome_recorded_at": None,
        }
        self._rows.append(row)
        return row_id

    def insert_dossier_override(self, payload: DossierOverridePayload) -> UUID:
        """Persist a dossier-flow override row (migration 072 shape).

        Mirrors the SupabaseOverrideRepository row shape so the
        in-memory backend feeds ``list_for_summary`` with the same
        keys the confusion-matrix endpoint reads.
        """
        row_id = uuid4()
        now = datetime.now().astimezone().isoformat()
        row = {
            "id": str(row_id),
            "merchant_id": str(payload.merchant_id),
            "document_id": str(payload.document_id),
            # deal_id mirrors document_id per migration 017's convention
            # (deal_id REFERENCES documents(id)); the dossier flow writes
            # both so future readers don't need to special-case the
            # legacy column.
            "deal_id": str(payload.document_id),
            "decision_id": str(payload.decision_id) if payload.decision_id else None,
            "original_recommendation": payload.original_recommendation,
            "operator_decision": payload.operator_decision,
            "reason_code": payload.reason_code,
            "reason_detail": payload.reason_detail,
            "pattern_false_positives": list(payload.pattern_false_positives),
            # Legacy singular column gets the same list so existing
            # readers that still query the old column keep seeing data.
            "pattern_false_positive": (
                list(payload.pattern_false_positives) if payload.pattern_false_positives else None
            ),
            "factors_disputed": None,
            "operator_id": payload.operator_id,
            "created_at": now,
            "outcome": None,
            "outcome_recorded_at": None,
        }
        self._rows.append(row)
        return row_id

    def list_for_summary(self) -> list[dict[str, Any]]:
        """Subset projection consumed by the confusion-matrix endpoint."""
        return [
            {
                "id": r.get("id"),
                "reason_code": r.get("reason_code"),
                "outcome": r.get("outcome"),
            }
            for r in self._rows
        ]

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows]


class SupabaseOverrideRepository:
    """Persists each override to the ``overrides`` table."""

    def insert_override(self, payload: OverridePayload) -> UUID:
        row_id = uuid4()
        body: dict[str, Any] = {
            "id": str(row_id),
            "deal_id": str(payload.deal_id),
            "decision_id": str(payload.decision_id),
            "original_recommendation": payload.original_recommendation,
            "operator_decision": payload.operator_decision,
            "reason_code": payload.reason_code,
            "reason_detail": payload.reason_detail,
            "factors_disputed": payload.factors_disputed or None,
            "pattern_false_positive": (
                list(payload.pattern_false_positive) if payload.pattern_false_positive else None
            ),
            "operator_id": payload.operator_id,
        }
        # supabase-py forwards dicts as jsonb directly; serialize via
        # json.loads(json.dumps(...)) so a stray UUID / Decimal in
        # factors_disputed becomes string before the wire.
        serialized = cast(dict[str, Any], json.loads(json.dumps(body, default=str)))
        try:
            get_supabase().table("overrides").insert(serialized).execute()
        except Exception as exc:
            _log.error(
                "overrides.write_failed deal_id=%s decision_id=%s",
                payload.deal_id,
                payload.decision_id,
            )
            raise OverrideError(
                f"failed to write override for decision {payload.decision_id}"
            ) from exc
        return row_id

    def insert_dossier_override(self, payload: DossierOverridePayload) -> UUID:
        """Persist a dossier-flow override row to the ``overrides`` table.

        Writes the post-072 columns (merchant_id, document_id,
        pattern_false_positives) plus deal_id (mirror of document_id)
        and the legacy pattern_false_positive (singular) column so a
        rollback to pre-072 readers still sees the operator's
        selection.
        """
        row_id = uuid4()
        body: dict[str, Any] = {
            "id": str(row_id),
            "merchant_id": str(payload.merchant_id),
            "document_id": str(payload.document_id),
            "deal_id": str(payload.document_id),
            "decision_id": (str(payload.decision_id) if payload.decision_id is not None else None),
            "original_recommendation": payload.original_recommendation,
            "operator_decision": payload.operator_decision,
            "reason_code": payload.reason_code,
            "reason_detail": payload.reason_detail,
            "pattern_false_positives": list(payload.pattern_false_positives),
            "pattern_false_positive": (
                list(payload.pattern_false_positives) if payload.pattern_false_positives else None
            ),
            "operator_id": payload.operator_id,
        }
        serialized = cast(dict[str, Any], json.loads(json.dumps(body, default=str)))
        try:
            get_supabase().table("overrides").insert(serialized).execute()
        except Exception as exc:
            _log.error(
                "overrides.dossier_write_failed document_id=%s merchant_id=%s",
                payload.document_id,
                payload.merchant_id,
            )
            raise OverrideError(
                f"failed to write dossier override for document {payload.document_id}"
            ) from exc
        return row_id

    def list_for_summary(self) -> list[dict[str, Any]]:
        """Read minimal columns for the confusion-matrix endpoint.

        The ``outcome`` column may be NULL (open / not-yet-reported)
        — the confusion matrix renders those as the ``pending`` bucket.
        """
        try:
            result = get_supabase().table("overrides").select("id,reason_code,outcome").execute()
        except Exception:
            _log.warning("overrides.list_for_summary_failed")
            return []
        return [
            {
                "id": r.get("id"),
                "reason_code": r.get("reason_code"),
                "outcome": r.get("outcome"),
            }
            for r in cast(list[dict[str, Any]], result.data or [])
            if isinstance(r, dict)
        ]

    def rows(self) -> list[dict[str, Any]]:  # pragma: no cover
        try:
            result = get_supabase().table("overrides").select("*").execute()
        except Exception:
            return []
        return [dict(r) for r in (result.data or []) if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------


@dataclass
class OverrideOutcome:
    """Result of one ``record_override`` call.

    ``override_id`` is always populated. ``back_stamped_outcome`` is
    set iff a matching funder reply was already in flight when the
    override landed (the symmetric case from refinement 5).
    """

    override_id: UUID
    back_stamped_outcome: str | None = None


def record_override(
    payload: OverridePayload,
    *,
    repo: OverrideRepository,
    reply_repo: FunderReplyRepository,
    audit: AuditLog,
) -> OverrideOutcome:
    """Persist an override + audit + back-stamp from any pending reply.

    Order:
      1. Insert overrides row. Failure → ``OverrideError`` (no state
         change beyond the validated payload).
      2. Audit ``decision.override`` with the operator's stated
         reason and the override_id for the audit trail.
      3. Look up the most-recent funder reply for this deal; if its
         status maps to an outcome, stamp the override exactly once
         (concurrent webhook ingestion would have stamped already,
         which the repo's ``WHERE outcome IS NULL`` guard handles).
    """
    override_id = repo.insert_override(payload)

    audit.record(
        actor=payload.operator_id,
        action="decision.override",
        subject_type="deal",
        subject_id=payload.deal_id,
        details={
            "override_id": str(override_id),
            "decision_id": str(payload.decision_id),
            "original_recommendation": payload.original_recommendation,
            "operator_decision": payload.operator_decision,
            "reason_code": payload.reason_code,
            "pattern_false_positive_count": len(payload.pattern_false_positive),
        },
    )

    back_stamped: str | None = None
    stamped_id = stamp_override_from_replies(
        override_id=override_id,
        deal_id=payload.deal_id,
        repo=reply_repo,
        audit=audit,
    )
    if stamped_id is not None:
        # We have the override_id from the stamp call; pull the
        # outcome via the reply to record it on the response.
        latest = reply_repo.latest_reply_for_deal(payload.deal_id)
        if latest is not None:
            # status -> outcome mapping lives in funders.replies;
            # we re-derive without importing the dict to avoid a
            # cross-module cycle on a small mapping.
            status = latest.get("status")
            if status == "approved":
                back_stamped = "funded"
            elif status == "declined":
                back_stamped = "declined_by_funder"
    return OverrideOutcome(override_id=override_id, back_stamped_outcome=back_stamped)


# ---------------------------------------------------------------------------
# Dossier flow (Phase 10 / migration 072)
# ---------------------------------------------------------------------------


# Operator decision → ``documents.parse_status`` mapping. ``approve``
# maps cleanly to the existing ``proceed`` value (the parse-pipeline
# proceed semantic — "the deal moves forward"). ``decline`` maps to the
# new ``decline`` value migration 072 adds to the CHECK constraint.
# Kept as a module-level mapping so a future enum widening only touches
# this one site.
PARSE_STATUS_AFTER_OVERRIDE: dict[DossierOperatorDecision, ParseStatus] = {
    "approve": "proceed",
    "decline": "decline",
}


def record_dossier_override(
    payload: DossierOverridePayload,
    *,
    override_repo: OverrideRepository,
    documents: DocumentRepository,
    audit: AuditLog,
) -> UUID:
    """Persist a dossier "override recommendation" decision.

    Order (audit-write last so an audit-fail still surfaces the
    underlying override / parse_status mutations as the operator's
    decision-of-record — matching the existing
    ``aegis.compliance.snapshot.record_decision`` ordering):

      1. ``insert_dossier_override`` — write the overrides row
         (migration 072 columns). ``OverrideError`` propagates.
      2. ``documents.set_parse_status`` — flip the document's
         parse_status to the operator's disposition (approve→proceed,
         decline→decline). The CHECK constraint on migration 072
         enforces the value set; a DB error here propagates.
      3. ``audit.record`` — write the ``deal.operator_override`` row
         with the operator's identity. ``AuditWriteError`` propagates
         (CLAUDE.md compliance: audit-write failure FAILS the
         operation; a state change without an audit row is a
         regulator-defense gap).

    ``actor`` follows the project memory ``audit_actor_shape_by_auth_method``
    convention: operator email when present (``operator:{email}``),
    otherwise the supplied ``operator_id`` value verbatim (the
    bearer-token / dashboard fallback). ``actor_email`` carries the
    raw email separately on the audit row.

    Returns the new override's UUID.
    """
    # Validate the document exists BEFORE any write. Sequencing the
    # validation first means an unknown document_id surfaces as a
    # ``DocumentNotFoundError`` from the caller's view without leaving
    # a half-written overrides row behind.
    documents.get_document(payload.document_id)

    override_id = override_repo.insert_dossier_override(payload)

    # Flip parse_status to reflect the operator's decision. The CHECK
    # constraint (migration 072) is the load-bearing validator on the
    # value; if a future operator_decision enum grows beyond
    # {approve, decline} the migration must extend the CHECK in the
    # same change.
    new_status = PARSE_STATUS_AFTER_OVERRIDE[payload.operator_decision]
    documents.set_parse_status(payload.document_id, new_status)

    # Actor follows the project's audit_actor_shape convention. The
    # email goes into the dedicated actor_email column so a downstream
    # consumer can filter on identity without parsing the actor string.
    actor = f"operator:{payload.operator_email}" if payload.operator_email else payload.operator_id
    try:
        audit.record(
            actor=actor,
            actor_email=payload.operator_email,
            action="deal.operator_override",
            subject_type="deal",
            subject_id=payload.document_id,
            details={
                "override_id": str(override_id),
                "merchant_id": str(payload.merchant_id),
                "document_id": str(payload.document_id),
                "decision_id": (str(payload.decision_id) if payload.decision_id else None),
                "original_recommendation": payload.original_recommendation,
                "operator_decision": payload.operator_decision,
                "reason_code": payload.reason_code,
                "new_parse_status": new_status,
                "pattern_false_positives": list(payload.pattern_false_positives),
                "pattern_false_positives_count": len(payload.pattern_false_positives),
            },
        )
    except AuditWriteError:
        # CLAUDE.md non-negotiable: audit-write failures FAIL the
        # operation. The override row + parse_status flip already
        # landed; the audit gap is the regulator-defense failure that
        # callers must see.
        _log.error(
            "dossier_override.audit_failed override_id=%s document_id=%s",
            override_id,
            payload.document_id,
        )
        raise
    return override_id


# ---------------------------------------------------------------------------
# Confusion-matrix aggregation (Phase 10 task 4 / master plan §20)
# ---------------------------------------------------------------------------


# Outcome bucket order surfaced in the confusion-matrix UI. Matches
# the migration 017 + 072 CHECK constraint enumeration. ``pending``
# is computed in-Python for rows whose outcome column is NULL.
OUTCOME_COLUMNS: tuple[str, ...] = (
    "funded",
    "declined_by_funder",
    "charged_off",
    "paid_in_full",
    "pending",
)


@dataclass(frozen=True)
class ReasonCodeRow:
    """One row in the confusion-matrix table.

    ``counts`` is indexed by the OUTCOME_COLUMNS tuple so the template
    can iterate in a stable order. ``total`` is the sum across
    columns — equals the count of overrides under this reason_code.
    """

    reason_code: str
    counts: dict[str, int]
    total: int


def build_reason_code_summary(
    rows: list[dict[str, Any]],
) -> list[ReasonCodeRow]:
    """Aggregate overrides into one row per (reason_code, outcome) counts.

    Empty ``rows`` returns an empty list — the template renders the
    empty-state copy. Rows whose ``outcome`` is None / unknown collapse
    into the ``pending`` bucket so the operator's first day post-deploy
    (before any funder reply has been ingested) still renders a
    meaningful table.

    Sorted by total descending so the operator sees the most-frequent
    override reasons at the top. Ties broken alphabetically by
    reason_code for determinism.
    """
    buckets: dict[str, dict[str, int]] = {}
    for r in rows:
        reason = str(r.get("reason_code") or "")
        if not reason:
            continue
        outcome = r.get("outcome")
        # NULL / unknown / pending → pending bucket
        if outcome not in OUTCOME_COLUMNS or outcome is None:
            outcome_key = "pending"
        else:
            outcome_key = outcome
        if reason not in buckets:
            buckets[reason] = {col: 0 for col in OUTCOME_COLUMNS}
        buckets[reason][outcome_key] = buckets[reason].get(outcome_key, 0) + 1
    out: list[ReasonCodeRow] = []
    for reason, counts in buckets.items():
        total = sum(counts.values())
        out.append(ReasonCodeRow(reason_code=reason, counts=counts, total=total))
    out.sort(key=lambda r: (-r.total, r.reason_code))
    return out


__all__ = [
    "OUTCOME_COLUMNS",
    "PARSE_STATUS_AFTER_OVERRIDE",
    "DossierOperatorDecision",
    "DossierOverridePayload",
    "InMemoryOverrideRepository",
    "OperatorDecision",
    "OverrideError",
    "OverrideOutcome",
    "OverridePayload",
    "OverrideRepository",
    "ParserRecommendation",
    "ReasonCode",
    "ReasonCodeRow",
    "SupabaseOverrideRepository",
    "build_reason_code_summary",
    "record_dossier_override",
    "record_override",
]
