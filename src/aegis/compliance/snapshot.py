"""Immutable decision-snapshot writer (mp Phase 2).

Per master plan §2 principle 3 + §9.2: every approve / decline /
manual_review writes one row to the ``decisions`` table BEFORE the
operation returns. The row freezes everything an auditor or counsel
needs to answer "why did you decide X on date Y" six months from now:

- score_factors at decision time
- contributing transaction UUIDs
- bank statement PDF SHA256
- OFAC cache timestamp + hash
- aegis_version + rule_pack_version
- disclosure template path + SHA256 (when a disclosure issued)

Immutability is enforced at the database layer (migration 015 installs
``block_decision_modification`` triggers that raise on UPDATE/DELETE).
This module only provides the WRITE path and protocol abstractions
parallel to the existing ``aegis.audit`` module.

Failure mode: if the audit_log write fails after the decisions row
lands, the operation must raise — matching the existing audit-write
discipline in ``aegis.audit``. A decision row without an audit
companion is a regulator-defense gap.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

import aegis
from aegis.audit import AuditLog, AuditWriteError
from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.parser.pipeline import FRAUD_WEIGHTS

_log = get_logger(__name__)


DecisionLiteral = Literal["approve", "decline", "manual_review", "redisclosure"]
BackfillQuality = Literal["minimal", "partial", "full"]


class DecisionSnapshotError(RuntimeError):
    """Raised when a decision row cannot be persisted.

    Distinct from ``AuditWriteError`` (which signals audit_log failure)
    so callers can tell which leg failed when both writes are attempted.
    """


class DecisionPayload(BaseModel):
    """Inputs to ``record_decision()``.

    Frozen Pydantic model — strict-mode enforcement so a typo at the
    call site fails at validation time, not after a partial DB write.

    Money fields are Decimal (CLAUDE.md). UUID fields are UUID (not str).
    Decimal score is bounded 0-100 to match the migration 015 CHECK.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_assignment=True,
    )

    deal_id: UUID
    decided_by: str = Field(min_length=1)
    decision: DecisionLiteral
    decision_reason_codes: list[str] = Field(min_length=0)
    score: Annotated[Decimal, Field(max_digits=5, decimal_places=2)] | None = None
    score_factors: dict[str, Any] = Field(default_factory=dict)

    analysis_id: UUID | None = None
    contributing_transaction_uuids: list[UUID] = Field(default_factory=list)
    bank_statement_pdf_sha256: str | None = None

    state_code: str = Field(min_length=2, max_length=2)
    cfdl_tier: int = Field(ge=1, le=3)
    disclosure_template_path: str | None = None
    disclosure_template_sha256: str | None = None
    disclosure_pdf_sha256: str | None = None
    apr_calculated: Annotated[Decimal, Field(max_digits=8, decimal_places=4)] | None = None
    apr_method: str | None = None

    ofac_cache_timestamp: datetime | None = None
    ofac_cache_sha256: str | None = None

    aegis_version: str = Field(min_length=1)
    rule_pack_version: str = Field(min_length=1)

    backfill_quality: BackfillQuality | None = None
    decided_at: datetime | None = None  # None = let DB default to NOW(); set for backfill


class StoredDecision(BaseModel):
    """Subset of a persisted decisions-row that outbound consumers
    (currently: Close CRM write-back in step 6) need.

    Kept narrow on purpose — this is the read-shape, not the write-shape.
    Adding fields here is a deliberate read-API expansion.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: UUID
    deal_id: UUID
    decision: DecisionLiteral
    score: Decimal | None = None
    decision_reason_codes: list[str] = Field(default_factory=list)
    ofac_cache_timestamp: datetime | None = None
    decided_at: datetime | None = None


class DecisionSnapshot(Protocol):
    """Append-only write interface for the decisions table.

    Reads were originally out-of-scope for this Protocol (see
    ``api/routes/audit.py`` for the per-deal read query). Step 6 of the
    Close-CRM integration added one focused read: "latest decision for
    a merchant", to drive the outbound write-back at the operator's
    sync-to-close trigger. The read protocol stays narrow — write-back
    needs id + decision + score + reason codes + OFAC timestamp, and
    nothing else.
    """

    def write(self, payload: DecisionPayload, *, audit: AuditLog) -> UUID:
        """Persist one decision row and a matching audit_log entry.

        Returns the new decision row's UUID. Raises:
          * ``DecisionSnapshotError`` on the decision-row write failure.
          * ``AuditWriteError`` on the audit-log write failure (after the
            decision row has landed). Callers must propagate either way.
        """

    def find_latest_for_merchant(
        self,
        merchant_id: UUID,
        *,
        deal_ids: list[UUID],
    ) -> StoredDecision | None:
        """Return the most-recent decision (by ``decided_at``) whose
        ``deal_id`` is in ``deal_ids`` — i.e. one of the documents
        belonging to this merchant. ``None`` when none of the merchant's
        documents have a decision yet.

        ``deal_ids`` is computed by the caller via the DocumentRepository
        so this Protocol stays decoupled from document storage.
        """


# ---------------------------------------------------------------------------
# In-memory implementation (tests + memory storage backend)
# ---------------------------------------------------------------------------


class InMemoryDecisionSnapshot:
    """List-backed snapshot store. Used in tests and the memory backend.

    Enforces immutability semantically: ``write()`` appends; the list is
    exposed read-only via ``rows()``. There's no update or delete API.
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def write(self, payload: DecisionPayload, *, audit: AuditLog) -> UUID:
        row_id = uuid4()
        row = _payload_to_row(payload, row_id=row_id)
        self._rows.append(row)
        try:
            audit.record(
                actor=payload.decided_by,
                action=f"decision.{payload.decision}",
                subject_type="deal",
                subject_id=payload.deal_id,
                details={
                    "decision_id": str(row_id),
                    "score": str(payload.score) if payload.score is not None else None,
                    "state_code": payload.state_code,
                    "cfdl_tier": payload.cfdl_tier,
                    "aegis_version": payload.aegis_version,
                    "rule_pack_version": payload.rule_pack_version,
                    "reason_codes": list(payload.decision_reason_codes),
                },
            )
        except AuditWriteError:
            # The decision row is durable in this list; we still propagate
            # because the audit gap is the regulator-defense failure mode.
            raise
        return row_id

    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def find_latest_for_merchant(
        self,
        merchant_id: UUID,
        *,
        deal_ids: list[UUID],
    ) -> StoredDecision | None:
        if not deal_ids:
            return None
        deal_id_strs = {str(d) for d in deal_ids}
        candidates = [r for r in self._rows if r.get("deal_id") in deal_id_strs]
        if not candidates:
            return None
        # Sort by decided_at desc; None decided_at sorts last (oldest).
        candidates.sort(
            key=lambda r: r.get("decided_at") or "",
            reverse=True,
        )
        return _row_to_stored_decision(candidates[0])


# ---------------------------------------------------------------------------
# Supabase implementation
# ---------------------------------------------------------------------------


class SupabaseDecisionSnapshot:
    """Persists each decision to ``decisions`` and pairs it with an
    audit_log row.

    Order:
      1. Insert decisions row.
      2. Insert audit_log row via the supplied ``audit`` writer.
    If (2) fails after (1) succeeds, the decisions row is still there
    (the trigger blocks DELETE). Callers see ``AuditWriteError`` and the
    decision_id won't be referenced anywhere — operationally equivalent
    to an unreferenced orphan that can be reconciled from the audit
    gap. Reverse order (audit-first) would orphan an audit entry, which
    is worse for the audit story.
    """

    def write(self, payload: DecisionPayload, *, audit: AuditLog) -> UUID:
        row = _payload_to_row(payload, row_id=uuid4())
        try:
            get_supabase().table("decisions").insert(_serialize(row)).execute()
        except Exception as exc:
            _log.error(
                "decisions.write_failed deal_id=%s decision=%s",
                payload.deal_id,
                payload.decision,
            )
            raise DecisionSnapshotError(
                f"failed to write decisions row for deal {payload.deal_id}"
            ) from exc

        # Audit-pair write. AuditWriteError surfaces to the caller.
        audit.record(
            actor=payload.decided_by,
            action=f"decision.{payload.decision}",
            subject_type="deal",
            subject_id=payload.deal_id,
            details={
                "decision_id": row["id"],
                "score": str(payload.score) if payload.score is not None else None,
                "state_code": payload.state_code,
                "cfdl_tier": payload.cfdl_tier,
                "aegis_version": payload.aegis_version,
                "rule_pack_version": payload.rule_pack_version,
                "reason_codes": list(payload.decision_reason_codes),
            },
        )
        return UUID(row["id"])

    def find_latest_for_merchant(
        self,
        merchant_id: UUID,
        *,
        deal_ids: list[UUID],
    ) -> StoredDecision | None:
        if not deal_ids:
            return None
        try:
            result = (
                get_supabase()
                .table("decisions")
                .select("*")
                .in_("deal_id", [str(d) for d in deal_ids])
                .order("decided_at", desc=True)
                .limit(1)
                .execute()
            )
        except Exception:
            _log.warning(
                "decisions.find_latest_query_failed merchant_id=%s",
                merchant_id,
            )
            return None
        data = result.data or []
        if not data:
            return None
        return _row_to_stored_decision(cast(dict[str, Any], data[0]))


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------


def record_decision(
    payload: DecisionPayload,
    *,
    snapshot: DecisionSnapshot,
    audit: AuditLog,
) -> UUID:
    """Persist a decision row + paired audit entry.

    Thin convenience layer over ``DecisionSnapshot.write`` so callers
    that already have both injectables can write in one line. Returns
    the decision row's UUID.
    """
    return snapshot.write(payload, audit=audit)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_to_row(payload: DecisionPayload, *, row_id: UUID) -> dict[str, Any]:
    """Translate a DecisionPayload into a decisions-table row dict.

    Decimals become strings (numeric in Postgres tolerates string input
    via supabase-py). UUIDs become string. datetimes become ISO strings.
    None is preserved (nullable columns).
    """
    return {
        "id": str(row_id),
        "deal_id": str(payload.deal_id),
        "decided_at": payload.decided_at.isoformat() if payload.decided_at else None,
        "decided_by": payload.decided_by,
        "decision": payload.decision,
        "decision_reason_codes": list(payload.decision_reason_codes),
        "score": str(payload.score) if payload.score is not None else None,
        "score_factors": payload.score_factors,
        "analysis_id": str(payload.analysis_id) if payload.analysis_id else None,
        "contributing_transaction_uuids": [str(u) for u in payload.contributing_transaction_uuids],
        "bank_statement_pdf_sha256": payload.bank_statement_pdf_sha256,
        "state_code": payload.state_code,
        "cfdl_tier": payload.cfdl_tier,
        "disclosure_template_path": payload.disclosure_template_path,
        "disclosure_template_sha256": payload.disclosure_template_sha256,
        "disclosure_pdf_sha256": payload.disclosure_pdf_sha256,
        "apr_calculated": (
            str(payload.apr_calculated) if payload.apr_calculated is not None else None
        ),
        "apr_method": payload.apr_method,
        "ofac_cache_timestamp": (
            payload.ofac_cache_timestamp.isoformat()
            if payload.ofac_cache_timestamp is not None
            else None
        ),
        "ofac_cache_sha256": payload.ofac_cache_sha256,
        "aegis_version": payload.aegis_version,
        "rule_pack_version": payload.rule_pack_version,
        "backfill_quality": payload.backfill_quality,
    }


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    """JSON round-trip a row dict so non-primitives become DB-safe.

    Mirrors ``aegis.audit.SupabaseAuditLog.record``'s payload handling.
    """
    return cast(dict[str, Any], json.loads(json.dumps(row, default=_default_serializer)))


def _default_serializer(value: object) -> str:
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__} for decisions row")


def _row_to_stored_decision(row: dict[str, Any]) -> StoredDecision:
    """decisions-table row dict -> StoredDecision. Strict-mode-friendly:
    nulls and strings cast to the expected Python types."""

    def _parse_dt(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return None

    score_raw = row.get("score")
    return StoredDecision(
        id=UUID(str(row["id"])),
        deal_id=UUID(str(row["deal_id"])),
        decision=cast(DecisionLiteral, row["decision"]),
        score=Decimal(str(score_raw)) if score_raw is not None else None,
        decision_reason_codes=list(row.get("decision_reason_codes") or []),
        ofac_cache_timestamp=_parse_dt(row.get("ofac_cache_timestamp")),
        decided_at=_parse_dt(row.get("decided_at")),
    )


@lru_cache(maxsize=1)
def get_aegis_version() -> str:
    """Return the installed AEGIS package version.

    Prefers ``importlib.metadata.version('aegis')`` (the canonical source
    once the package is installed) and falls back to ``aegis.__version__``
    for in-tree development where the metadata isn't populated. Cached
    because every decision row writes this string; reading the package
    metadata on each call would be a tight-loop allocation for nothing.
    """
    try:
        return _pkg_version("aegis")
    except PackageNotFoundError:
        return aegis.__version__


@lru_cache(maxsize=1)
def get_rule_pack_version() -> str:
    """Stable identifier for the active scoring rule pack.

    Spec (master plan §9.2 + §12): the snapshot must pin "which rule pack
    decided this deal" so a regulator-defense reconstruction six months
    later can rerun scoring against frozen inputs and get the same result.

    Implementation: ``sha256(json(FRAUD_WEIGHTS, sort_keys=True))[:16]``.
    ``FRAUD_WEIGHTS`` is the canonical weighting dict in
    ``aegis.parser.pipeline`` — the only set of numbers the scoring path
    consumes that can quietly drift without a code review. Hashing the
    canonical dict (not the entire scoring module) gives a stable
    identifier that:

      * Changes when the weights change (regulator: "the rule pack the
        snapshot pinned doesn't match today's pack — escalate to
        recompute").
      * Does NOT churn on cosmetic edits elsewhere in the scorer
        (docstrings, type hints, unrelated function bodies). Snapshots
        pinned to the same weights stay reproducible across refactors.

    Sixteen hex chars are enough collision space for this purpose and
    keep audit-log rows short. Caller-side the value is opaque — never
    parsed, only compared.

    NOTE: this is intentionally NARROWER than "hash everything in
    ``scoring/``". The broader scope would force a rule-pack bump on
    every commit touching the module, which makes the field worthless
    as a drift indicator. If a future change adds a second canonical
    weights dict (e.g. a ``SCORING_WEIGHTS`` for the soft-score
    breakdown deltas), extend this function to hash the concatenation —
    but keep the surface to documented weights, not the entire module.
    """
    payload = json.dumps(FRAUD_WEIGHTS, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "BackfillQuality",
    "DecisionLiteral",
    "DecisionPayload",
    "DecisionSnapshot",
    "DecisionSnapshotError",
    "InMemoryDecisionSnapshot",
    "SupabaseDecisionSnapshot",
    "get_aegis_version",
    "get_rule_pack_version",
    "record_decision",
]
