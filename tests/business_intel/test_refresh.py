"""Tests for ``aegis.business_intel.refresh`` — persistence orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.business_intel.refresh import (
    ensure_ucc_check,
    refresh_ucc_for_merchant,
)
from aegis.business_intel.ucc_checker import UCCResult
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    MerchantNotFoundError,
)


def _stub_checker(result: UCCResult) -> object:
    calls: list[tuple[str, str | None, str | None]] = []

    def _call(
        business_name: str,
        state: str | None = None,
        owner_name: str | None = None,
    ) -> UCCResult:
        calls.append((business_name, state, owner_name))
        return result

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


def _make_merchant(*, checked_at: datetime | None = None) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Inc.",
        state="MA",
        ucc_checked_at=checked_at,
    )


# ---------------------------------------------------------------------------
# refresh_ucc_for_merchant
# ---------------------------------------------------------------------------


def test_refresh_persists_lists_and_writes_audit() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant()
    repo.upsert(merchant)

    checked_at = datetime.now(UTC)
    checker = _stub_checker(
        UCCResult(
            ucc_filings=("OnDeck",),
            default_indicators=("lawsuit_2024",),
            source_summary="One filing + one judgment.",
            checked_at=checked_at,
        )
    )

    result = refresh_ucc_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        checker=checker,  # type: ignore[arg-type]
    )
    assert result.ucc_filings == ("OnDeck",)
    assert result.default_indicators == ("lawsuit_2024",)

    refreshed = repo.get(merchant.id)
    assert refreshed.ucc_filings == ["OnDeck"]
    assert refreshed.ucc_default_indicators == ["lawsuit_2024"]
    assert refreshed.ucc_checked_at == checked_at

    row = next(e for e in audit.entries if e["action"] == "merchant.ucc_check.completed")
    assert row["details"]["ucc_filing_count"] == 1
    assert row["details"]["default_indicator_count"] == 1
    assert row["details"]["bedrock_succeeded"] is True


def test_refresh_persists_empty_on_bedrock_failure() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant()
    repo.upsert(merchant)

    refresh_ucc_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        checker=_stub_checker(UCCResult()),  # type: ignore[arg-type]
    )
    refreshed = repo.get(merchant.id)
    assert refreshed.ucc_filings == []
    assert refreshed.ucc_default_indicators == []
    assert refreshed.ucc_checked_at is None

    row = next(e for e in audit.entries if e["action"] == "merchant.ucc_check.completed")
    assert row["details"]["bedrock_succeeded"] is False


def test_refresh_unknown_merchant_raises() -> None:
    with pytest.raises(MerchantNotFoundError):
        refresh_ucc_for_merchant(
            uuid4(),
            merchants_repo=InMemoryMerchantRepository(),
            audit=InMemoryAuditLog(),
            checker=_stub_checker(UCCResult()),  # type: ignore[arg-type]
        )


def test_refresh_audit_does_not_leak_filing_strings() -> None:
    """CLAUDE.md PII rule — details carries counts only."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant()
    repo.upsert(merchant)

    refresh_ucc_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        checker=_stub_checker(  # type: ignore[arg-type]
            UCCResult(
                ucc_filings=("OnDeck Capital Confidential",),
                default_indicators=("named_lawsuit_doe_v_acme",),
                checked_at=datetime.now(UTC),
            )
        ),
    )
    row = next(e for e in audit.entries if e["action"] == "merchant.ucc_check.completed")
    details_str = str(row["details"])
    assert "OnDeck Capital" not in details_str
    assert "named_lawsuit" not in details_str


# ---------------------------------------------------------------------------
# ensure_ucc_check
# ---------------------------------------------------------------------------


def test_ensure_skips_when_already_checked() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    earlier = datetime(2026, 6, 1, tzinfo=UTC)
    merchant = _make_merchant(checked_at=earlier)
    repo.upsert(merchant)

    checker = _stub_checker(UCCResult())
    result_row = ensure_ucc_check(
        merchant,
        merchants_repo=repo,
        audit=audit,
        checker=checker,  # type: ignore[arg-type]
    )
    assert checker.calls == []  # type: ignore[attr-defined]
    assert result_row.ucc_checked_at == earlier
    assert audit.entries == []


def test_ensure_runs_when_checked_at_is_none() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant(checked_at=None)
    repo.upsert(merchant)

    checked_at = datetime.now(UTC)
    checker = _stub_checker(
        UCCResult(
            ucc_filings=("X",),
            default_indicators=(),
            checked_at=checked_at,
        )
    )
    refreshed = ensure_ucc_check(
        merchant,
        merchants_repo=repo,
        audit=audit,
        checker=checker,  # type: ignore[arg-type]
    )
    assert refreshed.ucc_filings == ["X"]
    assert any(e["action"] == "merchant.ucc_check.completed" for e in audit.entries)
