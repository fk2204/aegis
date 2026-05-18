"""Operator-override capture (mp Phase 10 / Stage 2D-main).

Persists one row to the ``overrides`` table (migration 017) each time
the operator disagrees with AEGIS's recommendation on a specific
decision. The override is pinned to a ``decision_id`` so the snapshot
table + override table together answer the regulator-defense question
"what did AEGIS recommend, what did the operator do, and what
happened?".

The write path also back-stamps the override's ``outcome`` if a
matching funder reply already arrived (refinement (5) symmetric
case): if an operator overrides AEGIS and there's a stamped reply
in flight, we want the operator's override to inherit the outcome
without waiting for a second reply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.audit import AuditLog
from aegis.db import get_supabase
from aegis.funders.replies import (
    FunderReplyRepository,
    stamp_override_from_replies,
)
from aegis.logger import get_logger

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


# ---------------------------------------------------------------------------
# Repository protocol + in-memory + Supabase impls
# ---------------------------------------------------------------------------


class OverrideRepository(Protocol):
    """Append-only write interface for the overrides table."""

    def insert_override(self, payload: OverridePayload) -> UUID:
        """Persist one overrides row; return its UUID."""

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
                list(payload.pattern_false_positive)
                if payload.pattern_false_positive
                else None
            ),
            "operator_id": payload.operator_id,
        }
        # supabase-py forwards dicts as jsonb directly; serialize via
        # json.loads(json.dumps(...)) so a stray UUID / Decimal in
        # factors_disputed becomes string before the wire.
        serialized = cast(
            dict[str, Any], json.loads(json.dumps(body, default=str))
        )
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


__all__ = [
    "InMemoryOverrideRepository",
    "OperatorDecision",
    "OverrideError",
    "OverrideOutcome",
    "OverridePayload",
    "OverrideRepository",
    "ReasonCode",
    "SupabaseOverrideRepository",
    "record_override",
]
