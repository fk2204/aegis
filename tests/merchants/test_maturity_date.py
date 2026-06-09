"""Unit tests for ``merchants.maturity_date`` (migration 039 — R3.2 follow-up).

Verifies the post-migration shape: ``MerchantRow.maturity_date`` round-trips
through the in-memory repository, and ``list_upcoming_renewals`` returns
rows whose maturity falls in the window with the state-disclosure-deadline
math intact (CA=60d, NY=30d, other states=None).

The column is operator-visibility only; AEGIS does not own regulator-facing
renewal disclosures (see CLAUDE.md mission statement + the
``_STATE_DISCLOSURE_LEAD_DAYS`` docstring in
``src/aegis/merchants/repository.py``).
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest

from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    list_upcoming_renewals,
)


@pytest.fixture
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


def _merchant(
    *,
    business_name: str,
    state: str | None,
    is_renewal: bool,
    maturity_date: date | None,
    industry_naics: str | None = None,
) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name=business_name,
        state=state,
        is_renewal=is_renewal,
        maturity_date=maturity_date,
        industry_naics=industry_naics,
    )


# -- model + repo round-trip ------------------------------------------------


def test_merchant_row_accepts_maturity_date() -> None:
    """``MerchantRow`` carries ``maturity_date`` as ``date | None``."""
    m = MerchantRow(business_name="Acme LLC", maturity_date=date(2026, 9, 1))
    assert m.maturity_date == date(2026, 9, 1)


def test_merchant_row_maturity_date_defaults_to_none() -> None:
    """Default value is ``None`` — no implicit seed."""
    m = MerchantRow(business_name="Acme LLC")
    assert m.maturity_date is None


def test_repo_upsert_round_trips_maturity_date(
    repo: InMemoryMerchantRepository,
) -> None:
    """``maturity_date`` persists through ``upsert`` and ``get``."""
    target = date(2026, 8, 15)
    stored = repo.upsert(
        _merchant(
            business_name="NY Pizza LLC",
            state="NY",
            is_renewal=True,
            maturity_date=target,
        )
    )
    fetched = repo.get(stored.id)
    assert fetched.maturity_date == target


# -- list_upcoming_renewals --------------------------------------------------


def test_list_upcoming_renewals_returns_row_with_maturity(
    repo: InMemoryMerchantRepository,
) -> None:
    """A single renewing merchant within the window surfaces as one summary."""
    today = date(2026, 6, 9)
    repo.upsert(
        _merchant(
            business_name="NY Pizza LLC",
            state="NY",
            is_renewal=True,
            maturity_date=today + timedelta(days=20),
        )
    )
    summaries = list_upcoming_renewals(repo, today=today)
    assert len(summaries) == 1
    s = summaries[0]
    assert s.business_name == "NY Pizza LLC"
    assert s.state == "NY"
    assert s.days_until_maturity == 20
    # NY = 30-day lead → days_until_state_deadline = 20 - 30 = -10
    assert s.days_until_state_deadline == -10
    assert s.renewal_status == "not_required_funder_owns"


def test_list_upcoming_renewals_filters_non_renewals(
    repo: InMemoryMerchantRepository,
) -> None:
    """A merchant with ``is_renewal=False`` is excluded even when
    ``maturity_date`` is set."""
    today = date(2026, 6, 9)
    repo.upsert(
        _merchant(
            business_name="Not A Renewal",
            state="NY",
            is_renewal=False,
            maturity_date=today + timedelta(days=20),
        )
    )
    assert list_upcoming_renewals(repo, today=today) == []


def test_list_upcoming_renewals_filters_missing_maturity(
    repo: InMemoryMerchantRepository,
) -> None:
    """A renewing merchant with no ``maturity_date`` is excluded."""
    today = date(2026, 6, 9)
    repo.upsert(
        _merchant(
            business_name="Maturity Unknown LLC",
            state="CA",
            is_renewal=True,
            maturity_date=None,
        )
    )
    assert list_upcoming_renewals(repo, today=today) == []


def test_list_upcoming_renewals_filters_past_and_far_future(
    repo: InMemoryMerchantRepository,
) -> None:
    """Past maturities and out-of-window futures are excluded."""
    today = date(2026, 6, 9)
    repo.upsert(
        _merchant(
            business_name="Past Maturity LLC",
            state="CA",
            is_renewal=True,
            maturity_date=today - timedelta(days=5),
        )
    )
    repo.upsert(
        _merchant(
            business_name="Far Future LLC",
            state="GA",
            is_renewal=True,
            maturity_date=today + timedelta(days=120),
        )
    )
    assert list_upcoming_renewals(repo, window_days=90, today=today) == []


def test_list_upcoming_renewals_sorted_by_urgency(
    repo: InMemoryMerchantRepository,
) -> None:
    """Rows are returned ascending by ``days_until_maturity``."""
    today = date(2026, 6, 9)
    repo.upsert(
        _merchant(
            business_name="Far",
            state="FL",
            is_renewal=True,
            maturity_date=today + timedelta(days=80),
        )
    )
    repo.upsert(
        _merchant(
            business_name="Near",
            state="NY",
            is_renewal=True,
            maturity_date=today + timedelta(days=10),
        )
    )
    repo.upsert(
        _merchant(
            business_name="Mid",
            state="CA",
            is_renewal=True,
            maturity_date=today + timedelta(days=45),
        )
    )
    summaries = list_upcoming_renewals(repo, today=today)
    assert [s.business_name for s in summaries] == ["Near", "Mid", "Far"]


def test_list_upcoming_renewals_state_deadline_none_for_other_states(
    repo: InMemoryMerchantRepository,
) -> None:
    """States outside CA / NY get ``days_until_state_deadline=None``."""
    today = date(2026, 6, 9)
    repo.upsert(
        _merchant(
            business_name="FL Auto",
            state="FL",
            is_renewal=True,
            maturity_date=today + timedelta(days=40),
        )
    )
    summaries = list_upcoming_renewals(repo, today=today)
    assert len(summaries) == 1
    assert summaries[0].days_until_state_deadline is None


def test_list_upcoming_renewals_state_deadline_ca_60_day_lead(
    repo: InMemoryMerchantRepository,
) -> None:
    """CA = 60-day pre-maturity lead."""
    today = date(2026, 6, 9)
    repo.upsert(
        _merchant(
            business_name="CA Salon",
            state="CA",
            is_renewal=True,
            maturity_date=today + timedelta(days=70),
        )
    )
    summaries = list_upcoming_renewals(repo, today=today)
    assert summaries[0].days_until_state_deadline == 70 - 60
