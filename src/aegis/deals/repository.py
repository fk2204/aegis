"""Deal repository: a read-only projection over merchants x documents x analyses.

No persistence layer of its own — the in-memory implementation composes
the merchant + document repositories, and the Supabase implementation
issues nested-select joins through supabase-py.

Why a Protocol + two impls (same as funders/merchants/storage): tests
run offline against the in-memory backend; production swaps to the
Supabase backend through ``api.deps`` with no caller change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from aegis.db import get_supabase
from aegis.deals.models import (
    DealRow,
    ParseStatus,
    ScoreRecommendation,
    format_deal_id,
    parse_deal_id,
)
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository
from aegis.storage import DocumentNotFoundError, DocumentRepository


class DealNotFoundError(KeyError):
    """Raised when a deal_id has no matching (merchant, document) pair."""


class DealRepository(Protocol):
    """Read-only contract for deal projections."""

    def list_deals(
        self,
        *,
        merchant_id: UUID | None = None,
        state: str | None = None,
        parse_status: ParseStatus | None = None,
        limit: int = 100,
    ) -> list[DealRow]:
        """Return deals most-recent first, optionally filtered."""

    def get_deal(self, deal_id: str) -> DealRow | None:
        """Return one deal, or ``None`` if the components don't resolve."""

    def parse_deal_id(self, deal_id: str) -> tuple[UUID, UUID]:
        """Parse a deal_id to its component ids. Convenience for callers."""


# In-memory implementation ----------------------------------------------------


class InMemoryDealRepository:
    """Composes the in-memory merchant + document repos. Test-friendly.

    Does not maintain its own state — every call reads through to the
    underlying repos, so a document added after this repo was constructed
    is visible immediately.
    """

    def __init__(
        self,
        *,
        merchants: MerchantRepository,
        documents: DocumentRepository,
    ) -> None:
        self._merchants = merchants
        self._documents = documents

    def list_deals(
        self,
        *,
        merchant_id: UUID | None = None,
        state: str | None = None,
        parse_status: ParseStatus | None = None,
        limit: int = 100,
    ) -> list[DealRow]:
        docs = self._documents.list_documents(
            parse_status=parse_status,
            merchant_id=merchant_id,
            limit=limit,
        )
        rows: list[DealRow] = []
        for doc in docs:
            # A deal requires a merchant. Documents in the "intake but
            # not yet matched to a merchant" state (merchant_id is None)
            # are not deals — they live on the review queue.
            if doc.merchant_id is None:
                continue
            try:
                merchant = self._merchants.get(doc.merchant_id)
            except MerchantNotFoundError:
                # Merchant was deleted; document survived because the FK
                # is ON DELETE SET NULL. Skip — there's no deal without
                # a merchant.
                continue
            if state is not None and (
                merchant.state is None
                or merchant.state.upper() != state.upper()
            ):
                # A state-less merchant never matches a state filter
                # (auto-finalized merchants without state, post-034).
                continue

            analysis = self._documents.get_analysis(doc.id)
            rows.append(
                DealRow(
                    deal_id=format_deal_id(merchant.id, doc.id),
                    merchant_id=merchant.id,
                    document_id=doc.id,
                    created_at=doc.uploaded_at,
                    business_name=merchant.business_name,
                    state=merchant.state.upper() if merchant.state else None,
                    parse_status=doc.parse_status,
                    fraud_score=doc.fraud_score,
                    # ``score_recommendation`` lives in audit_log / a cached
                    # ScoreResult, neither of which the in-memory repos
                    # surface. Left ``None`` here; production reads it via
                    # the Supabase impl below from a future score cache.
                    score_recommendation=_recommendation_from_analysis(analysis),
                )
            )
        return rows

    def get_deal(self, deal_id: str) -> DealRow | None:
        try:
            merchant_id, document_id = parse_deal_id(deal_id)
        except ValueError:
            return None
        try:
            doc = self._documents.get_document(document_id)
        except DocumentNotFoundError:
            return None
        if doc.merchant_id != merchant_id:
            return None
        try:
            merchant = self._merchants.get(merchant_id)
        except MerchantNotFoundError:
            return None
        analysis = self._documents.get_analysis(doc.id)
        return DealRow(
            deal_id=format_deal_id(merchant.id, doc.id),
            merchant_id=merchant.id,
            document_id=doc.id,
            created_at=doc.uploaded_at,
            business_name=merchant.business_name,
            state=merchant.state.upper() if merchant.state else None,
            parse_status=doc.parse_status,
            fraud_score=doc.fraud_score,
            score_recommendation=_recommendation_from_analysis(analysis),
        )

    def parse_deal_id(self, deal_id: str) -> tuple[UUID, UUID]:
        return parse_deal_id(deal_id)


# Supabase implementation -----------------------------------------------------


class SupabaseDealRepository:
    """Persistence-backed implementation using PostgREST nested selects.

    Issues a single ``documents`` query joined to ``merchants`` and
    ``analyses`` via the foreign-key relations Postgres already knows
    about. The SQL view created in migration 012 covers the same shape
    if a caller prefers ``.from_("deals")``; this implementation goes
    through ``documents`` to share filter semantics with
    ``SupabaseDocumentRepository.list_documents``.
    """

    def list_deals(
        self,
        *,
        merchant_id: UUID | None = None,
        state: str | None = None,
        parse_status: ParseStatus | None = None,
        limit: int = 100,
    ) -> list[DealRow]:
        # Documents drive the result; merchants + analyses are nested.
        # ``!inner`` on merchants means rows without a merchant are
        # excluded — matching the in-memory impl.
        query = (
            get_supabase()
            .table("documents")
            .select(
                "id,merchant_id,parse_status,fraud_score,uploaded_at,"
                "merchants!inner(business_name,state),"
                "analyses(id)"
            )
            .order("uploaded_at", desc=True)
            .limit(limit)
        )
        if merchant_id is not None:
            query = query.eq("merchant_id", str(merchant_id))
        if parse_status is not None:
            query = query.eq("parse_status", parse_status)
        if state is not None:
            query = query.eq("merchants.state", state.upper())

        result = query.execute()
        rows = cast(list[dict[str, Any]], result.data or [])
        return [
            _row_to_deal(r) for r in rows if r.get("merchant_id") is not None
        ]

    def get_deal(self, deal_id: str) -> DealRow | None:
        try:
            merchant_id, document_id = parse_deal_id(deal_id)
        except ValueError:
            return None

        result = (
            get_supabase()
            .table("documents")
            .select(
                "id,merchant_id,parse_status,fraud_score,uploaded_at,"
                "merchants!inner(business_name,state),"
                "analyses(id)"
            )
            .eq("id", str(document_id))
            .eq("merchant_id", str(merchant_id))
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        return _row_to_deal(cast(dict[str, Any], result.data[0]))

    def parse_deal_id(self, deal_id: str) -> tuple[UUID, UUID]:
        return parse_deal_id(deal_id)


# Helpers ---------------------------------------------------------------------


def _recommendation_from_analysis(analysis: object) -> ScoreRecommendation | None:
    """Score recommendation lives on the score cache (not yet built).

    Returning ``None`` here is correct today: scoring re-runs on demand
    via ``POST /deals/score``; the dashboard list view shows "—" for
    rows without a cached recommendation. A future score-cache layer
    will populate this without changing the DealRow contract.
    """
    _ = analysis  # placeholder until score cache lands
    return None


def _row_to_deal(row: dict[str, Any]) -> DealRow:
    merchant_block = cast(dict[str, Any], row.get("merchants") or {})
    document_id = UUID(row["id"])
    merchant_id = UUID(row["merchant_id"])

    # ``DealRow.state`` is ``str | None``. Migration 034 made
    # ``merchants.state`` nullable so auto-finalized merchants without an
    # extracted address can land in a valid finalized state — the
    # in-memory variant mirrors the same None pass-through (see line 117
    # of this file). Without this guard ``str(None).upper()`` produces
    # the literal ``"NONE"`` which fails Pydantic's 2-char state
    # validator and 500s every Deals-list / Deal-detail render that
    # touches a state-less merchant.
    raw_state = merchant_block.get("state")
    state = str(raw_state).upper() if raw_state else None

    return DealRow(
        deal_id=format_deal_id(merchant_id, document_id),
        merchant_id=merchant_id,
        document_id=document_id,
        created_at=_parse_dt(row["uploaded_at"]),
        business_name=merchant_block["business_name"],
        state=state,
        parse_status=row["parse_status"],
        fraud_score=row.get("fraud_score"),
        score_recommendation=None,
    )


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


__all__ = [
    "DealNotFoundError",
    "DealRepository",
    "InMemoryDealRepository",
    "SupabaseDealRepository",
]
