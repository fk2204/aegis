"""Persistence orchestrator for the Secretary of State entity check.

Mirrors ``aegis.business_intel.refresh`` exactly:

* ``refresh_sos_for_merchant`` — always run the check, persist the
  result, write one ``merchant.sos_check.completed`` audit row.
* ``ensure_sos_check`` — lazy version called from the scorer's
  first-check hook. Respects the 30-day TTL.

Audit details carry source + status tokens only (PII rule — entity
name lives on the merchants row, not the audit row).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from aegis.audit import AuditLog
from aegis.business_intel.sos_checker import SOSChecker, SOSResult
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository

_log = get_logger(__name__)

# 30 days between refreshes. Bedrock fallback is the expensive branch;
# local DB hits are free but still respect the TTL so the audit log
# doesn't fill with duplicate rows on every dossier render.
SOS_CHECK_TTL = timedelta(days=30)


class _CheckerLike(Protocol):
    def check_entity(self, business_name: str, state: str | None) -> SOSResult: ...


def _default_checker() -> SOSChecker:
    return SOSChecker()


def refresh_sos_for_merchant(
    merchant_id: UUID,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    checker: _CheckerLike | None = None,
) -> SOSResult:
    """Always run a fresh SOS check and persist it."""
    merchant = merchants_repo.get(merchant_id)
    active_checker = checker if checker is not None else _default_checker()
    result = active_checker.check_entity(
        business_name=merchant.business_name or "",
        state=merchant.state,
    )

    updated = merchant.model_copy(
        update={
            "sos_checked_at": result.checked_at,
            "sos_status": result.status,
            "sos_entity_name": result.entity_name,
            "sos_formation_date": result.formation_date,
            "sos_is_active": result.is_active,
            "sos_data_source": result.data_source,
            "sos_state_checked": merchant.state,
        }
    )
    merchants_repo.upsert(updated)

    audit.record(
        actor="operator",
        action="merchant.sos_check.completed",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "found": result.found,
            "is_active": result.is_active,
            "data_source": result.data_source,
            "state_checked": merchant.state,
            "checked_at": result.checked_at.isoformat(),
            "error": result.error,
        },
    )
    return result


def ensure_sos_check(
    merchant: MerchantRow,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    checker: _CheckerLike | None = None,
) -> MerchantRow:
    """Lazy check: run only when no check yet OR TTL expired."""
    if merchant.sos_checked_at is not None:
        age = datetime.now(UTC) - merchant.sos_checked_at
        if age < SOS_CHECK_TTL:
            return merchant
    try:
        refresh_sos_for_merchant(
            merchant.id,
            merchants_repo=merchants_repo,
            audit=audit,
            checker=checker,
        )
    except MerchantNotFoundError:
        _log.warning(
            "sos_check.ensure_skipped_unknown_merchant merchant_id=%s",
            merchant.id,
        )
        return merchant
    return merchants_repo.get(merchant.id)


__all__ = [
    "SOS_CHECK_TTL",
    "ensure_sos_check",
    "refresh_sos_for_merchant",
]
