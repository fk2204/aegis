"""Persistence helper for the federal bankruptcy check.

Mirrors ``aegis.business_intel.refresh`` (the UCC equivalent) in shape:

* ``refresh_bankruptcy_for_merchant`` — always runs the check, always
  persists the five ``merchants.bankruptcy_*`` columns, always writes
  one ``merchant.bankruptcy_check.completed`` audit row, and writes
  one ``compliance.bankruptcy_block`` row when an active Chapter 7
  is detected.
* ``ensure_bankruptcy_check`` — TTL-driven (30 days). Returns the
  (possibly refreshed) merchant row so callers can reuse it in the
  same request without a second DB read.

Audit details carry counts + chapter ONLY (no PII). The case list
lives on the merchant row; the audit row is the durable check-event
record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from aegis.audit import AuditLog
from aegis.business_intel.bankruptcy_checker import (
    BankruptcyResult,
    check_bankruptcy,
)
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository

_log = get_logger(__name__)

# How long an existing check is treated as fresh. CourtListener data
# updates daily; 30 days matches Commera's underwriting refresh
# cadence (a deal that hasn't been priced in 30 days re-runs the full
# stack anyway).
_BANKRUPTCY_CHECK_TTL_DAYS: int = 30


class _CheckerLike(Protocol):
    async def __call__(
        self,
        business_name: str,
        owner_name: str | None = ...,
    ) -> BankruptcyResult: ...


async def refresh_bankruptcy_for_merchant(
    merchant_id: UUID,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    checker: _CheckerLike = check_bankruptcy,
) -> BankruptcyResult:
    """Always run a fresh bankruptcy check and persist it.

    Network failure → empty result persisted + audit row with
    ``error`` carried in details so the operator can distinguish
    "checked, no bankruptcy" from "couldn't check (CourtListener
    down)". A Chapter 7 active result additionally writes a
    ``compliance.bankruptcy_block`` audit row so the dossier gate's
    activation is auditable.
    """
    merchant = merchants_repo.get(merchant_id)
    result = await checker(
        business_name=merchant.business_name or "",
        owner_name=merchant.owner_name,
    )
    updated = merchant.model_copy(
        update={
            "bankruptcy_checked_at": result.checked_at,
            "bankruptcy_active": result.active,
            "bankruptcy_recent": result.recent,
            "bankruptcy_chapter": result.chapter,
            "bankruptcy_cases": list(result.cases),
        }
    )
    merchants_repo.upsert(updated)

    audit.record(
        actor="system",
        action="compliance.bankruptcy_screened",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "active": result.active,
            "recent": result.recent,
            "chapter": result.chapter,
            "case_count": len(result.cases),
            "checked_at": result.checked_at.isoformat() if result.checked_at else None,
            "error": result.error,
        },
    )

    # Chapter 7 active → hard gate at the dossier. Audit the block
    # event separately so the underwriting timeline shows the
    # specific decision point (the screening row above fires on
    # every check; the block row fires only when the gate trips).
    if result.active and result.chapter == "7":
        audit.record(
            actor="system",
            action="compliance.bankruptcy_block",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "chapter": result.chapter,
                "case_count": len(result.cases),
            },
        )

    return result


async def ensure_bankruptcy_check(
    merchant: MerchantRow,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    checker: _CheckerLike = check_bankruptcy,
) -> MerchantRow:
    """TTL-bounded version: run a check when none was recorded in the
    last 30 days.

    Returns the (possibly refreshed) merchant row so the caller can
    pass the bankruptcy fields into the dossier gate in the same
    request. A CourtListener failure persists the empty result + audit
    row so the next scoring pass doesn't re-fire the failed check
    inside the TTL window.
    """
    if merchant.bankruptcy_checked_at is not None:
        age = datetime.now(UTC) - merchant.bankruptcy_checked_at
        if age < timedelta(days=_BANKRUPTCY_CHECK_TTL_DAYS):
            return merchant
    try:
        await refresh_bankruptcy_for_merchant(
            merchant.id,
            merchants_repo=merchants_repo,
            audit=audit,
            checker=checker,
        )
    except MerchantNotFoundError:
        _log.warning(
            "bankruptcy_check.ensure_skipped_unknown_merchant merchant_id=%s",
            merchant.id,
        )
        return merchant
    return merchants_repo.get(merchant.id)


__all__ = [
    "ensure_bankruptcy_check",
    "refresh_bankruptcy_for_merchant",
]
