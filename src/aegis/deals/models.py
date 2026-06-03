"""DealRow projection + deal_id format/parse helpers.

A deal is the join ``(merchants x documents [x analyses])``. AEGIS does
not store a ``deals`` table — F1 of the Phase 7 audit explicitly forbids
one. ``DealRow`` is the read-only Pydantic projection callers receive
from ``DealRepository``.

deal_id format decision
-----------------------
Two options were considered:

1. ``deal_id = f"{merchant_id}:{document_id}"`` — composite string.
2. ``deal_id = uuid5(NAMESPACE, f"{merchant_id}:{document_id}")`` — a
   deterministic v5 UUID.

The composite string was chosen because the spec requires
``parse_deal_id(deal_id) -> tuple[UUID, UUID]`` as a pure function. UUID
v5 is a one-way hash — recovering the inputs from the v5 would require
either storing a (deal_id → merchant_id, document_id) lookup table (the
exact thing F1 forbids) or carrying the inputs alongside the v5 (which
defeats having a single id at all). The composite is reversible, stable,
and human-readable in URLs / log lines / CRM fields.

The trade-off: ``deal_id`` is therefore typed ``str``, not ``UUID``. The
``audit_log.subject_id`` column is typed ``UUID``, so audit rows for
deal-scoped actions write the ``document_id`` UUID into ``subject_id``
and include the composite ``deal_id`` in the ``details`` JSONB. This
matches the existing pattern: ``disclosure_transmission_log.deal_id``
references ``documents.id`` directly today (migration 004 line 23-24).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Same shape as merchants.models / parser.models / etc.
ParseStatus = Literal["pending", "proceed", "review", "manual_review", "error"]
ScoreRecommendation = Literal["approve", "decline", "refer"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


def format_deal_id(merchant_id: UUID, document_id: UUID) -> str:
    """Format a deal_id from its component ids. Inverse of ``parse_deal_id``.

    The format is ``"{merchant_id}:{document_id}"`` — both UUIDs in their
    canonical 36-char hyphenated form. Total length 73.
    """
    return f"{merchant_id}:{document_id}"


def parse_deal_id(deal_id: str) -> tuple[UUID, UUID]:
    """Parse a deal_id back to its component ``(merchant_id, document_id)``.

    Raises ``ValueError`` on a malformed deal_id so callers (the API,
    the dashboard route) surface a 400 rather than a 500 — the same way
    ``UUID(str)`` already does on malformed UUIDs.
    """
    parts = deal_id.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"deal_id must be 'merchant_id:document_id' (got {len(parts)} parts)"
        )
    merchant_part, document_part = parts
    try:
        merchant_id = UUID(merchant_part)
        document_id = UUID(document_part)
    except ValueError as exc:
        raise ValueError(f"deal_id contains a malformed UUID: {exc}") from exc
    return merchant_id, document_id


class DealRow(_StrictModel):
    """A merchant x document projection. Read-only — never persisted.

    Built from joins across ``merchants``, ``documents``, and ``analyses``.
    Convenience fields (``business_name``, ``state``, ``fraud_score``,
    ``score_recommendation``, ``parse_status``) live here so the dashboard
    list view does not re-query per row.

    ``score_recommendation`` is sourced from a cached score (the most
    recent ``audit_log`` ``deal.score`` action) when available, otherwise
    ``None`` — the dashboard then shows "not scored yet". This avoids
    re-running scoring during a list render.

    ``created_at`` mirrors ``documents.uploaded_at`` because a deal is
    materially created by the document upload — the merchant may pre-date
    it but the deal is the application of underwriting to a specific
    statement.
    """

    deal_id: str = Field(min_length=73, max_length=73)
    merchant_id: UUID
    document_id: UUID
    created_at: datetime

    # Convenience joins (read-only). ``state`` is nullable to mirror
    # ``MerchantRow.state`` post-migration-034 — an auto-finalized
    # merchant can sit without state until operator edits.
    business_name: str = Field(min_length=1)
    state: str | None = Field(default=None, min_length=2, max_length=2)
    parse_status: ParseStatus

    # Optional analytics surface. None when the document has not been
    # parsed yet (parse_status in {pending, error}) or when scoring has
    # not run.
    fraud_score: int | None = Field(default=None, ge=0, le=100)
    score_recommendation: ScoreRecommendation | None = None

    @field_validator("deal_id")
    @classmethod
    def _validate_deal_id_format(cls, v: str) -> str:
        # Round-trip parse to enforce ``merchant_id:document_id`` shape.
        parse_deal_id(v)
        return v

    @field_validator("state")
    @classmethod
    def _normalize_state(cls, v: str) -> str:
        return v.upper()


__all__ = [
    "DealRow",
    "ParseStatus",
    "ScoreRecommendation",
    "format_deal_id",
    "parse_deal_id",
]
