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
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from aegis.db import get_supabase
from aegis.merchants.models import MerchantRow

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


class RenewalSummary(BaseModel):
    """One row on the operator's upcoming-renewals calendar.

    Pure projection — never persisted. Built by ``list_upcoming_renewals``
    from ``MerchantRow`` + the in-Python state-deadline lookup. None on
    ``days_until_state_deadline`` means the merchant's state has no
    renewal-disclosure deadline AEGIS tracks (i.e. anything other than
    CA or NY). None on ``maturity_date`` is impossible by construction —
    the accessor only returns rows whose maturity is known and lies in
    the lookahead window.

    ``renewal_status`` is always ``"not_required_funder_owns"`` today
    because AEGIS is a pure ISO broker (see CLAUDE.md SCOPE NOTE) and
    has no signal on whether the funder has actually transmitted the
    pre-maturity disclosure. The column is shipped so the visual
    framing is honest — and so a future funder-disclosure-attestation
    table can flip the status to ``"disclosure_sent"`` /
    ``"disclosure_pending"`` without a schema change here.
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


def list_upcoming_renewals(
    repo: MerchantRepository,
    *,
    window_days: int = 90,
    today: date | None = None,
) -> list[RenewalSummary]:
    """List renewing merchants whose maturity falls within ``window_days``.

    Reads from the supplied ``MerchantRepository``. Returns rows sorted by
    ``days_until_maturity`` ascending (most urgent first).

    Schema-augmentation gap
    -----------------------
    The ``merchants`` table does NOT have a ``maturity_date`` column
    today (verified 2026-06-09). Per the R3.2 scope this accessor MUST
    NOT auto-migrate the schema — adding a column is an explicit
    operator decision. While the column is absent, this function logs
    one INFO line and returns ``[]``; the route still renders the
    operator-visibility banner and an empty-state message.

    When the column lands (additive migration, no backfill), the
    accessor will:

      * Filter to ``is_renewal=True AND maturity_date IS NOT NULL``.
      * Compute ``days_until_maturity = maturity_date - today``.
      * Drop rows where ``days_until_maturity < 0`` or
        ``> window_days``.
      * Look up the state-disclosure lead from
        ``_STATE_DISCLOSURE_LEAD_DAYS`` (CA=60, NY=30, else None) and
        compute ``days_until_state_deadline =
        days_until_maturity - lead_days``.
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")
    as_of = today or datetime.now(UTC).date()
    rows = list(repo.list_all())
    candidates: list[tuple[MerchantRow, date]] = []
    schema_has_maturity = False
    for m in rows:
        if not m.is_renewal:
            continue
        # ``maturity_date`` is read via getattr so the accessor compiles
        # cleanly before the schema augmentation lands. When the column
        # is added to ``MerchantRow``, this branch resolves the attribute
        # normally and no further code changes are needed here.
        maturity: object = getattr(m, "maturity_date", None)
        if isinstance(maturity, date) and not isinstance(maturity, datetime):
            schema_has_maturity = True
            candidates.append((m, maturity))
    if not schema_has_maturity:
        _log.info(
            "renewals: maturity_date column not present — schema augmentation pending"
        )
        return []
    summaries: list[RenewalSummary] = []
    for m, maturity in candidates:
        delta_days = (maturity - as_of).days
        if delta_days < 0 or delta_days > window_days:
            continue
        lead = _STATE_DISCLOSURE_LEAD_DAYS.get((m.state or "").upper())
        days_until_state_deadline = (
            delta_days - lead if lead is not None else None
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
                renewal_status="not_required_funder_owns",
            )
        )
    summaries.sort(key=lambda s: s.days_until_maturity)
    return summaries


class MerchantRepository(Protocol):
    def get(self, merchant_id: UUID) -> MerchantRow: ...
    def find_by_close_lead_id(
        self, close_lead_id: str
    ) -> MerchantRow | None: ...
    def find_by_email(self, email: str) -> MerchantRow | None: ...
    def list_all(self, *, state: str | None = None) -> list[MerchantRow]: ...
    def count_total(self) -> int: ...
    def upsert(self, merchant: MerchantRow) -> MerchantRow: ...
    def delete(self, merchant_id: UUID) -> None: ...

    # Migration 034 — merchant-from-statement flow ---------------------------

    def create_provisional(self) -> MerchantRow:
        """Insert a fresh row with ``status='provisional'`` and NULL
        ``business_name`` / ``owner_name`` / ``state``. Used by the
        dashboard ``/ui/upload`` auto-create branch when the operator
        uploads without picking a merchant.
        """

    def finalize_provisional(
        self, *, merchant_id: UUID, business_name: str
    ) -> int:
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


class InMemoryMerchantRepository:
    """Dict-backed merchant store. Tests + offline."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, MerchantRow] = {}

    def get(self, merchant_id: UUID) -> MerchantRow:
        try:
            return self._by_id[merchant_id]
        except KeyError as exc:
            raise MerchantNotFoundError(str(merchant_id)) from exc

    def find_by_close_lead_id(self, close_lead_id: str) -> MerchantRow | None:
        for m in self._by_id.values():
            if m.close_lead_id == close_lead_id:
                return m
        return None

    def find_by_email(self, email: str) -> MerchantRow | None:
        needle = email.strip().lower()
        if not needle:
            return None
        for m in self._by_id.values():
            if m.email and m.email.strip().lower() == needle:
                return m
        return None

    def list_all(self, *, state: str | None = None) -> list[MerchantRow]:
        rows = list(self._by_id.values())
        if state is not None:
            s = state.upper()
            rows = [m for m in rows if m.state == s]
        return sorted(rows, key=lambda m: m.business_name.lower())

    def count_total(self) -> int:
        return len(self._by_id)

    def upsert(self, merchant: MerchantRow) -> MerchantRow:
        # Enforce uniqueness on close_lead_id (DB partial-UNIQUE index
        # enforces this at the storage layer; raise early in-memory too).
        if merchant.close_lead_id is not None:
            for existing in self._by_id.values():
                if (
                    existing.id != merchant.id
                    and existing.close_lead_id == merchant.close_lead_id
                ):
                    raise MerchantConflictError(
                        f"close_lead_id {merchant.close_lead_id!r} "
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

    def finalize_provisional(
        self, *, merchant_id: UUID, business_name: str
    ) -> int:
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


class SupabaseMerchantRepository:
    """Persistence backed by Postgres ``merchants`` table."""

    def get(self, merchant_id: UUID) -> MerchantRow:
        result = (
            get_supabase()
            .table("merchants")
            .select("*")
            .eq("id", str(merchant_id))
            .limit(1)
            .execute()
        )
        if not result.data:
            raise MerchantNotFoundError(str(merchant_id))
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def find_by_close_lead_id(self, close_lead_id: str) -> MerchantRow | None:
        result = (
            get_supabase()
            .table("merchants")
            .select("*")
            .eq("close_lead_id", close_lead_id)
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
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def list_all(self, *, state: str | None = None) -> list[MerchantRow]:
        query = get_supabase().table("merchants").select("*").order("business_name")
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
                .limit(10000)
                .execute()
            )
        except Exception:
            return 0
        return len(result.data or [])

    def upsert(self, merchant: MerchantRow) -> MerchantRow:
        payload = _merchant_to_payload(merchant)
        # ``ON CONFLICT (id) DO UPDATE`` semantics via supabase-py upsert().
        result = (
            get_supabase()
            .table("merchants")
            .upsert(payload, on_conflict="id")
            .execute()
        )
        if not result.data:
            raise RuntimeError("supabase.upsert returned no row")
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def delete(self, merchant_id: UUID) -> None:
        get_supabase().table("merchants").delete().eq(
            "id", str(merchant_id)
        ).execute()

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
        result = (
            get_supabase()
            .table("merchants")
            .insert(payload)
            .execute()
        )
        if not result.data:
            raise RuntimeError("supabase.insert returned no row")
        return _row_to_merchant(cast(dict[str, Any], result.data[0]))

    def finalize_provisional(
        self, *, merchant_id: UUID, business_name: str
    ) -> int:
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


def _row_to_merchant(row: dict[str, Any]) -> MerchantRow:
    # Migration 034 made owner_name / state nullable; business_name
    # stays NOT NULL (provisional rows carry a placeholder string per
    # the model docstring). ``status`` defaults to ``'finalized'`` for
    # safety against a pre-034 read (replica, restored backup) — matches
    # the DB DEFAULT on the column.
    from decimal import Decimal as _Decimal

    return MerchantRow(
        id=UUID(row["id"]),
        status=row.get("status") or "finalized",
        business_name=row["business_name"],
        dba=row.get("dba"),
        owner_name=row.get("owner_name"),
        state=row.get("state"),
        industry_naics=row.get("industry_naics"),
        industry_risk_tier=row.get("industry_risk_tier"),
        time_in_business_months=row.get("time_in_business_months"),
        credit_score=row.get("credit_score"),
        email=row.get("email"),
        phone=row.get("phone"),
        entity_type=row.get("entity_type"),
        ein=row.get("ein"),
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
        broker_source=row.get("broker_source"),
        intake_date=_parse_date(row.get("intake_date")),
        is_renewal=bool(row.get("is_renewal", False)),
        preferred_funder_id=(
            UUID(row["preferred_funder_id"])
            if row.get("preferred_funder_id")
            else None
        ),
        close_lead_id=row.get("close_lead_id"),
        created_at=_parse_dt(row.get("created_at")),
        updated_at=_parse_dt(row.get("updated_at")),
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
        "preferred_funder_id": (
            str(m.preferred_funder_id) if m.preferred_funder_id else None
        ),
        "close_lead_id": m.close_lead_id,
    }
    return payload


__all__ = [
    "PROVISIONAL_BUSINESS_NAME_PLACEHOLDER",
    "InMemoryMerchantRepository",
    "MerchantConflictError",
    "MerchantNotFoundError",
    "MerchantRepository",
    "RenewalSummary",
    "SupabaseMerchantRepository",
    "list_upcoming_renewals",
]
