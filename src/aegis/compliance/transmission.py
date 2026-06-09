"""Disclosure transmission audit-trail helper (R0.5).

CA 10 CCR § 952 + NY 23 NYCRR § 600 require a four-year audit trail of
every commercial-financing disclosure transmitted to a merchant. This
module owns the write path for the ``disclosure_transmissions`` table
(migration 036) — one row per transmission event carrying the disclosed
financial terms, the rendered-HTML hash, the recipient channel, and a
STORED 4-year ``retention_until`` floor.

Two implementations of the ``DisclosureTransmissionRepository`` Protocol:

  * ``InMemoryDisclosureTransmissionRepository`` — list-backed, used by
    tests and the in-memory backend.
  * ``SupabaseDisclosureTransmissionRepository`` — writes one row per
    ``record()`` call to Postgres. Insert failure raises so the calling
    pipeline can refuse to mark the disclosure as transmitted.

The shape mirrors the schema verbatim so a regulator-shaped audit query
(``SELECT ... WHERE state = 'CA' AND sent_at BETWEEN ... ``) can read
the table without joining anything else.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


class DisclosureTransmissionWriteError(RuntimeError):
    """Raised when a transmission row could not be persisted.

    Mirrors ``AuditWriteError`` semantics: a write failure halts the
    calling operation rather than letting it ship a disclosure with no
    audit trail. The four-year retention requirement is meaningless if
    the row is silently dropped.
    """


class DisclosureTransmissionRecord(BaseModel):
    """One transmission event. Pydantic so callers cannot pass loose dicts."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: UUID
    deal_id: UUID | None
    merchant_id: UUID | None
    state: str
    disclosure_version: str
    template_path: str
    html_sha256: str
    recipient_email: str | None
    sent_at: datetime
    sent_by: str | None
    apr: Decimal | None
    funding_provided: Decimal | None
    finance_charge: Decimal | None
    estimated_total_payment: Decimal | None
    estimated_term_days: int | None
    factor_rate: Decimal | None
    holdback_pct: Decimal | None
    metadata: dict[str, Any] | None


class DisclosureTransmissionRepository(Protocol):
    """Append-only interface. Implementations must raise on write failure."""

    def record(
        self,
        *,
        deal_id: UUID | None,
        merchant_id: UUID | None,
        state: str,
        disclosure_version: str,
        template_path: str,
        rendered_html: str,
        recipient_email: str | None,
        sent_by: str | None,
        apr: Decimal | None,
        funding_provided: Decimal | None,
        finance_charge: Decimal | None,
        estimated_total_payment: Decimal | None,
        estimated_term_days: int | None,
        factor_rate: Decimal | None,
        holdback_pct: Decimal | None,
        sent_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DisclosureTransmissionRecord: ...


def _sha256_hex(s: str) -> str:
    """Lowercase hex sha256 of the UTF-8 bytes of ``s``."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _normalize_state(state: str) -> str:
    """USPS 2-letter uppercase. Defensive — caller may pass mixed case."""
    upper = (state or "").upper()
    if len(upper) != 2:
        raise ValueError(f"state must be a 2-letter USPS code, got {state!r}")
    return upper


class InMemoryDisclosureTransmissionRepository:
    """List-backed implementation. Used by tests and the memory backend."""

    def __init__(self) -> None:
        self.rows: list[DisclosureTransmissionRecord] = []

    def record(
        self,
        *,
        deal_id: UUID | None,
        merchant_id: UUID | None,
        state: str,
        disclosure_version: str,
        template_path: str,
        rendered_html: str,
        recipient_email: str | None,
        sent_by: str | None,
        apr: Decimal | None,
        funding_provided: Decimal | None,
        finance_charge: Decimal | None,
        estimated_total_payment: Decimal | None,
        estimated_term_days: int | None,
        factor_rate: Decimal | None,
        holdback_pct: Decimal | None,
        sent_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DisclosureTransmissionRecord:
        record = DisclosureTransmissionRecord(
            id=uuid4(),
            deal_id=deal_id,
            merchant_id=merchant_id,
            state=_normalize_state(state),
            disclosure_version=disclosure_version,
            template_path=template_path,
            html_sha256=_sha256_hex(rendered_html),
            recipient_email=recipient_email,
            sent_at=sent_at or datetime.now(UTC),
            sent_by=sent_by,
            apr=apr,
            funding_provided=funding_provided,
            finance_charge=finance_charge,
            estimated_total_payment=estimated_total_payment,
            estimated_term_days=estimated_term_days,
            factor_rate=factor_rate,
            holdback_pct=holdback_pct,
            metadata=metadata,
        )
        self.rows.append(record)
        return record


class SupabaseDisclosureTransmissionRepository:
    """Persistence backed by Postgres ``disclosure_transmissions`` table.

    Mirrors the in-memory contract; Postgres-side STORED column computes
    ``retention_until`` so callers cannot accidentally undershoot the
    4-year floor.
    """

    def record(
        self,
        *,
        deal_id: UUID | None,
        merchant_id: UUID | None,
        state: str,
        disclosure_version: str,
        template_path: str,
        rendered_html: str,
        recipient_email: str | None,
        sent_by: str | None,
        apr: Decimal | None,
        funding_provided: Decimal | None,
        finance_charge: Decimal | None,
        estimated_total_payment: Decimal | None,
        estimated_term_days: int | None,
        factor_rate: Decimal | None,
        holdback_pct: Decimal | None,
        sent_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DisclosureTransmissionRecord:
        norm_state = _normalize_state(state)
        html_sha = _sha256_hex(rendered_html)
        ts = sent_at or datetime.now(UTC)

        def _money_or_none(v: Decimal | None) -> str | None:
            return str(v) if v is not None else None

        payload: dict[str, Any] = {
            "deal_id": str(deal_id) if deal_id is not None else None,
            "merchant_id": str(merchant_id) if merchant_id is not None else None,
            "state": norm_state,
            "disclosure_version": disclosure_version,
            "template_path": template_path,
            "html_sha256": html_sha,
            "recipient_email": recipient_email,
            "sent_at": ts.isoformat(),
            "sent_by": sent_by,
            "apr": _money_or_none(apr),
            "funding_provided": _money_or_none(funding_provided),
            "finance_charge": _money_or_none(finance_charge),
            "estimated_total_payment": _money_or_none(estimated_total_payment),
            "estimated_term_days": estimated_term_days,
            "factor_rate": _money_or_none(factor_rate),
            "holdback_pct": _money_or_none(holdback_pct),
            "metadata": metadata,
        }

        try:
            result = (
                get_supabase()
                .table("disclosure_transmissions")
                .insert(payload)
                .execute()
            )
        except Exception as exc:
            _log.error(
                "compliance.disclosure_transmission.write_failed state=%s template=%s",
                norm_state,
                template_path,
            )
            raise DisclosureTransmissionWriteError(
                f"failed to record disclosure transmission for state={norm_state}"
            ) from exc

        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise DisclosureTransmissionWriteError(
                "supabase insert returned no row for disclosure transmission"
            )
        row = rows[0]

        def _money(key: str) -> Decimal | None:
            v = row.get(key)
            return Decimal(str(v)) if v is not None else None

        return DisclosureTransmissionRecord(
            id=UUID(row["id"]),
            deal_id=UUID(row["deal_id"]) if row.get("deal_id") else None,
            merchant_id=UUID(row["merchant_id"]) if row.get("merchant_id") else None,
            state=row["state"],
            disclosure_version=row["disclosure_version"],
            template_path=row["template_path"],
            html_sha256=row["html_sha256"],
            recipient_email=row.get("recipient_email"),
            sent_at=(
                datetime.fromisoformat(str(row["sent_at"]).replace("Z", "+00:00"))
                if not isinstance(row["sent_at"], datetime)
                else row["sent_at"]
            ),
            sent_by=row.get("sent_by"),
            apr=_money("apr"),
            funding_provided=_money("funding_provided"),
            finance_charge=_money("finance_charge"),
            estimated_total_payment=_money("estimated_total_payment"),
            estimated_term_days=row.get("estimated_term_days"),
            factor_rate=_money("factor_rate"),
            holdback_pct=_money("holdback_pct"),
            metadata=row.get("metadata"),
        )


def record_disclosure_transmission(
    repo: DisclosureTransmissionRepository,
    *,
    deal_id: UUID | None,
    merchant_id: UUID | None,
    state: str,
    disclosure_version: str,
    template_path: str,
    rendered_html: str,
    recipient_email: str | None,
    sent_by: str | None,
    apr: Decimal | None,
    funding_provided: Decimal | None,
    finance_charge: Decimal | None,
    estimated_total_payment: Decimal | None,
    estimated_term_days: int | None,
    factor_rate: Decimal | None,
    holdback_pct: Decimal | None,
    sent_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DisclosureTransmissionRecord:
    """Helper: compute html_sha256 + write the audit row in one call.

    Use from the disclosure-send pipeline immediately after the rendered
    HTML lands in the merchant's inbox (or after the operator confirms
    out-of-band transmission). Failure to write the row raises
    ``DisclosureTransmissionWriteError`` — the pipeline must propagate
    rather than silently mark the disclosure as transmitted.

    Mirrors the ``record_disclosure_transmission(...)`` signature the
    R0.5 audit asked for; ``repo`` is the explicit dependency injection
    point so callers swap in-memory in tests and Supabase in prod.
    """
    return repo.record(
        deal_id=deal_id,
        merchant_id=merchant_id,
        state=state,
        disclosure_version=disclosure_version,
        template_path=template_path,
        rendered_html=rendered_html,
        recipient_email=recipient_email,
        sent_by=sent_by,
        apr=apr,
        funding_provided=funding_provided,
        finance_charge=finance_charge,
        estimated_total_payment=estimated_total_payment,
        estimated_term_days=estimated_term_days,
        factor_rate=factor_rate,
        holdback_pct=holdback_pct,
        sent_at=sent_at,
        metadata=metadata,
    )


__all__ = [
    "DisclosureTransmissionRecord",
    "DisclosureTransmissionRepository",
    "DisclosureTransmissionWriteError",
    "InMemoryDisclosureTransmissionRepository",
    "SupabaseDisclosureTransmissionRepository",
    "record_disclosure_transmission",
]
