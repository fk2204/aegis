"""Funder persistence layer.

`FunderRepository` is the Protocol the rest of the system depends on.
`InMemoryFunderRepository` is a reference implementation backed by a
dict — sufficient for tests and the parser-level integration before
Phase 5 wires Supabase.

Phase 5 adds `SupabaseFunderRepository` (same Protocol, talks to
`funders` Postgres table). At that point matchers + the dashboard will
swap implementations without code changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID

from aegis.db import get_supabase
from aegis.funders.models import FunderRow, FunderTier


class FunderNotFoundError(KeyError):
    """Raised when a funder id has no matching row."""


class FunderRepository(Protocol):
    """CRUD interface for funders. Implementations enforce uniqueness on `name`."""

    def get(self, funder_id: UUID) -> FunderRow:
        """Return the funder with this id. Raises `FunderNotFoundError` otherwise."""

    def list_active(self) -> list[FunderRow]:
        """Return all active funders, ordered by name."""

    def upsert(self, funder: FunderRow) -> FunderRow:
        """Insert or replace by id; uniqueness on name is enforced."""

    def delete(self, funder_id: UUID) -> None:
        """Remove the funder. No-op if not present."""


class InMemoryFunderRepository:
    """Dict-backed implementation. Phase 5 swaps for Supabase-backed."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, FunderRow] = {}

    def get(self, funder_id: UUID) -> FunderRow:
        try:
            return self._by_id[funder_id]
        except KeyError as exc:
            raise FunderNotFoundError(str(funder_id)) from exc

    def list_active(self) -> list[FunderRow]:
        return sorted(
            (f for f in self._by_id.values() if f.active),
            key=lambda f: f.name.lower(),
        )

    def upsert(self, funder: FunderRow) -> FunderRow:
        # Enforce name uniqueness across different ids.
        for existing in self._by_id.values():
            if existing.id != funder.id and existing.name.lower() == funder.name.lower():
                raise ValueError(
                    f"funder name conflict: '{funder.name}' already exists under id={existing.id}"
                )
        self._by_id[funder.id] = funder
        return funder

    def delete(self, funder_id: UUID) -> None:
        self._by_id.pop(funder_id, None)


class SupabaseFunderRepository:
    """Persistence backed by Postgres ``funders`` table.

    Mirrors the in-memory contract; Postgres enforces ``UNIQUE(name)`` so
    a unique-violation surfaces as a Supabase error from ``upsert()``.
    """

    def get(self, funder_id: UUID) -> FunderRow:
        result = (
            get_supabase().table("funders").select("*").eq("id", str(funder_id)).limit(1).execute()
        )
        if not result.data:
            raise FunderNotFoundError(str(funder_id))
        return _row_to_funder(cast(dict[str, Any], result.data[0]))

    def list_active(self) -> list[FunderRow]:
        result = (
            get_supabase().table("funders").select("*").eq("active", True).order("name").execute()
        )
        return [_row_to_funder(cast(dict[str, Any], r)) for r in (result.data or [])]

    def upsert(self, funder: FunderRow) -> FunderRow:
        payload = _funder_to_payload(funder)
        result = get_supabase().table("funders").upsert(payload, on_conflict="id").execute()
        if not result.data:
            raise RuntimeError("supabase.upsert returned no row")
        return _row_to_funder(cast(dict[str, Any], result.data[0]))

    def delete(self, funder_id: UUID) -> None:
        get_supabase().table("funders").delete().eq("id", str(funder_id)).execute()


def _row_to_funder(row: dict[str, Any]) -> FunderRow:
    def _money(key: str) -> Decimal | None:
        val = row.get(key)
        return Decimal(str(val)) if val is not None else None

    def _dt(key: str) -> datetime | None:
        val = row.get(key)
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))

    return FunderRow(
        id=UUID(row["id"]),
        name=row["name"],
        active=row.get("active", True),
        min_monthly_revenue=_money("min_monthly_revenue"),
        min_avg_daily_balance=_money("min_avg_daily_balance"),
        min_credit_score=row.get("min_credit_score"),
        min_months_in_business=row.get("min_months_in_business"),
        max_positions=row.get("max_positions"),
        accepts_stacking=row.get("accepts_stacking", False),
        min_advance=_money("min_advance"),
        max_advance=_money("max_advance"),
        max_nsf_tolerance=row.get("max_nsf_tolerance"),
        requires_coj=row.get("requires_coj", False),
        aegis_compensation_disclosure_text=(row.get("aegis_compensation_disclosure_text") or ""),
        charges_merchant_advance_fees=row.get("charges_merchant_advance_fees", False),
        typical_factor_low=_money("typical_factor_low"),
        typical_factor_high=_money("typical_factor_high"),
        typical_holdback_low=_money("typical_holdback_low"),
        typical_holdback_high=_money("typical_holdback_high"),
        excluded_industries=tuple(row.get("excluded_industries") or ()),
        excluded_states=tuple(row.get("excluded_states") or ()),
        deal_types_accepted=tuple(row.get("deal_types_accepted") or ()),
        funding_velocity_days=row.get("funding_velocity_days"),
        preferred_states=tuple(row.get("preferred_states") or ()),
        guidelines_extracted_at=_dt("guidelines_extracted_at"),
        guidelines_source_pdf_hash=row.get("guidelines_source_pdf_hash"),
        contact_name=row.get("contact_name") or "",
        contact_phone=row.get("contact_phone") or "",
        contact_email=row.get("contact_email") or "",
        submission_email=row.get("submission_email") or "",
        tiers=tuple(FunderTier.model_validate(t) for t in (row.get("tiers") or [])),
        auto_decline_conditions=tuple(row.get("auto_decline_conditions") or ()),
        conditional_requirements=tuple(row.get("conditional_requirements") or ()),
        notes=row.get("notes") or "",
        notes_residual=row.get("notes_residual") or "",
        operator_notes=row.get("operator_notes") or "",
    )


def _funder_to_payload(f: FunderRow) -> dict[str, Any]:
    def _str_or_none(v: Decimal | None) -> str | None:
        return str(v) if v is not None else None

    return {
        "id": str(f.id),
        "name": f.name,
        "active": f.active,
        "min_monthly_revenue": _str_or_none(f.min_monthly_revenue),
        "min_avg_daily_balance": _str_or_none(f.min_avg_daily_balance),
        "min_credit_score": f.min_credit_score,
        "min_months_in_business": f.min_months_in_business,
        "max_positions": f.max_positions,
        "accepts_stacking": f.accepts_stacking,
        "min_advance": _str_or_none(f.min_advance),
        "max_advance": _str_or_none(f.max_advance),
        "max_nsf_tolerance": f.max_nsf_tolerance,
        "requires_coj": f.requires_coj,
        "aegis_compensation_disclosure_text": f.aegis_compensation_disclosure_text,
        "charges_merchant_advance_fees": f.charges_merchant_advance_fees,
        "typical_factor_low": _str_or_none(f.typical_factor_low),
        "typical_factor_high": _str_or_none(f.typical_factor_high),
        "typical_holdback_low": _str_or_none(f.typical_holdback_low),
        "typical_holdback_high": _str_or_none(f.typical_holdback_high),
        "excluded_industries": list(f.excluded_industries),
        "excluded_states": list(f.excluded_states),
        "deal_types_accepted": list(f.deal_types_accepted),
        "funding_velocity_days": f.funding_velocity_days,
        "preferred_states": list(f.preferred_states),
        "guidelines_extracted_at": (
            f.guidelines_extracted_at.isoformat() if f.guidelines_extracted_at else None
        ),
        "guidelines_source_pdf_hash": f.guidelines_source_pdf_hash,
        "contact_name": f.contact_name,
        "contact_phone": f.contact_phone,
        "contact_email": f.contact_email,
        "submission_email": f.submission_email,
        # JSONB tiers — model_dump(mode="json") serializes Decimal as
        # strings so JSON round-trips preserve precision.
        "tiers": [t.model_dump(mode="json") for t in f.tiers],
        "auto_decline_conditions": list(f.auto_decline_conditions),
        "conditional_requirements": list(f.conditional_requirements),
        "notes": f.notes,
        "notes_residual": f.notes_residual,
        "operator_notes": f.operator_notes,
        "updated_at": datetime.now(UTC).isoformat(),
    }


__all__ = [
    "FunderNotFoundError",
    "FunderRepository",
    "InMemoryFunderRepository",
    "SupabaseFunderRepository",
]
