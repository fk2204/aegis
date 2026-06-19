"""Persistence helper for the UCC + previous-default check.

Mirrors ``aegis.web_presence.refresh`` — load merchant → run
``check_ucc_and_defaults`` → persist three columns → write one
``merchant.ucc_check.completed`` audit row → return the result.

Two callers:

* Dossier "Refresh" button (POST /ui/merchants/{id}/refresh-ucc) —
  always runs the check, always persists.
* Scorer's "first check" hook (``ensure_ucc_check``) — runs only
  when ``merchant.ucc_checked_at is None``.

Audit details carry counts only (PII rule). The filing / default
strings live on the merchants row; the audit row is the durable
check-event record.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from aegis.audit import AuditLog
from aegis.business_intel.ucc_checker import UCCResult, check_ucc_and_defaults
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository

_log = get_logger(__name__)


class _CheckerLike(Protocol):
    def __call__(
        self,
        business_name: str,
        state: str | None = ...,
        owner_name: str | None = ...,
    ) -> UCCResult: ...


def refresh_ucc_for_merchant(
    merchant_id: UUID,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    checker: _CheckerLike = check_ucc_and_defaults,
) -> UCCResult:
    """Always run a fresh check and persist it.

    Bedrock failure → empty result persisted + audit row with
    ``bedrock_succeeded=False`` so the operator can distinguish
    "checked and found nothing" from "check failed".
    """
    merchant = merchants_repo.get(merchant_id)
    result = checker(
        business_name=merchant.business_name or "",
        state=merchant.state,
        owner_name=merchant.owner_name,
    )
    updated = merchant.model_copy(
        update={
            "ucc_filings": list(result.ucc_filings),
            "ucc_default_indicators": list(result.default_indicators),
            "ucc_checked_at": result.checked_at,
        }
    )
    merchants_repo.upsert(updated)

    audit.record(
        actor="operator",
        action="merchant.ucc_check.completed",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "ucc_filing_count": len(result.ucc_filings),
            "default_indicator_count": len(result.default_indicators),
            "checked_at": result.checked_at.isoformat() if result.checked_at else None,
            "bedrock_succeeded": result.checked_at is not None,
        },
    )
    return result


def ensure_ucc_check(
    merchant: MerchantRow,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    checker: _CheckerLike = check_ucc_and_defaults,
) -> MerchantRow:
    """Lazy version: run a check only when ``ucc_checked_at`` is None.

    Returns the (possibly refreshed) merchant row so the caller can
    pass the updated lists into ``match_funder`` in the same request.
    A Bedrock failure persists the empty result + audit row so the
    next score doesn't re-fire the failed check.
    """
    if merchant.ucc_checked_at is not None:
        return merchant
    try:
        refresh_ucc_for_merchant(
            merchant.id,
            merchants_repo=merchants_repo,
            audit=audit,
            checker=checker,
        )
    except MerchantNotFoundError:
        _log.warning(
            "ucc_check.ensure_skipped_unknown_merchant merchant_id=%s",
            merchant.id,
        )
        return merchant
    return merchants_repo.get(merchant.id)


__all__ = [
    "ensure_ucc_check",
    "refresh_ucc_for_merchant",
]
