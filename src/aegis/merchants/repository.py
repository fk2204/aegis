"""Merchant persistence.

Mirrors the funder pattern: ``MerchantRepository`` Protocol +
``InMemoryMerchantRepository`` for tests + ``SupabaseMerchantRepository``
for production. Uniqueness invariants enforced at this layer:

  * ``close_lead_id`` (when set) is unique — the Close-sync idempotency
    key. The DB also enforces this via a partial UNIQUE index (migration
    026); this layer raises early.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID, uuid4

from postgrest.exceptions import APIError
from pydantic import BaseModel, ConfigDict

from aegis.db import get_supabase
from aegis.merchants.models import MerchantNoteRow, MerchantRow


def _is_close_lead_id_unique_violation(exc: APIError) -> bool:
    """``True`` for the partial-unique-index race on ``close_lead_id``.

    Postgres SQLSTATE 23505 plus the index name in ``details`` is the
    signal — anything else (a different unique constraint, a different
    error class) re-raises unchanged.
    """
    code = getattr(exc, "code", None)
    if code != "23505":
        return False
    details = getattr(exc, "details", None) or ""
    message = getattr(exc, "message", None) or ""
    return "close_lead_id" in details or "close_lead_id" in message


if TYPE_CHECKING:
    from aegis.merchants.renewal_attestations import (
        RenewalAttestationRepository,
    )

_log = logging.getLogger(__name__)


class MerchantNotFoundError(KeyError):
    """Raised when a merchant id (or close_lead_id) has no row."""


class MerchantConflictError(ValueError):
    """Raised when a uniqueness constraint would be violated."""


# Placeholder ``business_name`` written by ``create_provisional`` and
# overwritten by ``finalize_provisional`` once the worker has read
# ``statement.account_holder``. Keeps ``business_name`` non-null at the
# type level so the slugify / dossier / sort cascade doesn't need
# None-guards. The OFAC + scoring paths are status-gated separately
# (see the ``is_finalized`` checks in ``web/router.py``) so this
# placeholder never reaches a regulatory or scoring surface.
PROVISIONAL_BUSINESS_NAME_PLACEHOLDER = "(awaiting parse)"


# Per ``.claude/rules/compliance.md`` SCOPE NOTE: AEGIS does not own
# regulator-facing renewal disclosure issuance — funder partners do. The
# state-deadline columns below (CA SB 362 60-day pre-maturity / NY
# § 600.17 30-day pre-maturity) are surfaced for OPERATOR VISIBILITY so
# the operator can verify the funder has sent the required notice. The
# row count is NOT used to drive a broker-side enforcement gate.
_STATE_DISCLOSURE_LEAD_DAYS: dict[str, int] = {
    "CA": 60,  # SB 362 — 60-day pre-maturity renewal disclosure
    "NY": 30,  # 23 NYCRR § 600.17 — 30-day pre-maturity renewal disclosure
}


# How many days before the state-disclosure deadline AEGIS flips a row
# from ``not_required_funder_owns`` (default, no urgency) to
# ``disclosure_pending`` (operator should check that the funder has sent
# the notice). The 14-day window mirrors the operator-visibility framing
# of the renewal calendar: a 14-day countdown is short enough to be a
# real prompt and long enough to give the operator time to chase the
# funder before the regulator-facing deadline.
_DISCLOSURE_PENDING_LEAD_DAYS: int = 14


class RenewalSummary(BaseModel):
    """One row on the operator's upcoming-renewals calendar.

    Pure projection — never persisted. Built by ``list_upcoming_renewals``
    from ``MerchantRow`` + the in-Python state-deadline lookup. None on
    ``days_until_state_deadline`` means the merchant's state has no
    renewal-disclosure deadline AEGIS tracks (i.e. anything other than
    CA or NY). None on ``maturity_date`` is impossible by construction —
    the accessor only returns rows whose maturity is known and lies in
    the lookahead window.

    ``renewal_status`` is one of:

      * ``"disclosure_sent"``         — operator attested via the
        ``funder_renewal_attestations`` table (migration 040 / U6) that
        the funder transmitted the required notice.
      * ``"disclosure_pending"``      — no attestation AND the state
        deadline is within 14 days but not past — the operator should
        chase the funder.
      * ``"disclosure_overdue"``      — no attestation AND the state
        deadline has already passed.
      * ``"not_required_funder_owns"`` — default. Either the merchant
        is in a state with no AEGIS-tracked renewal deadline (anything
        outside CA / NY) or the deadline is > 14 days out.

    The status never drives a broker-side enforcement gate — AEGIS is a
    pure ISO broker (see CLAUDE.md SCOPE NOTE) and funders own the
    regulator-facing obligation. The status is operator-visibility
    framing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    merchant_id: UUID
    business_name: str
    state: str | None
    industry_naics: str | None
    maturity_date: date
    days_until_maturity: int
    days_until_state_deadline: int | None
    renewal_status: str


class RenewalPipelineRow(BaseModel):
    """One row on the 14-day renewal-pipeline queue at /ui/renewals.

    Distinct from :class:`RenewalSummary`: that summary drives the
    90-day state-disclosure-deadline calendar (the regulator-facing
    surface, which funders own). This row drives the operator
    re-engagement queue — "which merchants are inside the window
    where I should reach out about a renewal advance?"

    Pure projection — never persisted. ``last_score_tier`` and
    ``suggested_renewal_amount`` are read tolerantly: a merchant
    without a stored decision or analysis surfaces ``None`` for both
    and the dossier column renders an em-dash. ``days_until_maturity``
    may be negative — overdue maturities still appear (operator can
    chase the stale renewal) and the template highlights them in red.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    merchant_id: UUID
    business_name: str
    maturity_date: date
    days_until_maturity: int
    last_score_tier: str | None
    # Decimal per CLAUDE.md money-math rule. ``None`` when the merchant
    # had no requested_amount and no prior decision to fall back on.
    suggested_renewal_amount: Decimal | None


_RENEWAL_PIPELINE_WINDOW_DAYS: int = 14


def list_renewal_pipeline(
    repo: MerchantRepository,
    *,
    today: date | None = None,
) -> list[RenewalPipelineRow]:
    """Renewals queue: merchants whose ``maturity_date`` is within the
    next 14 days (or overdue).

    Implementation choice (b) — derived directly from
    ``merchants.maturity_date``. ``funding_date + term_days`` are not
    yet separately captured on the merchants table; ``maturity_date``
    (migration 039) already encodes the sum. The operator's spec asks
    for "within 2 weeks of renewal eligible" which is exactly
    ``maturity_date <= today + 14``.

    Overdue merchants (``maturity_date < today``) DO appear — the
    operator may need to chase a stale renewal. Sort order is
    ``maturity_date`` ascending, so the soonest-due / most-overdue
    row lands at the top.

    ``last_score_tier`` and ``suggested_renewal_amount`` default to
    ``None`` here — the production wiring layer reads the latest
    ``DecisionSnapshot`` row and patches them in via a list
    comprehension at the route site. Keeping them ``None`` at the
    repo layer avoids dragging a decisions dependency into this
    module.
    """
    as_of = today or datetime.now(UTC).date()
    cutoff = as_of.toordinal() + _RENEWAL_PIPELINE_WINDOW_DAYS
    rows: list[RenewalPipelineRow] = []
    for m in repo.list_all():
        # Pipeline is the RENEWAL queue. Filter out non-renewal merchants
        # so the operator-decision view stays focused; first-deal
        # maturities live elsewhere in the flow.
        if not m.is_renewal:
            continue
        maturity = m.maturity_date
        if maturity is None:
            continue
        if maturity.toordinal() > cutoff:
            continue
        rows.append(
            RenewalPipelineRow(
                merchant_id=m.id,
                business_name=m.business_name,
                maturity_date=maturity,
                days_until_maturity=(maturity - as_of).days,
                last_score_tier=None,
                # Decimal money — CLAUDE.md "never float for money".
                # ``requested_amount`` is already Money (Decimal-backed)
                # so the assignment round-trips cleanly through
                # Pydantic.
                suggested_renewal_amount=m.requested_amount,
            )
        )
    rows.sort(key=lambda r: r.maturity_date)
    return rows


def _derive_renewal_status(
    *,
    days_until_state_deadline: int | None,
    has_attestation: bool,
) -> str:
    """Compute one of the four renewal-status values.

    Logic (U6 — operator-side attestation flow):

      * ``disclosure_sent``           — an attestation row exists for this
        (merchant, maturity_date) tuple.
      * ``disclosure_overdue``        — no attestation AND
        ``days_until_state_deadline < 0`` (deadline already past).
      * ``disclosure_pending``        — no attestation AND
        ``0 <= days_until_state_deadline < 14`` (within the operator-
        prompt window).
      * ``not_required_funder_owns``  — default. No attestation AND either
        the merchant is in a state with no AEGIS-tracked deadline OR the
        deadline is more than 14 days away.

    Note: this function never inspects ``days_until_maturity`` — only the
    state-deadline-relative urgency drives the status. Per CLAUDE.md
    SCOPE NOTE, AEGIS surfaces operator visibility into the funder's
    obligation, not the maturity itself.
    """
    if has_attestation:
        return "disclosure_sent"
    if days_until_state_deadline is None:
        return "not_required_funder_owns"
    if days_until_state_deadline < 0:
        return "disclosure_overdue"
    if days_until_state_deadline < _DISCLOSURE_PENDING_LEAD_DAYS:
        return "disclosure_pending"
    return "not_required_funder_owns"


def list_upcoming_renewals(
    repo: MerchantRepository,
    *,
    window_days: int = 90,
    today: date | None = None,
    attestations: RenewalAttestationRepository | None = None,
) -> list[RenewalSummary]:
    """List renewing merchants whose maturity falls within ``window_days``.

    Reads from the supplied ``MerchantRepository``. Returns rows sorted by
    ``days_until_maturity`` ascending (most urgent first).

    Logic (migration 039 — ``maturity_date`` column landed):

      * Filter to ``is_renewal=True AND maturity_date IS NOT NULL``.
      * Compute ``days_until_maturity = maturity_date - today``.
      * Drop rows where ``days_until_maturity < 0`` or
        ``> window_days``.
      * Look up the state-disclosure lead from
        ``_STATE_DISCLOSURE_LEAD_DAYS`` (CA=60, NY=30, else None) and
        compute ``days_until_state_deadline =
        days_until_maturity - lead_days``.
      * When ``attestations`` is provided (U6 — migration 040), consult
        ``funder_renewal_attestations`` for each (merchant, maturity)
        pair to flip the per-row status off the default. The four-state
        logic lives in ``_derive_renewal_status``.

    The ``attestations`` parameter is optional so existing callers that
    don't care about attestation status (CSV export, in-process tests
    pre-U6) keep working — when omitted, every row defaults to
    ``not_required_funder_owns`` as before.
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")
    as_of = today or datetime.now(UTC).date()
    rows = list(repo.list_all())
    candidates: list[tuple[MerchantRow, date]] = []
    for m in rows:
        if not m.is_renewal:
            continue
        maturity = m.maturity_date
        if maturity is None:
            continue
        candidates.append((m, maturity))
    summaries: list[RenewalSummary] = []
    for m, maturity in candidates:
        delta_days = (maturity - as_of).days
        if delta_days < 0 or delta_days > window_days:
            continue
        lead = _STATE_DISCLOSURE_LEAD_DAYS.get((m.state or "").upper())
        days_until_state_deadline = delta_days - lead if lead is not None else None
        # When the caller passes ``attestations`` (U6 — migration 040)
        # we consult the table and compute one of the four statuses.
        # When the caller omits the repo (legacy callers, CSV export
        # paths, pre-U6 in-process tests), we preserve the previous
        # behavior: every row defaults to ``not_required_funder_owns``.
        if attestations is None:
            renewal_status = "not_required_funder_owns"
        else:
            has_attestation = bool(
                attestations.find_for_renewal(merchant_id=m.id, maturity_date=maturity)
            )
            renewal_status = _derive_renewal_status(
                days_until_state_deadline=days_until_state_deadline,
                has_attestation=has_attestation,
            )
        summaries.append(
            RenewalSummary(
                merchant_id=m.id,
                business_name=m.business_name,
                state=m.state,
                industry_naics=m.industry_naics,
                maturity_date=maturity,
                days_until_maturity=delta_days,
                days_until_state_deadline=days_until_state_deadline,
                renewal_status=renewal_status,
            )
        )
    summaries.sort(key=lambda s: s.days_until_maturity)
    if not summaries:
        _log.info("renewals: no renewals in window (window_days=%d)", window_days)
    return summaries


class MerchantRepository(Protocol):
    def get(self, merchant_id: UUID) -> MerchantRow: ...
    def find_by_close_lead_id(
        self,
        close_lead_id: str,
        *,
        include_deleted: bool = False,
    ) -> MerchantRow | None: ...
    def find_by_close_opportunity_id(self, close_opportunity_id: str) -> MerchantRow | None: ...
    def find_by_email(self, email: str) -> MerchantRow | None: ...
    def list_all(self, *, state: str | None = None) -> list[MerchantRow]: ...
    def count_total(self) -> int: ...
    def upsert(self, merchant: MerchantRow) -> MerchantRow: ...
    def delete(self, merchant_id: UUID) -> None: ...

    # Migration 065 — operator-initiated soft-delete ----------------------------

    def soft_delete(self, merchant_id: UUID, *, deleted_at: datetime) -> MerchantRow:
        """Mark a merchant as soft-deleted; return the updated row.

        Sets ``merchants.deleted_at`` to the supplied timestamp. After
        this call the merchant is invisible to every read method on the
        repository (``get``, ``list_all``, ``find_by_*``,
        ``count_total``) — that's the whole point of the soft-delete:
        the row keeps its history (documents, transactions, analyses,
        decisions, audit log) but the dossier surface stops rendering
        it.

        Raises ``MerchantNotFoundError`` when the row is unknown OR
        already soft-deleted. The "already deleted" path guards against
        a double-submit that would otherwise silently re-stamp the
        timestamp and produce a spurious audit row at the caller.
        """

    # Migration 034 — merchant-from-statement flow ---------------------------

    def create_provisional(self) -> MerchantRow:
        """Insert a fresh row with ``status='provisional'`` and NULL
        ``business_name`` / ``owner_name`` / ``state``. Used by the
        dashboard ``/ui/upload`` auto-create branch when the operator
        uploads without picking a merchant.
        """

    def finalize_provisional(self, *, merchant_id: UUID, business_name: str) -> int:
        """Transition ``provisional`` or ``needs_manual_naming`` →
        ``finalized``, setting ``business_name``. ``owner_name`` and
        ``state`` are intentionally NOT touched (see migration 034 +
        design doc §10 confirmed decision 1).

        Idempotent: filtered on ``status IN ('provisional',
        'needs_manual_naming')`` so re-parses, operator-manual
        finalizations, and concurrent paths each see at most one
        successful UPDATE. Returns the rowcount so the worker can
        gate its ``merchant.finalized`` audit row on observed change
        (operator-required — false audit rows are unacceptable).
        """

    def mark_needs_manual_naming(self, *, merchant_id: UUID) -> int:
        """Transition ``provisional`` → ``needs_manual_naming``. Used by
        the worker when parse-completion can't auto-name (blank
        ``account_holder``), when the parse raised, when the parse
        was cancelled (arq timeout), and when the processor branch
        succeeds (no ``account_holder`` analogue today).

        Idempotent: filtered on ``status='provisional'`` so an
        already-finalized merchant or an already-needs-naming row is
        a no-op. Returns the rowcount; the worker gates its audit
        row on observed change.
        """

    # Migration 066 — operator notes (Feature C) ---------------------------

    def add_note(
        self,
        *,
        merchant_id: UUID,
        body: str,
        actor: str,
    ) -> MerchantNoteRow:
        """Persist one operator note row.

        Body is trimmed by the caller; this method does not re-validate
        the trim. The 4000-char cap is enforced at both the application
        layer (the route returns 400) and the DB (CHECK constraint), but
        the model field validation is the canonical gate — passing a
        body longer than ``MERCHANT_NOTE_MAX_CHARS`` raises a
        Pydantic ValidationError.
        """

    def list_notes(
        self,
        *,
        merchant_id: UUID,
        limit: int = 50,
    ) -> list[MerchantNoteRow]:
        """Return notes for one merchant, newest-first.

        Drives the dossier operator-notes card list. Bounded ``limit``
        keeps the read predictable even when a merchant accumulates many
        notes over time.
        """

    # Feature D — merchant context fields (migration 064) ----------------

    def set_deal_context(self, merchant_id: UUID, text: str | None) -> int:
        """Persist ``deal_context`` for the merchant.

        IO-only — the calling route writes the
        ``merchant.deal_context.updated`` audit row. Returns the rowcount
        (0 when the merchant doesn't exist) so the caller can gate its
        audit + redirect on observed change. ``text=None`` clears the
        column; empty string is treated identically by the route.
        """

    def set_close_context(
        self,
        merchant_id: UUID,
        *,
        notes_summary: str | None,
        lead_description: str | None,
        call_transcripts: str | None,
    ) -> int:
        """Persist the three Close-derived context columns atomically.

        IO-only — the calling orchestrator
        (``aegis.merchants.close_context``) writes the
        ``merchant.close_context.refreshed`` audit row with the counts
        only. Bodies are NEVER passed through ``audit_log`` per
        CLAUDE.md PII rule. Returns the rowcount.
        """


class InMemoryMerchantRepository:
    """Dict-backed merchant store. Tests + offline."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, MerchantRow] = {}
        # Migration 066 — Feature C operator-notes panel. List-backed so
        # the round-trip mirrors the Supabase ``merchant_notes`` table
        # newest-first semantics. Pre-066 ``merchants.notes`` text-column
        # data is NOT migrated into here automatically — the new panel
        # only renders rows added via ``add_note``.
        self._notes: list[MerchantNoteRow] = []

    def get(self, merchant_id: UUID) -> MerchantRow:
        # Migration 065 — soft-deleted rows are invisible to read paths.
        # Treating them as not-found makes the existing 404 cascade on
        # the dossier / edit / match routes behave correctly without
        # any caller changes.
        try:
            row = self._by_id[merchant_id]
        except KeyError as exc:
            raise MerchantNotFoundError(str(merchant_id)) from exc
        if row.deleted_at is not None:
            raise MerchantNotFoundError(str(merchant_id))
        return row

    def find_by_close_lead_id(
        self,
        close_lead_id: str,
        *,
        include_deleted: bool = False,
    ) -> MerchantRow | None:
        for m in self._by_id.values():
            if not include_deleted and m.deleted_at is not None:
                continue
            if m.close_lead_id == close_lead_id:
                return m
        return None

    def find_by_close_opportunity_id(self, close_opportunity_id: str) -> MerchantRow | None:
        for m in self._by_id.values():
            if m.deleted_at is not None:
                continue
            if m.close_opportunity_id == close_opportunity_id:
                return m
        return None

    def find_by_email(self, email: str) -> MerchantRow | None:
        needle = email.strip().lower()
        if not needle:
            return None
        for m in self._by_id.values():
            if m.deleted_at is not None:
                continue
            if m.email and m.email.strip().lower() == needle:
                return m
        return None

    def list_all(self, *, state: str | None = None) -> list[MerchantRow]:
        # Migration 065 — filter out soft-deleted rows. The list surface
        # is the dominant read path (every dashboard, every match /
        # portfolio / renewals projection iterates it), so the filter
        # lands here once.
        rows = [m for m in self._by_id.values() if m.deleted_at is None]
        if state is not None:
            s = state.upper()
            rows = [m for m in rows if m.state == s]
        return sorted(rows, key=lambda m: m.business_name.lower())

    def count_total(self) -> int:
        return sum(1 for m in self._by_id.values() if m.deleted_at is None)

    def upsert(self, merchant: MerchantRow) -> MerchantRow:
        # Enforce uniqueness on close_lead_id (DB partial-UNIQUE index
        # enforces this at the storage layer; raise early in-memory too).
        if merchant.close_lead_id is not None:
            for existing in self._by_id.values():
                if existing.id != merchant.id and existing.close_lead_id == merchant.close_lead_id:
                    raise MerchantConflictError(
                        f"close_lead_id {merchant.close_lead_id!r} "
                        f"already on merchant {existing.id}"
                    )
        # Same partial-UNIQUE on close_opportunity_id (migration 054).
        if merchant.close_opportunity_id is not None:
            for existing in self._by_id.values():
                if (
                    existing.id != merchant.id
                    and existing.close_opportunity_id == merchant.close_opportunity_id
                ):
                    raise MerchantConflictError(
                        f"close_opportunity_id {merchant.close_opportunity_id!r} "
                        f"already on merchant {existing.id}"
                    )
        if merchant.id not in self._by_id:
            merchant = merchant.model_copy(
                update={"id": merchant.id or uuid4(), "created_at": datetime.now(UTC)}
            )
        merchant = merchant.model_copy(update={"updated_at": datetime.now(UTC)})
        self._by_id[merchant.id] = merchant
        return merchant

    def delete(self, merchant_id: UUID) -> None:
        self._by_id.pop(merchant_id, None)

    # Migration 065 — operator-initiated soft-delete ----------------------------

    def soft_delete(self, merchant_id: UUID, *, deleted_at: datetime) -> MerchantRow:
        # ``self._by_id`` retains soft-deleted rows (the whole point of
        # soft-delete), so we peek at the underlying dict directly
        # instead of going through ``get`` — ``get`` already filters
        # them out as not-found.
        existing = self._by_id.get(merchant_id)
        if existing is None or existing.deleted_at is not None:
            raise MerchantNotFoundError(str(merchant_id))
        updated = existing.model_copy(update={"deleted_at": deleted_at, "updated_at": deleted_at})
        self._by_id[merchant_id] = updated
        return updated

    # Migration 034 — merchant-from-statement flow ---------------------------

    def create_provisional(self) -> MerchantRow:
        now = datetime.now(UTC)
        row = MerchantRow(
            id=uuid4(),
            status="provisional",
            business_name=PROVISIONAL_BUSINESS_NAME_PLACEHOLDER,
            owner_name=None,
            state=None,
            created_at=now,
            updated_at=now,
        )
        self._by_id[row.id] = row
        return row

    def finalize_provisional(self, *, merchant_id: UUID, business_name: str) -> int:
        existing = self._by_id.get(merchant_id)
        if existing is None:
            return 0
        if existing.status not in ("provisional", "needs_manual_naming"):
            return 0
        updated = existing.model_copy(
            update={
                "status": "finalized",
                "business_name": business_name,
                "updated_at": datetime.now(UTC),
            }
        )
        self._by_id[merchant_id] = updated
        return 1

    def mark_needs_manual_naming(self, *, merchant_id: UUID) -> int:
        existing = self._by_id.get(merchant_id)
        if existing is None:
            return 0
        if existing.status != "provisional":
            return 0
        updated = existing.model_copy(
            update={
                "status": "needs_manual_naming",
                "updated_at": datetime.now(UTC),
            }
        )
        self._by_id[merchant_id] = updated
        return 1

    # Migration 066 — operator notes (Feature C) ---------------------------

    def add_note(
        self,
        *,
        merchant_id: UUID,
        body: str,
        actor: str,
    ) -> MerchantNoteRow:
        # MerchantNoteRow's Field constraints enforce the 1..4000 char
        # cap + non-empty actor at construction time — passing through
        # validation here so the in-memory backend matches the DB CHECK.
        row = MerchantNoteRow(
            merchant_id=merchant_id,
            body=body,
            actor=actor,
            created_at=datetime.now(UTC),
        )
        self._notes.append(row)
        return row

    def list_notes(
        self,
        *,
        merchant_id: UUID,
        limit: int = 50,
    ) -> list[MerchantNoteRow]:
        matches = [n for n in self._notes if n.merchant_id == merchant_id]
        # Newest-first. created_at is set at insert time on the in-memory
        # path so None never appears, but sort tolerantly anyway so a
        # future SQL-direct seed test doesn't crash on a None timestamp.
        matches.sort(
            key=lambda n: n.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return matches[: max(0, limit)]

    # Feature D — merchant context fields (migration 064) ----------------

    def set_deal_context(self, merchant_id: UUID, text: str | None) -> int:
        existing = self._by_id.get(merchant_id)
        if existing is None or existing.deleted_at is not None:
            return 0
        normalized = text if text else None
        updated = existing.model_copy(
            update={
                "deal_context": normalized,
                "updated_at": datetime.now(UTC),
            }
        )
        self._by_id[merchant_id] = updated
        return 1

    def set_close_context(
        self,
        merchant_id: UUID,
        *,
        notes_summary: str | None,
        lead_description: str | None,
        call_transcripts: str | None,
    ) -> int:
        existing = self._by_id.get(merchant_id)
        if existing is None or existing.deleted_at is not None:
            return 0
        updated = existing.model_copy(
            update={
                "close_notes_summary": notes_summary,
                "close_lead_description": lead_description,
                "close_call_transcripts": call_transcripts,
                "updated_at": datetime.now(UTC),
            }
        )
        self._by_id[merchant_id] = updated
        return 1


class SupabaseMerchantRepository:
    """Persistence backed by Postgres ``merchants`` table."""

    def get(self, merchant_id: UUID) -> MerchantRow:
        # Migration 065 — every read path filters ``deleted_at IS NULL``
        # so a soft-deleted merchant 404s on the dossier, edit form,
        # match panel, etc. without any caller changes. ``is_("deleted_at",
        # "null")`` is the supabase-py PostgREST idiom for ``IS NULL``.
        result = (
            get_supabase()
            .table("merchants")
            .select("*")
            .eq("id", str(merchant_id))
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        if not result.data:
            raise MerchantNotFoundError(str(merchant_id))
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def find_by_close_lead_id(
        self,
        close_lead_id: str,
        *,
        include_deleted: bool = False,
    ) -> MerchantRow | None:
        # ``include_deleted=True`` is used by the Close webhook handler's
        # soft-delete suppression check: the partial unique index on
        # ``close_lead_id`` includes soft-deleted rows, so an operator-
        # soft-deleted merchant blocks INSERTs that the active-row filter
        # can't see. The handler ACKs Close silently when the matching
        # row turns out to be soft-deleted.
        query = get_supabase().table("merchants").select("*").eq("close_lead_id", close_lead_id)
        if not include_deleted:
            query = query.is_("deleted_at", "null")
        result = query.limit(1).execute()
        if not result.data:
            return None
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def find_by_close_opportunity_id(self, close_opportunity_id: str) -> MerchantRow | None:
        result = (
            get_supabase()
            .table("merchants")
            .select("*")
            .eq("close_opportunity_id", close_opportunity_id)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def find_by_email(self, email: str) -> MerchantRow | None:
        needle = email.strip()
        if not needle:
            return None
        result = (
            get_supabase()
            .table("merchants")
            .select("*")
            .ilike("email", needle)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def list_all(self, *, state: str | None = None) -> list[MerchantRow]:
        query = (
            get_supabase()
            .table("merchants")
            .select("*")
            .is_("deleted_at", "null")
            .order("business_name")
        )
        if state is not None:
            query = query.eq("state", state.upper())
        result = query.execute()
        return [_row_to_merchant(cast(dict[str, Any], r)) for r in (result.data or [])]

    def count_total(self) -> int:
        try:
            result = (
                get_supabase()
                .table("merchants")
                .select("id")
                .is_("deleted_at", "null")
                .limit(10000)
                .execute()
            )
        except Exception:
            return 0
        return len(result.data or [])

    def upsert(self, merchant: MerchantRow) -> MerchantRow:
        payload = _merchant_to_payload(merchant)
        # ``ON CONFLICT (id) DO UPDATE`` semantics via supabase-py upsert().
        # The ``close_lead_id`` uniqueness is enforced by a PARTIAL unique
        # index (migration 026, ``WHERE close_lead_id IS NOT NULL``), which
        # Postgres ON CONFLICT can't target through supabase-py — so we
        # translate the resulting 23505 from a concurrent Close webhook
        # redelivery into :class:`MerchantConflictError` and let the
        # caller resolve the race with a read-and-retry.
        try:
            result = get_supabase().table("merchants").upsert(payload, on_conflict="id").execute()
        except APIError as exc:
            if _is_close_lead_id_unique_violation(exc):
                raise MerchantConflictError(
                    f"close_lead_id {merchant.close_lead_id!r} already present "
                    "(race against concurrent webhook redelivery)"
                ) from exc
            raise
        if not result.data:
            raise RuntimeError("supabase.upsert returned no row")
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def delete(self, merchant_id: UUID) -> None:
        get_supabase().table("merchants").delete().eq("id", str(merchant_id)).execute()

    # Migration 065 — operator-initiated soft-delete ----------------------------

    def soft_delete(self, merchant_id: UUID, *, deleted_at: datetime) -> MerchantRow:
        # ``UPDATE ... WHERE id = ? AND deleted_at IS NULL`` so a
        # double-submit raises NotFound rather than silently
        # re-stamping the timestamp. PostgREST returns the updated
        # rows by default; ``result.data`` empty == 0 rows matched.
        result = (
            get_supabase()
            .table("merchants")
            .update({"deleted_at": deleted_at.isoformat()})
            .eq("id", str(merchant_id))
            .is_("deleted_at", "null")
            .execute()
        )
        if not result.data:
            raise MerchantNotFoundError(str(merchant_id))
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    # Migration 034 — merchant-from-statement flow ---------------------------

    def create_provisional(self) -> MerchantRow:
        # Placeholder business_name (non-null at the type level — see
        # PROVISIONAL_BUSINESS_NAME_PLACEHOLDER for why). owner_name and
        # state stay NULL until operator edits or — in owner_name's case
        # — never (account_holder is the business, not the human owner).
        # No email / phone / intake fields written.
        payload: dict[str, Any] = {
            "status": "provisional",
            "business_name": PROVISIONAL_BUSINESS_NAME_PLACEHOLDER,
        }
        result = get_supabase().table("merchants").insert(payload).execute()
        if not result.data:
            raise RuntimeError("supabase.insert returned no row")
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def finalize_provisional(self, *, merchant_id: UUID, business_name: str) -> int:
        # Filtered on the two in-flux statuses so a row already
        # finalized (operator manual edit) or any other state never
        # gets silently overwritten. Returns the rowcount via the
        # length of result.data (PostgREST returns the updated rows
        # by default).
        result = (
            get_supabase()
            .table("merchants")
            .update({"status": "finalized", "business_name": business_name})
            .eq("id", str(merchant_id))
            .in_("status", ["provisional", "needs_manual_naming"])
            .execute()
        )
        return len(result.data or [])

    def mark_needs_manual_naming(self, *, merchant_id: UUID) -> int:
        # Filtered on status='provisional' so an already-needs-naming
        # or already-finalized row is a true no-op (rowcount 0). The
        # worker uses the returned count to gate its audit row so a
        # false claim of state change never lands.
        result = (
            get_supabase()
            .table("merchants")
            .update({"status": "needs_manual_naming"})
            .eq("id", str(merchant_id))
            .eq("status", "provisional")
            .execute()
        )
        return len(result.data or [])

    # Migration 066 — operator notes (Feature C) ---------------------------

    def add_note(
        self,
        *,
        merchant_id: UUID,
        body: str,
        actor: str,
    ) -> MerchantNoteRow:
        # Validate at the model layer first so the 1..4000 / actor-non-
        # empty contract is enforced identically on both backends — and
        # so we never round-trip through Postgres only to fail the
        # CHECK constraint with a less actionable error.
        candidate = MerchantNoteRow(
            merchant_id=merchant_id,
            body=body,
            actor=actor,
        )
        payload: dict[str, Any] = {
            "id": str(candidate.id),
            "merchant_id": str(candidate.merchant_id),
            "body": candidate.body,
            "actor": candidate.actor,
        }
        result = get_supabase().table("merchant_notes").insert(payload).execute()
        if not result.data:
            raise RuntimeError("supabase.insert returned no row for merchant_notes")
        return _row_to_merchant_note(cast(dict[str, Any], result.data[0]))

    def list_notes(
        self,
        *,
        merchant_id: UUID,
        limit: int = 50,
    ) -> list[MerchantNoteRow]:
        result = (
            get_supabase()
            .table("merchant_notes")
            .select("*")
            .eq("merchant_id", str(merchant_id))
            .order("created_at", desc=True)
            .limit(max(0, limit))
            .execute()
        )
        return [_row_to_merchant_note(cast(dict[str, Any], r)) for r in (result.data or [])]

    # Feature D — merchant context fields (migration 064) ----------------

    def set_deal_context(self, merchant_id: UUID, text: str | None) -> int:
        # Filter on ``deleted_at IS NULL`` so a soft-deleted merchant is
        # a true no-op (rowcount 0) — mirrors the in-memory contract and
        # protects the operator from a stale-tab POST landing on a
        # dossier that's been hidden.
        normalized = text if text else None
        result = (
            get_supabase()
            .table("merchants")
            .update({"deal_context": normalized})
            .eq("id", str(merchant_id))
            .is_("deleted_at", "null")
            .execute()
        )
        return len(result.data or [])

    def set_close_context(
        self,
        merchant_id: UUID,
        *,
        notes_summary: str | None,
        lead_description: str | None,
        call_transcripts: str | None,
    ) -> int:
        # Atomic three-column update; PostgREST applies the SET list in
        # one UPDATE so a transient Close failure on one slot can't leave
        # the other two in a half-refreshed state at the DB.
        result = (
            get_supabase()
            .table("merchants")
            .update(
                {
                    "close_notes_summary": notes_summary,
                    "close_lead_description": lead_description,
                    "close_call_transcripts": call_transcripts,
                }
            )
            .eq("id", str(merchant_id))
            .is_("deleted_at", "null")
            .execute()
        )
        return len(result.data or [])


def _row_to_merchant_note(row: dict[str, Any]) -> MerchantNoteRow:
    """Map a ``merchant_notes`` table row to ``MerchantNoteRow``.

    Mirrors the ``_row_to_merchant`` precedent. ``created_at`` is NOT NULL
    on the table side; the optional accessor + ``_parse_dt`` keep parity
    with the model's nullable annotation in case a future schema drift
    surfaces a NULL.
    """
    return MerchantNoteRow(
        id=UUID(row["id"]),
        merchant_id=UUID(row["merchant_id"]),
        body=row["body"],
        actor=row["actor"],
        created_at=_parse_dt(row.get("created_at")),
    )


def _none_if_empty(value: object) -> object:
    """Collapse empty / whitespace-only strings to ``None``; pass other
    values through unchanged.

    Hardens ``_row_to_merchant`` against pre-fix writes that landed
    ``""`` on nullable text columns. Surfaced by ADG Global Express
    on 2026-06-19: a Close webhook firing before fix `1966afa` had
    deployed wrote ``owner_name=""`` to its row; later bulk
    ``list_all()`` reads crashed Pydantic ``string_too_short``
    validation on every consumer of the API. Coercing on read means
    one poisoned row no longer blocks the rest of the table from
    hydrating, and the next webhook write through the fixed path
    (now `_str_or_none`-guarded) normalizes the column to NULL
    organically. Booleans / decimals / ints / dates / lists are
    unaffected because they're never strings on Supabase reads.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _row_to_merchant(row: dict[str, Any]) -> MerchantRow:
    # Migration 034 made owner_name / state nullable; business_name
    # stays NOT NULL (provisional rows carry a placeholder string per
    # the model docstring). ``status`` defaults to ``'finalized'`` for
    # safety against a pre-034 read (replica, restored backup) — matches
    # the DB DEFAULT on the column.
    #
    # Every nullable text column flows through ``_none_if_empty`` —
    # see the docstring there for the ADG-Global-Express precedent. The
    # NOT-NULL ``business_name`` is intentionally NOT coerced; an
    # empty business_name is real data corruption that should fail
    # loud, not silently hydrate as ``None``.
    from decimal import Decimal as _Decimal

    return MerchantRow(
        id=UUID(row["id"]),
        status=row.get("status") or "finalized",
        business_name=row["business_name"],
        dba=_none_if_empty(row.get("dba")),
        owner_name=_none_if_empty(row.get("owner_name")),
        state=_none_if_empty(row.get("state")),
        industry_naics=_none_if_empty(row.get("industry_naics")),
        industry_risk_tier=row.get("industry_risk_tier"),
        time_in_business_months=row.get("time_in_business_months"),
        credit_score=row.get("credit_score"),
        email=_none_if_empty(row.get("email")),
        phone=_none_if_empty(row.get("phone")),
        entity_type=row.get("entity_type"),
        ein=_none_if_empty(row.get("ein")),
        requested_amount=(
            _Decimal(str(row["requested_amount"]))
            if row.get("requested_amount") is not None
            else None
        ),
        requested_factor=(
            _Decimal(str(row["requested_factor"]))
            if row.get("requested_factor") is not None
            else None
        ),
        requested_term_days=row.get("requested_term_days"),
        broker_source=_none_if_empty(row.get("broker_source")),
        intake_date=_parse_date(row.get("intake_date")),
        is_renewal=bool(row.get("is_renewal", False)),
        maturity_date=_parse_date(row.get("maturity_date")),
        # Migration 061 — document-on-file flags. Default to False / 0
        # so pre-061 reads (replica, restored backup) surface as
        # "operator must check" rather than silently passing the gate.
        voided_check_on_file=bool(row.get("voided_check_on_file", False)),
        drivers_license_on_file=bool(row.get("drivers_license_on_file", False)),
        bank_statements_months=int(row.get("bank_statements_months") or 0),
        preferred_funder_id=(
            UUID(row["preferred_funder_id"]) if row.get("preferred_funder_id") else None
        ),
        close_lead_id=_none_if_empty(row.get("close_lead_id")),
        close_opportunity_id=_none_if_empty(row.get("close_opportunity_id")),
        industry_choice=_none_if_empty(row.get("industry_choice")),
        notes=_none_if_empty(row.get("notes")),
        # Feature D (migration 064). ``row.get`` collapses missing keys
        # to ``None`` so a pre-064 read (replica, restored backup)
        # surfaces every context field as ``None`` — the safe default
        # (prompt builder treats ``None`` / empty as "omit the line").
        deal_context=_none_if_empty(row.get("deal_context")),
        close_lead_description=_none_if_empty(row.get("close_lead_description")),
        close_notes_summary=_none_if_empty(row.get("close_notes_summary")),
        close_call_transcripts=_none_if_empty(row.get("close_call_transcripts")),
        # Migration 067 — web-presence reputation scan. Pre-067 reads
        # collapse to None / [] which is the "needs first scan" signal
        # the scorer checks before invoking the scanner.
        web_presence_summary=_none_if_empty(row.get("web_presence_summary")),
        web_presence_flags=list(row.get("web_presence_flags") or []),
        web_presence_scanned_at=_parse_dt(row.get("web_presence_scanned_at")),
        # Migration 068 — UCC filings + previous-default search.
        ucc_filings=list(row.get("ucc_filings") or []),
        ucc_default_indicators=list(row.get("ucc_default_indicators") or []),
        ucc_checked_at=_parse_dt(row.get("ucc_checked_at")),
        # Migration 083 — OFAC SDN screening. Pre-083 rows collapse to
        # None which is the "needs first check" signal the scorer
        # hook reads before invoking the screener.
        ofac_checked_at=_parse_dt(row.get("ofac_checked_at")),
        ofac_is_clear=row.get("ofac_is_clear"),
        ofac_match_detail=list(row.get("ofac_match_detail") or []),
        ofac_cache_date=_parse_dt(row.get("ofac_cache_date")),
        # Migration 084 — federal bankruptcy check (CourtListener v4).
        bankruptcy_checked_at=_parse_dt(row.get("bankruptcy_checked_at")),
        bankruptcy_active=row.get("bankruptcy_active"),
        bankruptcy_recent=row.get("bankruptcy_recent"),
        bankruptcy_chapter=row.get("bankruptcy_chapter"),
        bankruptcy_cases=list(row.get("bankruptcy_cases") or []),
        created_at=_parse_dt(row.get("created_at")),
        updated_at=_parse_dt(row.get("updated_at")),
        # Migration 065. None for every pre-065 row + every live row;
        # populated only after ``soft_delete``. Pre-065 reads (replica,
        # restored backup) collapse to None via ``row.get`` which is
        # the correct active-row default.
        deleted_at=_parse_dt(row.get("deleted_at")),
        # Migration 080. ``revenue_based`` for every pre-080 row via
        # the DB DEFAULT; for any forward-compat replica read where the
        # column is missing entirely (pre-080 backup restore), fall back
        # to the same default so the model parse cannot trip.
        product_type=row.get("product_type") or "revenue_based",
    )


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    return None


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _merchant_to_payload(m: MerchantRow) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(m.id),
        "status": m.status,
        "business_name": m.business_name,
        "dba": m.dba,
        "owner_name": m.owner_name,
        "state": m.state.upper() if m.state else None,
        "industry_naics": m.industry_naics,
        "industry_risk_tier": m.industry_risk_tier,
        "time_in_business_months": m.time_in_business_months,
        "credit_score": m.credit_score,
        "email": m.email,
        "phone": m.phone,
        "entity_type": m.entity_type,
        "ein": m.ein,
        "requested_amount": str(m.requested_amount) if m.requested_amount is not None else None,
        "requested_factor": str(m.requested_factor) if m.requested_factor is not None else None,
        "requested_term_days": m.requested_term_days,
        "broker_source": m.broker_source,
        "intake_date": m.intake_date.isoformat() if m.intake_date else None,
        "is_renewal": m.is_renewal,
        "maturity_date": m.maturity_date.isoformat() if m.maturity_date else None,
        "voided_check_on_file": m.voided_check_on_file,
        "drivers_license_on_file": m.drivers_license_on_file,
        "bank_statements_months": m.bank_statements_months,
        "preferred_funder_id": (str(m.preferred_funder_id) if m.preferred_funder_id else None),
        "close_lead_id": m.close_lead_id,
        "close_opportunity_id": m.close_opportunity_id,
        "industry_choice": m.industry_choice,
        "notes": m.notes,
        # Feature D (migration 064). Round-trip through ``upsert`` so a
        # full-row save (e.g. webhook merchant upsert) preserves any
        # context fields previously set. The dedicated ``set_*_context``
        # methods bypass the full-row payload and target the relevant
        # columns directly.
        "deal_context": m.deal_context,
        "close_lead_description": m.close_lead_description,
        "close_notes_summary": m.close_notes_summary,
        "close_call_transcripts": m.close_call_transcripts,
        "web_presence_summary": m.web_presence_summary,
        # supabase-py serialises a Python list to a PostgREST array; the
        # column is text[] so empty list maps to {} which Supabase
        # stores as an empty array (not NULL) — which is what we want
        # for "scanned but no flags" so the scorer doesn't re-fire.
        "web_presence_flags": list(m.web_presence_flags),
        "web_presence_scanned_at": m.web_presence_scanned_at,
        # Migration 068.
        "ucc_filings": list(m.ucc_filings),
        "ucc_default_indicators": list(m.ucc_default_indicators),
        "ucc_checked_at": m.ucc_checked_at,
        # Migration 083 — OFAC SDN screening.
        "ofac_checked_at": m.ofac_checked_at,
        "ofac_is_clear": m.ofac_is_clear,
        "ofac_match_detail": list(m.ofac_match_detail),
        "ofac_cache_date": m.ofac_cache_date,
        # Migration 084 — federal bankruptcy check columns.
        "bankruptcy_checked_at": m.bankruptcy_checked_at,
        "bankruptcy_active": m.bankruptcy_active,
        "bankruptcy_recent": m.bankruptcy_recent,
        "bankruptcy_chapter": m.bankruptcy_chapter,
        "bankruptcy_cases": list(m.bankruptcy_cases),
        # Migration 080 — round-trips on every upsert so the column is
        # always written explicitly (rather than relying on the DB
        # DEFAULT to land on inserts only).
        "product_type": m.product_type,
    }
    return payload


__all__ = [
    "PROVISIONAL_BUSINESS_NAME_PLACEHOLDER",
    "InMemoryMerchantRepository",
    "MerchantConflictError",
    "MerchantNotFoundError",
    "MerchantRepository",
    "RenewalPipelineRow",
    "RenewalSummary",
    "SupabaseMerchantRepository",
    "list_renewal_pipeline",
    "list_upcoming_renewals",
]
