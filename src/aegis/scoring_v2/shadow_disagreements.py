"""Shadow-comparison disagreement persistence (R1.6 Step 2 cutover prep).

Commit 973d7fd shipped a corpus-walking diagnostic that compares the
LIVE ``score_deal`` pipeline (legacy ``fraud_score``) against the new
Track A / B / C tracks for every merchant and categorises each
disagreement into one of five buckets:

  * ``agreement``                          — both surfaces agree
  * ``new-is-better``                      — new tracks catch what live missed
  * ``old-caught-something-new-misses``    — REGRESSION sentinel (loud)
  * ``genuinely-ambiguous``                — operator judgment needed
  * ``insufficient-new-data``              — neither actionable

That script prints to stdout. This module owns the WRITE path for the
``scoring_shadow_disagreements`` table (migration 037): one row per
(merchant, comparison-run, evidence-shape) disagreement, with operator
triage fields layered on top. The view ``scoring_disagreements_open``
(migration 038) is the triage queue.

Idempotency
-----------
Re-running the comparison on the same merchant on the same UTC day with
the same categorisation evidence MUST NOT create a duplicate row. The
idempotency anchor is a hash over

    (merchant_id, comparison_run_at::date, category, evidence)

computed at the application layer (no DB-side ON CONFLICT — the
``evidence`` JSONB is too loose for a stable Postgres unique index, and
the corpus-run cardinality is small enough that a pre-query is cheap).
The repository pre-queries for existing rows with the same anchor; if
present, it returns the existing record unchanged.

Two implementations of the ``ScoringDisagreementRepository`` Protocol:

  * ``InMemoryScoringDisagreementRepository`` — list-backed; used by
    tests and the in-memory backend.
  * ``SupabaseScoringDisagreementRepository`` — writes rows to Postgres.
    Insert failure raises ``ScoringDisagreementWriteError`` so the
    calling script (``scripts/scoring_shadow_compare.py``) can refuse
    to claim a successful persist.

PII discipline
--------------
``evidence`` JSONB is categorical/numeric by construction (the
comparison script's ``_categorise()`` consumes enums + factor names +
numeric shares — no merchant names, no transaction descriptions, no
account numbers). The repository does NOT accept business_name and
does NOT serialise it. The triage UI surfaces ``merchant_id`` UUID and
the structural evidence only; an operator clicking through to the
dossier is the path to human-readable identity.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Final, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------
# Category enum — must mirror the comparison script's CAT_* constants.
# ---------------------------------------------------------------------

CATEGORY_AGREEMENT: Final = "agreement"
CATEGORY_NEW_BETTER: Final = "new-is-better"
CATEGORY_OLD_BETTER: Final = "old-caught-something-new-misses"
CATEGORY_AMBIGUOUS: Final = "genuinely-ambiguous"
CATEGORY_INSUFFICIENT: Final = "insufficient-new-data"

ALLOWED_CATEGORIES: Final = frozenset(
    {
        CATEGORY_AGREEMENT,
        CATEGORY_NEW_BETTER,
        CATEGORY_OLD_BETTER,
        CATEGORY_AMBIGUOUS,
        CATEGORY_INSUFFICIENT,
    }
)

# Triage decisions accepted on the operator-side update path. The
# comparison script does not write these; the triage UI does.
ALLOWED_TRIAGE_DECISIONS: Final = frozenset(
    {
        "accept-new",        # Track A/B/C wins; cutover safe for this case
        "accept-old",        # Legacy fraud_score wins; do NOT cut over
        "both-valid",        # Semantic disagreement; neither is wrong
        "needs-rule-change", # A track/severity needs adjusting before cutover
    }
)


class ScoringDisagreementWriteError(RuntimeError):
    """Raised when a disagreement row could not be persisted.

    Mirrors ``AuditWriteError`` / ``DisclosureTransmissionWriteError``
    semantics: write failures must not silently drop. The Step 2
    cutover decision depends on the integrity of this triage queue.
    """


class ScoringDisagreementRecord(BaseModel):
    """One disagreement row. Pydantic so callers cannot pass loose dicts."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: UUID
    merchant_id: UUID
    deal_id: UUID | None
    comparison_run_at: datetime
    legacy_fraud_score: int | None
    legacy_tier: str | None
    legacy_recommendation: str | None
    legacy_hard_declines: list[str] | None
    track_a_verdict: str | None
    track_b_band: str | None
    track_c_panel: dict[str, Any] | None
    category: str
    evidence: dict[str, Any] | None
    triaged_by: str | None = None
    triaged_at: datetime | None = None
    triage_decision: str | None = None
    triage_notes: str | None = None


class ScoringDisagreementRepository(Protocol):
    """Append-only-on-record, update-on-triage interface."""

    def record(
        self,
        *,
        merchant_id: UUID,
        deal_id: UUID | None,
        category: str,
        legacy_fraud_score: int | None,
        legacy_tier: str | None,
        legacy_recommendation: str | None,
        legacy_hard_declines: list[str] | None,
        track_a_verdict: str | None,
        track_b_band: str | None,
        track_c_panel: dict[str, Any] | None,
        evidence: dict[str, Any] | None,
        comparison_run_at: datetime | None = None,
    ) -> ScoringDisagreementRecord: ...

    def list_open(self) -> list[ScoringDisagreementRecord]:
        """Return every row with ``triaged_at IS NULL`` (triage queue)."""
        ...


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _validate_category(category: str) -> None:
    if category not in ALLOWED_CATEGORIES:
        raise ValueError(
            f"category must be one of {sorted(ALLOWED_CATEGORIES)}, "
            f"got {category!r}"
        )


def _validate_triage_decision(decision: str | None) -> None:
    if decision is not None and decision not in ALLOWED_TRIAGE_DECISIONS:
        raise ValueError(
            f"triage_decision must be one of {sorted(ALLOWED_TRIAGE_DECISIONS)} "
            f"or None, got {decision!r}"
        )


def _canonical_evidence_json(evidence: dict[str, Any] | None) -> str:
    """Stable string form of ``evidence`` for hashing.

    Sort keys so equal-content dicts hash identically regardless of
    insertion order. ``None`` serialises to the literal "null" so the
    hash distinguishes "no evidence captured" from "empty dict".
    """
    if evidence is None:
        return "null"
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)


def _evidence_hash(evidence: dict[str, Any] | None) -> str:
    """sha256 hex of the canonical evidence string."""
    return hashlib.sha256(_canonical_evidence_json(evidence).encode("utf-8")).hexdigest()


def _idempotency_anchor(
    *,
    merchant_id: UUID,
    comparison_run_at: datetime,
    category: str,
    evidence: dict[str, Any] | None,
) -> tuple[UUID, str, str, str]:
    """Idempotency key components.

    Day-precision on the comparison timestamp because the same nightly
    run can land slightly different ``comparison_run_at`` values across
    rows (each merchant's row is timestamped at process time). Within
    one calendar day, identical (merchant, category, evidence) is a
    duplicate.
    """
    day = comparison_run_at.astimezone(UTC).date().isoformat()
    return (merchant_id, day, category, _evidence_hash(evidence))


# ---------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------


class InMemoryScoringDisagreementRepository:
    """List-backed implementation. Used by tests and the memory backend."""

    def __init__(self) -> None:
        self.rows: list[ScoringDisagreementRecord] = []

    def _find_duplicate(
        self,
        anchor: tuple[UUID, str, str, str],
    ) -> ScoringDisagreementRecord | None:
        target_mid, target_day, target_cat, target_hash = anchor
        for row in self.rows:
            row_day = row.comparison_run_at.astimezone(UTC).date().isoformat()
            if (
                row.merchant_id == target_mid
                and row_day == target_day
                and row.category == target_cat
                and _evidence_hash(row.evidence) == target_hash
            ):
                return row
        return None

    def record(
        self,
        *,
        merchant_id: UUID,
        deal_id: UUID | None,
        category: str,
        legacy_fraud_score: int | None,
        legacy_tier: str | None,
        legacy_recommendation: str | None,
        legacy_hard_declines: list[str] | None,
        track_a_verdict: str | None,
        track_b_band: str | None,
        track_c_panel: dict[str, Any] | None,
        evidence: dict[str, Any] | None,
        comparison_run_at: datetime | None = None,
    ) -> ScoringDisagreementRecord:
        _validate_category(category)
        ts = comparison_run_at or datetime.now(UTC)
        anchor = _idempotency_anchor(
            merchant_id=merchant_id,
            comparison_run_at=ts,
            category=category,
            evidence=evidence,
        )
        existing = self._find_duplicate(anchor)
        if existing is not None:
            return existing

        record = ScoringDisagreementRecord(
            id=uuid4(),
            merchant_id=merchant_id,
            deal_id=deal_id,
            comparison_run_at=ts,
            legacy_fraud_score=legacy_fraud_score,
            legacy_tier=legacy_tier,
            legacy_recommendation=legacy_recommendation,
            legacy_hard_declines=legacy_hard_declines,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
            track_c_panel=track_c_panel,
            category=category,
            evidence=evidence,
        )
        self.rows.append(record)
        return record

    def list_open(self) -> list[ScoringDisagreementRecord]:
        return [r for r in self.rows if r.triaged_at is None]


# ---------------------------------------------------------------------
# Supabase implementation
# ---------------------------------------------------------------------


def _row_to_record(row: dict[str, Any]) -> ScoringDisagreementRecord:
    """Map a Supabase row dict to a ``ScoringDisagreementRecord``."""

    def _dt(key: str) -> datetime | None:
        v = row.get(key)
        if v is None:
            return None
        if isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))

    comparison_run_at = _dt("comparison_run_at")
    if comparison_run_at is None:
        raise ScoringDisagreementWriteError(
            "supabase row missing required 'comparison_run_at'"
        )

    hard_declines_raw = row.get("legacy_hard_declines")
    if hard_declines_raw is None:
        hard_declines: list[str] | None = None
    elif isinstance(hard_declines_raw, list):
        hard_declines = [str(x) for x in hard_declines_raw]
    else:
        raise ScoringDisagreementWriteError(
            f"legacy_hard_declines must be list or null, got {type(hard_declines_raw).__name__}"
        )

    return ScoringDisagreementRecord(
        id=UUID(row["id"]),
        merchant_id=UUID(row["merchant_id"]),
        deal_id=UUID(row["deal_id"]) if row.get("deal_id") else None,
        comparison_run_at=comparison_run_at,
        legacy_fraud_score=row.get("legacy_fraud_score"),
        legacy_tier=row.get("legacy_tier"),
        legacy_recommendation=row.get("legacy_recommendation"),
        legacy_hard_declines=hard_declines,
        track_a_verdict=row.get("track_a_verdict"),
        track_b_band=row.get("track_b_band"),
        track_c_panel=row.get("track_c_panel"),
        category=row["category"],
        evidence=row.get("evidence"),
        triaged_by=row.get("triaged_by"),
        triaged_at=_dt("triaged_at"),
        triage_decision=row.get("triage_decision"),
        triage_notes=row.get("triage_notes"),
    )


class SupabaseScoringDisagreementRepository:
    """Persistence backed by Postgres ``scoring_shadow_disagreements``.

    Pre-queries for the idempotency anchor before insert; if a row with
    the same (merchant_id, day, category, evidence-hash) exists, returns
    that existing row unchanged. Otherwise inserts a new row.
    """

    def _find_duplicate(
        self,
        *,
        merchant_id: UUID,
        comparison_run_at: datetime,
        category: str,
        evidence: dict[str, Any] | None,
    ) -> ScoringDisagreementRecord | None:
        """Pre-query for an existing row matching the idempotency anchor."""
        day_start = comparison_run_at.astimezone(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = comparison_run_at.astimezone(UTC).replace(
            hour=23, minute=59, second=59, microsecond=999_999
        )
        target_hash = _evidence_hash(evidence)
        try:
            result = (
                get_supabase()
                .table("scoring_shadow_disagreements")
                .select("*")
                .eq("merchant_id", str(merchant_id))
                .eq("category", category)
                .gte("comparison_run_at", day_start.isoformat())
                .lte("comparison_run_at", day_end.isoformat())
                .execute()
            )
        except Exception as exc:
            # A read failure here would silently break idempotency and
            # cause duplicate inserts. Surface it loudly.
            raise ScoringDisagreementWriteError(
                f"idempotency pre-query failed for merchant={merchant_id}"
            ) from exc

        rows = cast(list[dict[str, Any]], result.data or [])
        for row in rows:
            if _evidence_hash(row.get("evidence")) == target_hash:
                return _row_to_record(row)
        return None

    def record(
        self,
        *,
        merchant_id: UUID,
        deal_id: UUID | None,
        category: str,
        legacy_fraud_score: int | None,
        legacy_tier: str | None,
        legacy_recommendation: str | None,
        legacy_hard_declines: list[str] | None,
        track_a_verdict: str | None,
        track_b_band: str | None,
        track_c_panel: dict[str, Any] | None,
        evidence: dict[str, Any] | None,
        comparison_run_at: datetime | None = None,
    ) -> ScoringDisagreementRecord:
        _validate_category(category)
        ts = comparison_run_at or datetime.now(UTC)

        existing = self._find_duplicate(
            merchant_id=merchant_id,
            comparison_run_at=ts,
            category=category,
            evidence=evidence,
        )
        if existing is not None:
            return existing

        payload: dict[str, Any] = {
            "merchant_id": str(merchant_id),
            "deal_id": str(deal_id) if deal_id is not None else None,
            "comparison_run_at": ts.isoformat(),
            "legacy_fraud_score": legacy_fraud_score,
            "legacy_tier": legacy_tier,
            "legacy_recommendation": legacy_recommendation,
            "legacy_hard_declines": legacy_hard_declines,
            "track_a_verdict": track_a_verdict,
            "track_b_band": track_b_band,
            "track_c_panel": track_c_panel,
            "category": category,
            "evidence": evidence,
        }

        try:
            result = (
                get_supabase()
                .table("scoring_shadow_disagreements")
                .insert(payload)
                .execute()
            )
        except Exception as exc:
            _log.error(
                "scoring_v2.shadow_disagreement.write_failed category=%s",
                category,
            )
            raise ScoringDisagreementWriteError(
                f"failed to record disagreement for merchant={merchant_id} "
                f"category={category}"
            ) from exc

        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise ScoringDisagreementWriteError(
                "supabase insert returned no row for disagreement"
            )
        return _row_to_record(rows[0])

    def list_open(self) -> list[ScoringDisagreementRecord]:
        """Read the open-triage view (migration 038)."""
        try:
            result = (
                get_supabase()
                .table("scoring_disagreements_open")
                .select("*")
                .execute()
            )
        except Exception as exc:
            raise ScoringDisagreementWriteError(
                "failed to read scoring_disagreements_open view"
            ) from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        # The view drops triage_* columns by design (open == not triaged).
        # _row_to_record handles the missing-key path; defensive fill:
        for r in rows:
            r.setdefault("triaged_by", None)
            r.setdefault("triaged_at", None)
            r.setdefault("triage_decision", None)
            r.setdefault("triage_notes", None)
        return [_row_to_record(r) for r in rows]


# ---------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------


def record_disagreement(
    repo: ScoringDisagreementRepository,
    *,
    merchant_id: UUID,
    deal_id: UUID | None,
    category: str,
    legacy_fraud_score: int | None,
    legacy_tier: str | None,
    legacy_recommendation: str | None,
    legacy_hard_declines: list[str] | None,
    track_a_verdict: str | None,
    track_b_band: str | None,
    track_c_panel: dict[str, Any] | None,
    evidence: dict[str, Any] | None,
    comparison_run_at: datetime | None = None,
) -> ScoringDisagreementRecord:
    """Persist a disagreement row via the supplied repository.

    Thin façade so the comparison script imports a single function and
    the repo is the dependency-injection seam swappable in tests
    (InMemory) and prod (Supabase).
    """
    return repo.record(
        merchant_id=merchant_id,
        deal_id=deal_id,
        category=category,
        legacy_fraud_score=legacy_fraud_score,
        legacy_tier=legacy_tier,
        legacy_recommendation=legacy_recommendation,
        legacy_hard_declines=legacy_hard_declines,
        track_a_verdict=track_a_verdict,
        track_b_band=track_b_band,
        track_c_panel=track_c_panel,
        evidence=evidence,
        comparison_run_at=comparison_run_at,
    )


__all__ = [
    "ALLOWED_CATEGORIES",
    "ALLOWED_TRIAGE_DECISIONS",
    "CATEGORY_AGREEMENT",
    "CATEGORY_AMBIGUOUS",
    "CATEGORY_INSUFFICIENT",
    "CATEGORY_NEW_BETTER",
    "CATEGORY_OLD_BETTER",
    "InMemoryScoringDisagreementRepository",
    "ScoringDisagreementRecord",
    "ScoringDisagreementRepository",
    "ScoringDisagreementWriteError",
    "SupabaseScoringDisagreementRepository",
    "_evidence_hash",
    "record_disagreement",
]


# Pydantic models advertise their fields via Field default; included so
# mypy --strict doesn't complain about the unused import path.
_ = Field
