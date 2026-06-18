"""Tests for the migration-065 merchant soft-delete flow.

Covers the InMemoryMerchantRepository contract:

* ``soft_delete`` happy path: sets ``deleted_at``, returns the updated
  row, hides the merchant from ``get`` / ``list_all`` / ``count_total``
  / ``find_by_*`` reads.
* Double-delete raises ``MerchantNotFoundError`` rather than silently
  re-stamping the timestamp.
* Soft-delete on an unknown id raises ``MerchantNotFoundError``.
* Listing excludes soft-deleted rows even when filtered by state.
* The state filter still works for non-deleted rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    MerchantNotFoundError,
)


@pytest.fixture
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


def _seed(
    repo: InMemoryMerchantRepository,
    *,
    business_name: str = "Acme LLC",
    state: str | None = "CA",
    close_lead_id: str | None = None,
    close_opportunity_id: str | None = None,
    email: str | None = None,
) -> MerchantRow:
    m = MerchantRow(
        id=uuid4(),
        business_name=business_name,
        owner_name="Owner",
        state=state,
        close_lead_id=close_lead_id,
        close_opportunity_id=close_opportunity_id,
        email=email,
    )
    return repo.upsert(m)


def test_soft_delete_sets_deleted_at_and_returns_updated_row(
    repo: InMemoryMerchantRepository,
) -> None:
    m = _seed(repo)
    now = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)

    updated = repo.soft_delete(m.id, deleted_at=now)

    assert updated.id == m.id
    assert updated.deleted_at == now
    # The repo retains the row internally — soft-delete must preserve
    # history. ``get`` simply filters it out.
    assert m.id in repo._by_id


def test_get_404s_after_soft_delete(repo: InMemoryMerchantRepository) -> None:
    m = _seed(repo)
    repo.soft_delete(m.id, deleted_at=datetime.now(UTC))
    with pytest.raises(MerchantNotFoundError):
        repo.get(m.id)


def test_list_all_excludes_soft_deleted(repo: InMemoryMerchantRepository) -> None:
    active = _seed(repo, business_name="Active Co")
    deleted = _seed(repo, business_name="Tombstone Co")
    repo.soft_delete(deleted.id, deleted_at=datetime.now(UTC))

    rows = repo.list_all()

    assert [r.id for r in rows] == [active.id]
    assert all(r.deleted_at is None for r in rows)


def test_list_all_with_state_filter_excludes_soft_deleted(
    repo: InMemoryMerchantRepository,
) -> None:
    active_ca = _seed(repo, business_name="Active CA", state="CA")
    deleted_ca = _seed(repo, business_name="Deleted CA", state="CA")
    _seed(repo, business_name="Active NY", state="NY")
    repo.soft_delete(deleted_ca.id, deleted_at=datetime.now(UTC))

    rows = repo.list_all(state="CA")

    assert [r.id for r in rows] == [active_ca.id]


def test_count_total_excludes_soft_deleted(repo: InMemoryMerchantRepository) -> None:
    a = _seed(repo, business_name="One")
    b = _seed(repo, business_name="Two")
    _seed(repo, business_name="Three")

    assert repo.count_total() == 3
    repo.soft_delete(a.id, deleted_at=datetime.now(UTC))
    assert repo.count_total() == 2
    repo.soft_delete(b.id, deleted_at=datetime.now(UTC))
    assert repo.count_total() == 1


def test_find_by_close_lead_id_skips_soft_deleted(
    repo: InMemoryMerchantRepository,
) -> None:
    m = _seed(repo, close_lead_id="lead_abc")
    assert repo.find_by_close_lead_id("lead_abc") is not None

    repo.soft_delete(m.id, deleted_at=datetime.now(UTC))

    assert repo.find_by_close_lead_id("lead_abc") is None


def test_find_by_close_opportunity_id_skips_soft_deleted(
    repo: InMemoryMerchantRepository,
) -> None:
    m = _seed(repo, close_opportunity_id="oppo_abc")
    assert repo.find_by_close_opportunity_id("oppo_abc") is not None

    repo.soft_delete(m.id, deleted_at=datetime.now(UTC))

    assert repo.find_by_close_opportunity_id("oppo_abc") is None


def test_find_by_email_skips_soft_deleted(
    repo: InMemoryMerchantRepository,
) -> None:
    m = _seed(repo, email="ceo@acme.example")
    assert repo.find_by_email("ceo@acme.example") is not None

    repo.soft_delete(m.id, deleted_at=datetime.now(UTC))

    assert repo.find_by_email("ceo@acme.example") is None


def test_double_soft_delete_raises_not_found(
    repo: InMemoryMerchantRepository,
) -> None:
    m = _seed(repo)
    first = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    repo.soft_delete(m.id, deleted_at=first)

    with pytest.raises(MerchantNotFoundError):
        repo.soft_delete(m.id, deleted_at=datetime(2026, 6, 18, 13, 0, tzinfo=UTC))

    # First timestamp untouched — guard against silent re-stamp.
    assert repo._by_id[m.id].deleted_at == first


def test_soft_delete_unknown_id_raises_not_found(
    repo: InMemoryMerchantRepository,
) -> None:
    with pytest.raises(MerchantNotFoundError):
        repo.soft_delete(uuid4(), deleted_at=datetime.now(UTC))
