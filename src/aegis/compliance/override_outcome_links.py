"""Override → outcome flywheel link (build plan §9.2).

Junction table that connects an operator override (Phase 10 / Stage 2D-main,
migrations 017 + 072) to the eventual deal outcome (migration 074). The
table itself is created by migration 098.

Auto-link policy
----------------
When a new ``deal_outcomes`` row is written for merchant M, the
``record_deal_outcome`` handler in ``aegis.web.routers.merchants`` calls
``link_overrides_for_outcome(merchant_id=M, outcome_id=O, …)``:

  1. Read every ``overrides`` row for merchant M.
  2. For each, INSERT one ``override_outcome_links`` row keyed on
     (override_id, outcome_id).
  3. Audit ``override.outcome_linked`` per link created.

The link is a DERIVED flywheel artifact — if the link insert fails AFTER
the outcome insert succeeded, the failure audits as
``override.outcome_link_failed`` but the deal_outcomes row stays. The
operator-visible outcome data is the source of truth; the link is the
analytics seam.

Accuracy formula
----------------
For a given reason_code R, the accuracy is::

    accuracy(R) = right_calls(R) / linked_overrides_with_R

where ``right_calls`` depends on the reason_code's semantic direction:

  * ``score_too_conservative`` — operator overrode AEGIS's
    manual_review/decline to proceed. A subsequent ``funded`` /
    ``paying`` / ``paid_in_full`` outcome is a right call; a
    ``charged_off`` / ``defaulted`` outcome is a wrong call.
  * ``score_too_aggressive`` — operator overrode AEGIS's proceed to
    decline. Inverse: a ``charged_off`` / ``defaulted`` would have
    been a right call (the operator avoided a loss). This flips for
    ``score_too_aggressive`` accuracy reporting.
  * Every other ``reason_code`` (funder_specific_fit, gut, etc.)
    reports the funded / declined / charged-off breakdown without
    a directional "right call" — those rows describe context, not
    a calibration signal.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from aegis.audit import AuditLog, AuditWriteError
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Repository protocol + in-memory + Supabase impls
# ---------------------------------------------------------------------------


class OverrideOutcomeLinkRepository(Protocol):
    """Append-only write + bounded read interface for the link table."""

    def list_override_ids_for_merchant(self, merchant_id: UUID) -> list[UUID]:
        """Return every override.id row pinned to ``merchant_id``.

        Bounded by the per-merchant override count — typical merchants
        accumulate <5 overrides over their lifecycle so the unbounded
        read stays predictable. Falls back to empty list on DB failure;
        the auto-link callsite is opportunistic and a failure here
        MUST NOT block the deal_outcomes write.
        """

    def insert_link(self, override_id: UUID, outcome_id: UUID) -> UUID:
        """Persist one (override_id, outcome_id) row; return its UUID.

        The (override_id, outcome_id) UNIQUE constraint in migration 098
        rejects duplicate pairs cleanly — callers must treat the
        ``LinkAlreadyExistsError`` raise as success-for-idempotency.
        """

    def list_links_with_outcomes(self) -> list[dict[str, Any]]:
        """Return every link row joined to its outcome's terminal state.

        Powers the accuracy report. Each row carries
        ``{override_id, outcome_id, outcome, funder_decision}`` so the
        aggregator can split right vs wrong calls per reason_code.
        Failure returns an empty list — the summary view degrades
        gracefully to "no linked outcomes yet" rather than 500.
        """


class LinkAlreadyExistsError(RuntimeError):
    """Raised when (override_id, outcome_id) already linked.

    Auto-link is idempotent by design — a duplicate insert is success,
    not failure. Callers should catch this and treat it as a no-op.
    """


class LinkWriteError(RuntimeError):
    """Raised when the link insert failed for a non-UNIQUE reason.

    The auto-link caller logs + audits a ``override.outcome_link_failed``
    row and continues — the deal_outcomes write already landed.
    """


class InMemoryOverrideOutcomeLinkRepository:
    """List-backed repository for tests + memory storage backend."""

    def __init__(
        self,
        *,
        overrides_by_merchant: dict[UUID, list[UUID]] | None = None,
        outcome_states: dict[UUID, dict[str, Any]] | None = None,
    ) -> None:
        # Mirror of the overrides table's (merchant_id → [override_id])
        # projection that the auto-link query needs. Tests seed this
        # directly rather than wire a full override repo.
        self._overrides_by_merchant: dict[UUID, list[UUID]] = {
            k: list(v) for k, v in (overrides_by_merchant or {}).items()
        }
        # Mirror of deal_outcomes for the joined accuracy read. Keyed by
        # outcome_id → {"outcome": str, "funder_decision": str}.
        self._outcome_states: dict[UUID, dict[str, Any]] = dict(outcome_states or {})
        self._links: list[dict[str, Any]] = []

    def seed_override(self, merchant_id: UUID, override_id: UUID) -> None:
        """Test seam: register an override for a merchant.

        Production paths get the same projection from the Supabase
        backend's ``overrides`` table query.
        """
        self._overrides_by_merchant.setdefault(merchant_id, []).append(override_id)

    def seed_outcome(
        self,
        outcome_id: UUID,
        *,
        outcome: str,
        funder_decision: str,
    ) -> None:
        """Test seam: register a deal_outcomes row for the joined read."""
        self._outcome_states[outcome_id] = {
            "outcome": outcome,
            "funder_decision": funder_decision,
        }

    def list_override_ids_for_merchant(self, merchant_id: UUID) -> list[UUID]:
        return list(self._overrides_by_merchant.get(merchant_id, []))

    def insert_link(self, override_id: UUID, outcome_id: UUID) -> UUID:
        for existing in self._links:
            if existing["override_id"] == override_id and existing["outcome_id"] == outcome_id:
                raise LinkAlreadyExistsError(
                    f"link already exists for override={override_id} outcome={outcome_id}"
                )
        link_id = uuid4()
        self._links.append(
            {
                "id": link_id,
                "override_id": override_id,
                "outcome_id": outcome_id,
            }
        )
        return link_id

    def list_links_with_outcomes(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for link in self._links:
            state = self._outcome_states.get(cast(UUID, link["outcome_id"]))
            if state is None:
                # Link exists but outcome was deleted out-of-band — rare.
                # Surface as a row with outcome=None so the aggregator can
                # treat it as "linked but not graded".
                out.append(
                    {
                        "override_id": str(link["override_id"]),
                        "outcome_id": str(link["outcome_id"]),
                        "outcome": None,
                        "funder_decision": None,
                    }
                )
                continue
            out.append(
                {
                    "override_id": str(link["override_id"]),
                    "outcome_id": str(link["outcome_id"]),
                    "outcome": state.get("outcome"),
                    "funder_decision": state.get("funder_decision"),
                }
            )
        return out

    def rows(self) -> list[dict[str, Any]]:  # pragma: no cover — debugging aid
        return [dict(r) for r in self._links]


class SupabaseOverrideOutcomeLinkRepository:
    """Persists each link to the ``override_outcome_links`` table."""

    def list_override_ids_for_merchant(self, merchant_id: UUID) -> list[UUID]:
        try:
            result = (
                get_supabase()
                .table("overrides")
                .select("id")
                .eq("merchant_id", str(merchant_id))
                .execute()
            )
        except Exception:
            _log.warning(
                "override_outcome_links.list_overrides_failed merchant_id=%s",
                merchant_id,
            )
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        out: list[UUID] = []
        for r in rows:
            raw = r.get("id") if isinstance(r, dict) else None
            if not raw:
                continue
            try:
                out.append(UUID(str(raw)))
            except (ValueError, TypeError):
                continue
        return out

    def insert_link(self, override_id: UUID, outcome_id: UUID) -> UUID:
        link_id = uuid4()
        body = {
            "id": str(link_id),
            "override_id": str(override_id),
            "outcome_id": str(outcome_id),
        }
        try:
            get_supabase().table("override_outcome_links").insert(body).execute()
        except Exception as exc:
            # Unique-violation surfaces as a generic exception from
            # supabase-py; the auto-link callsite treats LinkWriteError
            # as "audit + continue", so we map every failure to that
            # type and let the message describe the underlying cause.
            message = str(exc).lower()
            if "duplicate" in message or "unique" in message or "23505" in message:
                raise LinkAlreadyExistsError(
                    f"link already exists for override={override_id} outcome={outcome_id}"
                ) from exc
            _log.error(
                "override_outcome_links.write_failed override_id=%s outcome_id=%s",
                override_id,
                outcome_id,
            )
            raise LinkWriteError(
                f"failed to write override_outcome_links row "
                f"override={override_id} outcome={outcome_id}"
            ) from exc
        return link_id

    def list_links_with_outcomes(self) -> list[dict[str, Any]]:
        # supabase-py PostgREST embed: pull link rows with the joined
        # deal_outcomes row (only outcome + funder_decision columns) in
        # one round-trip. The "!inner" disambiguation isn't required
        # because the FK is unambiguous.
        try:
            result = (
                get_supabase()
                .table("override_outcome_links")
                .select("override_id,outcome_id,deal_outcomes(outcome,funder_decision)")
                .execute()
            )
        except Exception:
            _log.warning("override_outcome_links.list_failed")
            return []
        out: list[dict[str, Any]] = []
        for r in cast(list[dict[str, Any]], result.data or []):
            if not isinstance(r, dict):
                continue
            outcome_state = r.get("deal_outcomes") or {}
            if isinstance(outcome_state, list):
                # PostgREST returns a list when the embedded resource is
                # a 1:many FK; here it's many:1 from link→outcome so a
                # single dict is expected but we defensively unwrap.
                outcome_state = outcome_state[0] if outcome_state else {}
            out.append(
                {
                    "override_id": r.get("override_id"),
                    "outcome_id": r.get("outcome_id"),
                    "outcome": (
                        outcome_state.get("outcome") if isinstance(outcome_state, dict) else None
                    ),
                    "funder_decision": (
                        outcome_state.get("funder_decision")
                        if isinstance(outcome_state, dict)
                        else None
                    ),
                }
            )
        return out


# ---------------------------------------------------------------------------
# Auto-link convenience (called from the deal_outcomes write path)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkAttempt:
    """Result of one auto-link attempt for an override → outcome pair.

    ``link_id`` is the new row's UUID on success or on idempotent
    re-link (the existing row is implied — we don't query it back to
    keep the path cheap). ``error`` is None on success / idempotent
    re-link; otherwise carries the failure description.
    """

    override_id: UUID
    outcome_id: UUID
    link_id: UUID | None
    error: str | None


def link_overrides_for_outcome(
    *,
    merchant_id: UUID,
    outcome_id: UUID,
    repo: OverrideOutcomeLinkRepository,
    audit: AuditLog,
    actor: str = "dashboard",
    actor_email: str | None = None,
) -> list[LinkAttempt]:
    """Connect every override for ``merchant_id`` to the new outcome.

    Side-effect ordering per build plan §9.2:

      1. ``repo.list_override_ids_for_merchant`` — bounded read; failure
         returns empty list which means "no overrides to link" which is
         the same as "merchant had no overrides" — safe.
      2. For each override_id: ``repo.insert_link``. Three outcomes:

         * Success → audit ``override.outcome_linked``.
         * ``LinkAlreadyExistsError`` → idempotent re-run; no audit
           (the original link's audit row is the durable record).
         * ``LinkWriteError`` → audit ``override.outcome_link_failed``
           with the override_id + outcome_id + error string. The
           caller continues to the next override.

    Audit-write failures within the link audit row use the same
    AuditWriteError-fails-the-operation discipline as the rest of the
    codebase (CLAUDE.md non-negotiable). The audit failure propagates
    to the caller; the link row itself stays (the analytics seam is
    secondary to the audit gap surfacing).

    Returns one ``LinkAttempt`` per override processed so the caller
    can include the count in its own audit / response.
    """
    override_ids = repo.list_override_ids_for_merchant(merchant_id)
    attempts: list[LinkAttempt] = []
    for override_id in override_ids:
        attempts.append(
            _attempt_one_link(
                override_id=override_id,
                outcome_id=outcome_id,
                merchant_id=merchant_id,
                repo=repo,
                audit=audit,
                actor=actor,
                actor_email=actor_email,
            )
        )
    return attempts


def _attempt_one_link(
    *,
    override_id: UUID,
    outcome_id: UUID,
    merchant_id: UUID,
    repo: OverrideOutcomeLinkRepository,
    audit: AuditLog,
    actor: str,
    actor_email: str | None,
) -> LinkAttempt:
    """Insert one link row + record the matching audit row.

    Extracted so the top-level loop in ``link_overrides_for_outcome``
    keeps a single responsibility (iterate) and the per-pair branches
    don't smear across the loop body.
    """
    try:
        link_id = repo.insert_link(override_id, outcome_id)
    except LinkAlreadyExistsError:
        # Idempotent re-link — no audit row. The original link's audit
        # row is the durable record.
        return LinkAttempt(
            override_id=override_id,
            outcome_id=outcome_id,
            link_id=None,
            error=None,
        )
    except LinkWriteError as exc:
        # Audit the failure but DO NOT propagate — the outcome row
        # already landed, the link is a derived analytics artifact.
        try:
            audit.record(
                actor=actor,
                actor_email=actor_email,
                action="override.outcome_link_failed",
                subject_type="override",
                subject_id=override_id,
                details={
                    "override_id": str(override_id),
                    "outcome_id": str(outcome_id),
                    "merchant_id": str(merchant_id),
                    "error": str(exc),
                },
            )
        except AuditWriteError:
            # Audit-write of a failure-row itself failed — log and
            # continue. The link is already lost; surfacing a 500
            # here would only mask the outcome write that DID land.
            _log.error(
                "override_outcome_links.audit_failure_unrecordable override_id=%s outcome_id=%s",
                override_id,
                outcome_id,
            )
        return LinkAttempt(
            override_id=override_id,
            outcome_id=outcome_id,
            link_id=None,
            error=str(exc),
        )

    # Success path — audit the link creation. Audit-write failure
    # propagates per CLAUDE.md discipline.
    audit.record(
        actor=actor,
        actor_email=actor_email,
        action="override.outcome_linked",
        subject_type="override",
        subject_id=override_id,
        details={
            "override_id": str(override_id),
            "outcome_id": str(outcome_id),
            "merchant_id": str(merchant_id),
            "link_id": str(link_id),
        },
    )
    return LinkAttempt(
        override_id=override_id,
        outcome_id=outcome_id,
        link_id=link_id,
        error=None,
    )


# ---------------------------------------------------------------------------
# Accuracy aggregation (powers /ui/overrides/summary extension)
# ---------------------------------------------------------------------------


# Outcomes that count as "funded" for the override-accuracy report. The
# deal_outcomes CHECK constraint enumerates {paying, paid_in_full,
# charged_off, defaulted, renewed, pending} — paying / paid_in_full /
# renewed all indicate the deal moved to funding (the operator's
# "score_too_conservative" override panned out).
_FUNDED_OUTCOMES: frozenset[str] = frozenset({"paying", "paid_in_full", "renewed"})
# Loss-side outcomes — the deal funded but lost money. For
# ``score_too_conservative`` this is a wrong call; for
# ``score_too_aggressive`` it would have been a right call.
_LOSS_OUTCOMES: frozenset[str] = frozenset({"charged_off", "defaulted"})
# Reason codes whose accuracy reading is "operator overrode to proceed —
# did the deal actually fund cleanly?" A funded / paying / paid_in_full
# outcome is a right call; loss outcomes are wrong calls.
_CONSERVATIVE_REASON_CODES: frozenset[str] = frozenset({"score_too_conservative"})
# Reason codes whose accuracy reading is inverted — operator overrode to
# decline; a loss outcome on those that DID get funded somehow would
# have been a right call. In practice score_too_aggressive overrides
# rarely have a downstream outcome (the deal got declined), but we
# count the inverse for the rare re-routed case.
_AGGRESSIVE_REASON_CODES: frozenset[str] = frozenset({"score_too_aggressive"})


@dataclass(frozen=True)
class ReasonAccuracyRow:
    """One row of the override-accuracy report.

    ``total_overrides`` is the count of overrides under this reason
    that have at least one linked outcome. ``funded`` / ``loss`` /
    ``declined`` / ``pending`` partition that count. ``right_calls``
    + ``accuracy_pct`` are derived per the reason-code direction
    (see module docstring). Reason codes that have no directional
    semantic carry ``accuracy_pct = None``.
    """

    reason_code: str
    total_overrides: int
    funded: int
    loss: int
    declined: int
    pending: int
    right_calls: int | None
    accuracy_pct: float | None


@dataclass(frozen=True)
class FlywheelSummary:
    """Aggregate flywheel stats for the overrides summary page header.

    ``total_overrides`` and ``linked_overrides`` come from the override
    table + link table joined counts; ``linked_pct`` is the ratio
    expressed as a 0-100 float (None when ``total_overrides == 0`` so
    the template can render "—" cleanly).
    """

    total_overrides: int
    linked_overrides: int
    linked_pct: float | None
    funded_pct: float | None
    declined_pct: float | None
    loss_pct: float | None


def _is_funded(outcome: str | None, funder_decision: str | None) -> bool:
    """An outcome counts as funded if its terminal state is funded-like.

    The funder_decision='declined' case can coexist with outcome=None
    when the operator records the funder turning the deal down but the
    deal never reached a post-fund state — those count as declines, not
    funded. Mirrors the convention in the deal_outcomes router.
    """
    if outcome and outcome in _FUNDED_OUTCOMES:
        return True
    return False


def _is_loss(outcome: str | None) -> bool:
    return bool(outcome and outcome in _LOSS_OUTCOMES)


def _is_declined(outcome: str | None, funder_decision: str | None) -> bool:
    """Declined surface: funder turned the deal down before fund.

    Tracks the funder_decision='declined' rows whose outcome stayed
    pending (deal never funded). The override-accuracy report counts
    these as a separate bucket from loss outcomes (which require
    post-fund losses).
    """
    if funder_decision and funder_decision.lower() == "declined":
        return True
    return False


def build_reason_accuracy_rows(
    overrides_rows: Iterable[dict[str, Any]],
    link_rows: Iterable[dict[str, Any]],
) -> list[ReasonAccuracyRow]:
    """Aggregate (overrides, links) into one row per reason_code.

    Order:
      1. Index overrides by id → reason_code.
      2. Walk link rows, attaching the outcome columns to each
         override's reason_code bucket.
      3. Per reason_code, partition into funded / loss / declined /
         pending and compute the right-call count where the reason
         has a directional semantic.

    Empty inputs → empty list. Sorting matches build_reason_code_summary:
    by linked-total descending, then reason_code ascending for ties.
    """
    # Step 1: override_id → reason_code lookup.
    reason_by_override: dict[str, str] = {}
    for row in overrides_rows:
        oid = row.get("id")
        reason = row.get("reason_code")
        if not oid or not reason:
            continue
        reason_by_override[str(oid)] = str(reason)

    # Step 2: bucket link rows by reason_code.
    @dataclass
    class _Bucket:
        total: int = 0
        funded: int = 0
        loss: int = 0
        declined: int = 0
        pending: int = 0

    buckets: dict[str, _Bucket] = {}
    for link in link_rows:
        oid = link.get("override_id")
        if not oid:
            continue
        reason = reason_by_override.get(str(oid))
        if reason is None:
            # Link exists for an override we don't have in the overrides
            # projection — skip rather than guess the reason.
            continue
        bucket = buckets.setdefault(reason, _Bucket())
        bucket.total += 1
        outcome = link.get("outcome")
        funder_decision = link.get("funder_decision")
        if _is_funded(outcome, funder_decision):
            bucket.funded += 1
        elif _is_loss(outcome):
            bucket.loss += 1
        elif _is_declined(outcome, funder_decision):
            bucket.declined += 1
        else:
            bucket.pending += 1

    out: list[ReasonAccuracyRow] = []
    for reason, bucket in buckets.items():
        right, pct = _compute_accuracy(reason, bucket.funded, bucket.loss, bucket.total)
        out.append(
            ReasonAccuracyRow(
                reason_code=reason,
                total_overrides=bucket.total,
                funded=bucket.funded,
                loss=bucket.loss,
                declined=bucket.declined,
                pending=bucket.pending,
                right_calls=right,
                accuracy_pct=pct,
            )
        )

    out.sort(key=lambda r: (-r.total_overrides, r.reason_code))
    return out


def _compute_accuracy(
    reason_code: str,
    funded: int,
    loss: int,
    total: int,
) -> tuple[int | None, float | None]:
    """Return (right_call_count, accuracy_pct) for one reason bucket.

    Conservative reasons (operator overrode AEGIS's
    manual_review/decline to proceed): funded = right, loss = wrong.
    Aggressive reasons: inverse. Other reason codes return (None, None)
    so the template renders an em-dash.

    Pending + declined-by-funder rows are excluded from the denominator
    on purpose — they have no signal on whether the operator's call
    was right yet.
    """
    if reason_code in _CONSERVATIVE_REASON_CODES:
        denom = funded + loss
        if denom == 0:
            return 0, None
        return funded, round(100.0 * funded / denom, 1)
    if reason_code in _AGGRESSIVE_REASON_CODES:
        denom = funded + loss
        if denom == 0:
            return 0, None
        # For aggressive overrides, the right call is the loss avoidance
        # — but since these rows only exist when the deal funded
        # *despite* the operator declining, the "right call" measure
        # is the loss count (the deal did blow up; operator was right).
        return loss, round(100.0 * loss / denom, 1)
    return None, None


def build_flywheel_summary(
    total_overrides: int,
    rows: list[ReasonAccuracyRow],
) -> FlywheelSummary:
    """Roll up the per-reason rows into the page-header summary."""
    linked_overrides = sum(r.total_overrides for r in rows)
    funded_total = sum(r.funded for r in rows)
    loss_total = sum(r.loss for r in rows)
    declined_total = sum(r.declined for r in rows)

    linked_pct = (
        round(100.0 * linked_overrides / total_overrides, 1) if total_overrides > 0 else None
    )

    def _pct(numerator: int) -> float | None:
        if linked_overrides == 0:
            return None
        return round(100.0 * numerator / linked_overrides, 1)

    return FlywheelSummary(
        total_overrides=total_overrides,
        linked_overrides=linked_overrides,
        linked_pct=linked_pct,
        funded_pct=_pct(funded_total),
        declined_pct=_pct(declined_total),
        loss_pct=_pct(loss_total),
    )


__all__ = [
    "FlywheelSummary",
    "InMemoryOverrideOutcomeLinkRepository",
    "LinkAlreadyExistsError",
    "LinkAttempt",
    "LinkWriteError",
    "OverrideOutcomeLinkRepository",
    "ReasonAccuracyRow",
    "SupabaseOverrideOutcomeLinkRepository",
    "build_flywheel_summary",
    "build_reason_accuracy_rows",
    "link_overrides_for_outcome",
]
