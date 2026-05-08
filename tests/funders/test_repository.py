"""InMemoryFunderRepository CRUD tests."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.funders.models import FunderRow
from aegis.funders.repository import (
    FunderNotFoundError,
    InMemoryFunderRepository,
)


def _funder(name: str = "Acme Capital") -> FunderRow:
    return FunderRow(
        id=uuid4(),
        name=name,
        min_monthly_revenue=Decimal("25000.00"),
        min_credit_score=580,
    )


def test_upsert_and_get_round_trip() -> None:
    repo = InMemoryFunderRepository()
    f = _funder()
    repo.upsert(f)
    assert repo.get(f.id) == f


def test_get_missing_raises() -> None:
    repo = InMemoryFunderRepository()
    with pytest.raises(FunderNotFoundError):
        repo.get(uuid4())


def test_list_active_only() -> None:
    repo = InMemoryFunderRepository()
    a = _funder("Active Co")
    inactive = _funder("Retired Co").model_copy(update={"active": False})
    repo.upsert(a)
    repo.upsert(inactive)
    listed = repo.list_active()
    assert [f.name for f in listed] == ["Active Co"]


def test_list_active_sorted_by_name() -> None:
    repo = InMemoryFunderRepository()
    repo.upsert(_funder("Zeta Fund"))
    repo.upsert(_funder("Alpha Fund"))
    repo.upsert(_funder("Mid Fund"))
    listed = repo.list_active()
    assert [f.name for f in listed] == ["Alpha Fund", "Mid Fund", "Zeta Fund"]


def test_upsert_replaces_existing_by_id() -> None:
    repo = InMemoryFunderRepository()
    f = _funder()
    repo.upsert(f)
    updated = f.model_copy(update={"min_credit_score": 620})
    repo.upsert(updated)
    assert repo.get(f.id).min_credit_score == 620


def test_name_uniqueness_rejected_for_different_id() -> None:
    repo = InMemoryFunderRepository()
    a = _funder("Same Name LLC")
    b = _funder("Same Name LLC")
    repo.upsert(a)
    with pytest.raises(ValueError, match="name conflict"):
        repo.upsert(b)


def test_delete() -> None:
    repo = InMemoryFunderRepository()
    f = _funder()
    repo.upsert(f)
    repo.delete(f.id)
    with pytest.raises(FunderNotFoundError):
        repo.get(f.id)


def test_delete_missing_is_noop() -> None:
    repo = InMemoryFunderRepository()
    repo.delete(uuid4())  # should not raise
