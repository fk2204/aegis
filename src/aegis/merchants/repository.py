"""Merchant persistence.

Mirrors the funder pattern: ``MerchantRepository`` Protocol +
``InMemoryMerchantRepository`` for tests + ``SupabaseMerchantRepository``
for production. Uniqueness invariants enforced at this layer:

  * ``zoho_deal_id`` (when set) is unique — the Zoho-sync idempotency
    key. The DB also enforces this via UNIQUE; this layer raises early.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from aegis.db import get_supabase
from aegis.merchants.models import MerchantRow


class MerchantNotFoundError(KeyError):
    """Raised when a merchant id (or zoho_deal_id) has no row."""


class MerchantConflictError(ValueError):
    """Raised when a uniqueness constraint would be violated."""


class MerchantRepository(Protocol):
    def get(self, merchant_id: UUID) -> MerchantRow: ...
    def find_by_zoho_deal_id(self, zoho_deal_id: str) -> MerchantRow | None: ...
    def list_all(self, *, state: str | None = None) -> list[MerchantRow]: ...
    def upsert(self, merchant: MerchantRow) -> MerchantRow: ...
    def delete(self, merchant_id: UUID) -> None: ...


class InMemoryMerchantRepository:
    """Dict-backed merchant store. Tests + offline."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, MerchantRow] = {}

    def get(self, merchant_id: UUID) -> MerchantRow:
        try:
            return self._by_id[merchant_id]
        except KeyError as exc:
            raise MerchantNotFoundError(str(merchant_id)) from exc

    def find_by_zoho_deal_id(self, zoho_deal_id: str) -> MerchantRow | None:
        for m in self._by_id.values():
            if m.zoho_deal_id == zoho_deal_id:
                return m
        return None

    def list_all(self, *, state: str | None = None) -> list[MerchantRow]:
        rows = list(self._by_id.values())
        if state is not None:
            s = state.upper()
            rows = [m for m in rows if m.state == s]
        return sorted(rows, key=lambda m: m.business_name.lower())

    def upsert(self, merchant: MerchantRow) -> MerchantRow:
        # Enforce uniqueness on zoho_deal_id across other ids.
        if merchant.zoho_deal_id is not None:
            for existing in self._by_id.values():
                if (
                    existing.id != merchant.id
                    and existing.zoho_deal_id == merchant.zoho_deal_id
                ):
                    raise MerchantConflictError(
                        f"zoho_deal_id {merchant.zoho_deal_id!r} already on merchant "
                        f"{existing.id}"
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

    def find_by_zoho_deal_id(self, zoho_deal_id: str) -> MerchantRow | None:
        result = (
            get_supabase()
            .table("merchants")
            .select("*")
            .eq("zoho_deal_id", zoho_deal_id)
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


def _row_to_merchant(row: dict[str, Any]) -> MerchantRow:
    return MerchantRow(
        id=UUID(row["id"]),
        business_name=row["business_name"],
        dba=row.get("dba"),
        owner_name=row["owner_name"],
        state=row["state"],
        industry_naics=row.get("industry_naics"),
        industry_risk_tier=row.get("industry_risk_tier"),
        time_in_business_months=row.get("time_in_business_months"),
        credit_score=row.get("credit_score"),
        email=row.get("email"),
        phone=row.get("phone"),
        zoho_deal_id=row.get("zoho_deal_id"),
        created_at=_parse_dt(row.get("created_at")),
        updated_at=_parse_dt(row.get("updated_at")),
    )


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
        "business_name": m.business_name,
        "dba": m.dba,
        "owner_name": m.owner_name,
        "state": m.state.upper(),
        "industry_naics": m.industry_naics,
        "industry_risk_tier": m.industry_risk_tier,
        "time_in_business_months": m.time_in_business_months,
        "credit_score": m.credit_score,
        "email": m.email,
        "phone": m.phone,
        "zoho_deal_id": m.zoho_deal_id,
    }
    return payload


__all__ = [
    "InMemoryMerchantRepository",
    "MerchantConflictError",
    "MerchantNotFoundError",
    "MerchantRepository",
    "SupabaseMerchantRepository",
]
