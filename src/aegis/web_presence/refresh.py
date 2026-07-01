"""Persistence helper for the web-presence scan.

Orchestrates: load merchant -> run ``scan_web_presence`` -> update
``web_presence_{summary,flags,scanned_at}`` on the row -> write one
``merchant.web_presence.scanned`` audit row -> return the result.

Two callers today:

* The dossier "Refresh" button (POST /ui/merchants/{id}/refresh-web-presence)
  — always runs the scan, always persists.
* The scorer's "first score" hook (``ensure_web_presence_scan``) —
  runs only when ``merchant.web_presence_scanned_at is None``.

Both use the same orchestrator so the audit-row shape stays consistent.

Audit-row ``details`` carries scan outcomes ONLY:

* ``summary_length`` — char count (never the body, per CLAUDE.md PII rule)
* ``flag_count``     — int
* ``scanned_at``     — ISO timestamp or None on a Bedrock failure

The summary body and flag tags live on the merchants row; the audit row
is the durable scan-event record.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from aegis.audit import AuditLog
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository
from aegis.web_presence.scanner import WebPresenceResult, scan_web_presence

_log = get_logger(__name__)


class _ScannerLike(Protocol):
    """Function shape so tests can inject a stub scanner without
    touching ``aegis.llm`` or the network."""

    def __call__(
        self,
        business_name: str,
        city: str | None = ...,
        state: str | None = ...,
    ) -> WebPresenceResult: ...


def refresh_web_presence_for_merchant(
    merchant_id: UUID,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    scanner: _ScannerLike = scan_web_presence,
) -> WebPresenceResult:
    """Always run a fresh scan and persist it.

    Raises ``MerchantNotFoundError`` when the id is unknown — the caller
    (a UI route) translates that to 404. A Bedrock failure surfaces as a
    ``WebPresenceResult`` with ``scanned_at=None``; we persist the empty
    result and audit that the attempt happened so the operator sees the
    refresh click did SOMETHING (even if Bedrock was down).
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    merchant = merchants_repo.get(merchant_id)
    result = scanner(
        business_name=merchant.business_name or "",
        city=merchant.state,  # state-only is enough; city not on MerchantRow today
        state=merchant.state,
    )
    # 2026-07-01 Phase 2B — always land a timestamp so the
    # background-check contract's "we attempted this" signal persists
    # even when Bedrock failed. Distinction between "scanned, empty"
    # vs "scan failed" is preserved via ``bedrock_succeeded`` in the
    # audit-detail below.
    _timestamp = result.scanned_at or _dt.now(_UTC)
    updated = merchant.model_copy(
        update={
            "web_presence_summary": result.summary or None,
            "web_presence_flags": list(result.risk_flags),
            "web_presence_scanned_at": _timestamp,
        }
    )
    merchants_repo.upsert(updated)

    audit.record(
        actor="operator",
        action="merchant.web_presence.scanned",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "summary_length": len(result.summary),
            "flag_count": len(result.risk_flags),
            "scanned_at": result.scanned_at.isoformat() if result.scanned_at else None,
            "bedrock_succeeded": result.scanned_at is not None,
        },
    )
    return result


def ensure_web_presence_scan(
    merchant: MerchantRow,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    scanner: _ScannerLike = scan_web_presence,
) -> MerchantRow:
    """Lazy version: run a scan only when ``web_presence_scanned_at`` is None.

    Returns the (possibly refreshed) merchant row so the caller can pass
    the updated ``web_presence_flags`` into ``match_funder`` in the same
    request. When a scan has already landed (``scanned_at`` populated)
    we return the input unchanged — refresh has to be explicit via the
    dossier button.

    A Bedrock failure persists the empty result + audit row exactly like
    the explicit refresh path. Otherwise EVERY future score would re-fire
    the failed scan, which is exactly what the "no retry" decision in
    ``scanner.scan_web_presence`` is trying to avoid.
    """
    if merchant.web_presence_scanned_at is not None:
        return merchant
    try:
        refresh_web_presence_for_merchant(
            merchant.id,
            merchants_repo=merchants_repo,
            audit=audit,
            scanner=scanner,
        )
    except MerchantNotFoundError:
        # Race with a soft-delete — return the input unchanged.
        _log.warning(
            "web_presence.ensure_skipped_unknown_merchant merchant_id=%s",
            merchant.id,
        )
        return merchant
    # Re-fetch so the caller's row carries the persisted flags.
    return merchants_repo.get(merchant.id)


__all__ = [
    "ensure_web_presence_scan",
    "refresh_web_presence_for_merchant",
]
