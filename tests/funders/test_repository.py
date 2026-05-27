"""InMemoryFunderRepository CRUD tests."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.funders.models import FunderRow, FunderTier
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


def test_default_empty_contact_and_tiers() -> None:
    f = _funder()
    assert f.contact_name == ""
    assert f.contact_phone == ""
    assert f.contact_email == ""
    assert f.submission_email == ""
    assert f.tiers == ()
    assert f.auto_decline_conditions == ()
    assert f.conditional_requirements == ()
    assert f.notes == ""
    assert f.notes_residual == ""


def test_notes_and_notes_residual_round_trip_independently() -> None:
    repo = InMemoryFunderRepository()
    f = _funder().model_copy(update={
        "notes": "Operator: called Jim, prioritising trucking next month.",
        "notes_residual": "Renewals: case-by-case after 50% paid down.",
    })
    repo.upsert(f)
    got = repo.get(f.id)
    assert got.notes == "Operator: called Jim, prioritising trucking next month."
    assert got.notes_residual == "Renewals: case-by-case after 50% paid down."


def test_contact_fields_round_trip() -> None:
    repo = InMemoryFunderRepository()
    f = _funder().model_copy(update={
        "contact_name":     "James Doe",
        "contact_phone":    "555-123-4567",
        "contact_email":    "james@logicadvance.com",
        "submission_email": "iso@logicadvance.com",
    })
    repo.upsert(f)
    got = repo.get(f.id)
    assert got.contact_name == "James Doe"
    assert got.contact_phone == "555-123-4567"
    assert got.contact_email == "james@logicadvance.com"
    assert got.submission_email == "iso@logicadvance.com"


def test_tiers_round_trip_preserves_decimal_precision() -> None:
    repo = InMemoryFunderRepository()
    elite = FunderTier(
        name="Elite",
        buy_rate_low=Decimal("1.25"),
        buy_rate_high=Decimal("1.30"),
        min_monthly_revenue=Decimal("100000.00"),
        min_credit_score=700,
        min_months_in_business=60,
        max_positions=1,
        max_advance=Decimal("1500000.00"),
        max_holdback=Decimal("0.15"),
    )
    a_tier = FunderTier(name="A", buy_rate_low=Decimal("1.28"))
    f = _funder().model_copy(update={"tiers": (elite, a_tier)})
    repo.upsert(f)
    got = repo.get(f.id)
    assert got.tiers == (elite, a_tier)
    assert got.tiers[0].buy_rate_low == Decimal("1.25")
    assert got.tiers[0].max_holdback == Decimal("0.15")


def test_auto_decline_and_conditional_lists_round_trip() -> None:
    repo = InMemoryFunderRepository()
    f = _funder().model_copy(update={
        "auto_decline_conditions": (
            "Restaurants with <12 mo TIB",
            "Active tax liens > $25K",
        ),
        "conditional_requirements": (
            "Trucking: 2 yr MVR clean",
            "Construction: WC certificate",
        ),
    })
    repo.upsert(f)
    got = repo.get(f.id)
    assert got.auto_decline_conditions == (
        "Restaurants with <12 mo TIB",
        "Active tax liens > $25K",
    )
    assert got.conditional_requirements == (
        "Trucking: 2 yr MVR clean",
        "Construction: WC certificate",
    )


def test_funder_tier_rejects_inverted_buy_rate() -> None:
    with pytest.raises(ValueError, match="buy_rate_low"):
        FunderTier(
            name="Bad",
            buy_rate_low=Decimal("1.40"),
            buy_rate_high=Decimal("1.30"),
        )


def test_funder_tier_accepts_equal_buy_rates() -> None:
    t = FunderTier(
        name="Flat",
        buy_rate_low=Decimal("1.30"),
        buy_rate_high=Decimal("1.30"),
    )
    assert t.buy_rate_low == t.buy_rate_high


def test_funder_tier_accepts_either_bound_missing() -> None:
    # Only one of (low, high) set — validator skips, no error.
    FunderTier(name="OnlyLow",  buy_rate_low=Decimal("1.25"))
    FunderTier(name="OnlyHigh", buy_rate_high=Decimal("1.30"))
