"""Tests for ``aegis.web_presence.refresh``.

Covers the persistence orchestrator that the dossier "Refresh" button
and the scorer's "first score" hook both call. Stubs the scanner so
no Bedrock call goes out; in-memory merchant repo + audit log to
exercise the persistence + audit-row contracts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    MerchantNotFoundError,
)
from aegis.web_presence.refresh import (
    ensure_web_presence_scan,
    refresh_web_presence_for_merchant,
)
from aegis.web_presence.scanner import WebPresenceResult


def _stub_scanner(result: WebPresenceResult) -> object:
    """Build a positional/keyword-compatible scanner stub.

    ``scan_web_presence`` is called with positional or keyword args; the
    stub accepts both and records the call count + last args.
    """
    calls: list[tuple[str, str | None, str | None]] = []

    def _call(
        business_name: str,
        city: str | None = None,
        state: str | None = None,
    ) -> WebPresenceResult:
        calls.append((business_name, city, state))
        return result

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


def _make_merchant(*, scanned_at: datetime | None = None) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Inc.",
        state="MA",
        web_presence_scanned_at=scanned_at,
    )


# ---------------------------------------------------------------------------
# refresh_web_presence_for_merchant — always runs
# ---------------------------------------------------------------------------


def test_refresh_persists_summary_flags_and_audit_row() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant()
    repo.upsert(merchant)

    scanned_at = datetime.now(UTC)
    scanner = _stub_scanner(
        WebPresenceResult(
            summary="Decent reputation online.",
            risk_flags=("bbb_unresolved_complaints",),
            scanned_at=scanned_at,
        )
    )

    result = refresh_web_presence_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        scanner=scanner,  # type: ignore[arg-type]
    )

    assert result.summary == "Decent reputation online."
    assert result.risk_flags == ("bbb_unresolved_complaints",)

    refreshed = repo.get(merchant.id)
    assert refreshed.web_presence_summary == "Decent reputation online."
    assert refreshed.web_presence_flags == ["bbb_unresolved_complaints"]
    assert refreshed.web_presence_scanned_at == scanned_at

    rows = [e for e in audit.entries if e["action"] == "merchant.web_presence.scanned"]
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["flag_count"] == 1
    assert details["summary_length"] == len("Decent reputation online.")
    assert details["bedrock_succeeded"] is True


def test_refresh_persists_empty_result_on_bedrock_failure() -> None:
    """A Bedrock failure surfaces as a WebPresenceResult with no
    scanned_at; we still upsert + audit so the operator can see the
    refresh click did SOMETHING."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant()
    repo.upsert(merchant)

    scanner = _stub_scanner(WebPresenceResult())  # all defaults — failure shape
    refresh_web_presence_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        scanner=scanner,  # type: ignore[arg-type]
    )

    refreshed = repo.get(merchant.id)
    assert refreshed.web_presence_summary is None
    assert refreshed.web_presence_flags == []
    assert refreshed.web_presence_scanned_at is None

    row = next(e for e in audit.entries if e["action"] == "merchant.web_presence.scanned")
    assert row["details"]["bedrock_succeeded"] is False


def test_refresh_unknown_merchant_raises_not_found() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    scanner = _stub_scanner(WebPresenceResult())
    with pytest.raises(MerchantNotFoundError):
        refresh_web_presence_for_merchant(
            uuid4(),
            merchants_repo=repo,
            audit=audit,
            scanner=scanner,  # type: ignore[arg-type]
        )


def test_refresh_does_not_blanket_log_body_into_audit() -> None:
    """CLAUDE.md PII rule — the audit-row ``details`` carries counts
    only, never the summary body or flag strings."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant()
    repo.upsert(merchant)

    scanner = _stub_scanner(
        WebPresenceResult(
            summary="A sensitive paragraph about the business.",
            risk_flags=("permanently_closed",),
            scanned_at=datetime.now(UTC),
        )
    )
    refresh_web_presence_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        scanner=scanner,  # type: ignore[arg-type]
    )

    row = next(e for e in audit.entries if e["action"] == "merchant.web_presence.scanned")
    details = row["details"]
    assert "sensitive" not in str(details)
    assert "permanently_closed" not in str(details)


# ---------------------------------------------------------------------------
# ensure_web_presence_scan — lazy
# ---------------------------------------------------------------------------


def test_ensure_skips_when_already_scanned() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    earlier = datetime(2026, 6, 1, tzinfo=UTC)
    merchant = _make_merchant(scanned_at=earlier)
    repo.upsert(merchant)

    scanner = _stub_scanner(WebPresenceResult(summary="x"))
    result_row = ensure_web_presence_scan(
        merchant,
        merchants_repo=repo,
        audit=audit,
        scanner=scanner,  # type: ignore[arg-type]
    )
    assert scanner.calls == []  # type: ignore[attr-defined]
    assert result_row.web_presence_scanned_at == earlier
    assert audit.entries == []


def test_ensure_runs_scan_when_scanned_at_is_none() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant(scanned_at=None)
    repo.upsert(merchant)

    scanned_at = datetime.now(UTC)
    scanner = _stub_scanner(
        WebPresenceResult(
            summary="Summary.",
            risk_flags=("recently_closed",),
            scanned_at=scanned_at,
        )
    )
    refreshed = ensure_web_presence_scan(
        merchant,
        merchants_repo=repo,
        audit=audit,
        scanner=scanner,  # type: ignore[arg-type]
    )
    assert refreshed.web_presence_summary == "Summary."
    assert refreshed.web_presence_flags == ["recently_closed"]
    assert any(e["action"] == "merchant.web_presence.scanned" for e in audit.entries)
