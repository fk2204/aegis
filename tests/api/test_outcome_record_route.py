"""Tests for ``POST /ui/merchants/{merchant_id}/decisions/{decision_id}/outcome``.

The route writes one row to ``deal_outcomes`` (migration 074) and writes
a paired ``deal.outcome_recorded`` audit row. Auth is the existing
SSO-or-bearer fallback; validation rejects invalid enums and missing
charge-off amounts on charged_off / defaulted outcomes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import aegis.db as _db
from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_repository,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository

# ---------------------------------------------------------------------------
# Fake Supabase
# ---------------------------------------------------------------------------


class _FakeExecuteResult:
    __slots__ = ("data",)

    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _FakeTable:
    """Mirror the supabase-py call chain we use: ``table().insert().execute()``.

    Stores inserted rows so the test can assert what landed in
    ``deal_outcomes``.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def insert(self, row: dict[str, Any]) -> _FakeTable:
        self.rows.append(row)
        return self

    def execute(self) -> _FakeExecuteResult:
        return _FakeExecuteResult(self.rows)


class _FakeSupabase:
    def __init__(self) -> None:
        self.by_table: dict[str, _FakeTable] = {}

    def table(self, name: str) -> _FakeTable:
        return self.by_table.setdefault(name, _FakeTable())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchant_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def merchant(merchant_repo: InMemoryMerchantRepository) -> MerchantRow:
    row = MerchantRow(business_name="Acme Co LLC", state="CA")
    merchant_repo._by_id[row.id] = row
    return row


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def decision_id() -> UUID:
    return uuid4()


@pytest.fixture
def fake_supabase(monkeypatch: pytest.MonkeyPatch) -> _FakeSupabase:
    fake = _FakeSupabase()
    monkeypatch.setattr(_db, "get_supabase", lambda: fake)
    return fake


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    merchant_repo: InMemoryMerchantRepository,
    funder_repo: InMemoryFunderRepository,
    fake_supabase: _FakeSupabase,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_inserts_row_and_writes_audit(
    client: TestClient,
    merchant: MerchantRow,
    decision_id: UUID,
    audit: InMemoryAuditLog,
    fake_supabase: _FakeSupabase,
) -> None:
    resp = client.post(
        f"/ui/merchants/{merchant.id}/decisions/{decision_id}/outcome",
        data={
            "funder_decision": "approved",
            "outcome": "paying",
            "funded_amount": "50000",
            "factor_rate": "1.35",
            "term_days": "180",
            "first_payment_date": "2026-07-01",
            "notes": "first deal funded",
        },
    )
    assert resp.status_code == 200, resp.text
    assert 'data-test-id="deal-outcome-recorded"' in resp.text

    # Confirm the row landed in fake supabase.
    rows = fake_supabase.by_table["deal_outcomes"].rows
    assert len(rows) == 1
    row = rows[0]
    assert row["merchant_id"] == str(merchant.id)
    assert row["decision_id"] == str(decision_id)
    assert row["outcome"] == "paying"
    assert row["funder_decision"] == "approved"
    assert row["funded_amount"] == "50000"
    assert row["factor_rate"] == "1.35"
    assert row["term_days"] == 180

    # Audit row.
    matching = [e for e in audit.entries if e["action"] == "deal.outcome_recorded"]
    assert len(matching) == 1
    assert matching[0]["subject_id"] == str(merchant.id)
    assert matching[0]["details"]["outcome"] == "paying"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_outcome_returns_400(
    client: TestClient,
    merchant: MerchantRow,
    decision_id: UUID,
) -> None:
    resp = client.post(
        f"/ui/merchants/{merchant.id}/decisions/{decision_id}/outcome",
        data={
            "funder_decision": "approved",
            "outcome": "no_such_outcome",
        },
    )
    assert resp.status_code == 400


def test_invalid_funder_decision_returns_400(
    client: TestClient,
    merchant: MerchantRow,
    decision_id: UUID,
) -> None:
    resp = client.post(
        f"/ui/merchants/{merchant.id}/decisions/{decision_id}/outcome",
        data={
            "funder_decision": "maybe",
            "outcome": "paying",
        },
    )
    assert resp.status_code == 400


def test_charged_off_requires_charge_off_amount(
    client: TestClient,
    merchant: MerchantRow,
    decision_id: UUID,
) -> None:
    """outcome=charged_off without a charge_off_amount must 400."""
    resp = client.post(
        f"/ui/merchants/{merchant.id}/decisions/{decision_id}/outcome",
        data={
            "funder_decision": "approved",
            "outcome": "charged_off",
            "funded_amount": "50000",
        },
    )
    assert resp.status_code == 400
    assert "charge_off_amount" in resp.text


def test_defaulted_requires_charge_off_amount(
    client: TestClient,
    merchant: MerchantRow,
    decision_id: UUID,
) -> None:
    """defaulted is the other loss-outcome that requires the amount."""
    resp = client.post(
        f"/ui/merchants/{merchant.id}/decisions/{decision_id}/outcome",
        data={
            "funder_decision": "approved",
            "outcome": "defaulted",
        },
    )
    assert resp.status_code == 400


def test_charged_off_with_amount_persists_charge_off(
    client: TestClient,
    merchant: MerchantRow,
    decision_id: UUID,
    fake_supabase: _FakeSupabase,
) -> None:
    resp = client.post(
        f"/ui/merchants/{merchant.id}/decisions/{decision_id}/outcome",
        data={
            "funder_decision": "approved",
            "outcome": "charged_off",
            "funded_amount": "50000",
            "charge_off_amount": "30000",
        },
    )
    assert resp.status_code == 200, resp.text
    row = fake_supabase.by_table["deal_outcomes"].rows[0]
    assert row["outcome"] == "charged_off"
    assert row["charge_off_amount"] == "30000"


def test_non_loss_outcome_drops_charge_off_amount(
    client: TestClient,
    merchant: MerchantRow,
    decision_id: UUID,
    fake_supabase: _FakeSupabase,
) -> None:
    """A stale modal POST with an irrelevant charge_off_amount on a
    paid_in_full outcome must store NULL, not the stale value."""
    resp = client.post(
        f"/ui/merchants/{merchant.id}/decisions/{decision_id}/outcome",
        data={
            "funder_decision": "approved",
            "outcome": "paid_in_full",
            "charge_off_amount": "30000",  # bogus — must be dropped
        },
    )
    assert resp.status_code == 200, resp.text
    row = fake_supabase.by_table["deal_outcomes"].rows[0]
    assert row["outcome"] == "paid_in_full"
    assert row["charge_off_amount"] is None


def test_unknown_merchant_returns_404(
    client: TestClient,
    decision_id: UUID,
) -> None:
    resp = client.post(
        f"/ui/merchants/{uuid4()}/decisions/{decision_id}/outcome",
        data={
            "funder_decision": "approved",
            "outcome": "paying",
        },
    )
    assert resp.status_code == 404
