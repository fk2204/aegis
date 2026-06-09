"""Tests for ``GET /ui/renewals`` (R3.2 — operator-visibility calendar).

The route ships as an OPERATOR VISIBILITY surface only — per
``.claude/rules/compliance.md`` the funder owns the regulator-facing
renewal-disclosure obligation. These tests verify the route renders,
honors the ``window_days`` query parameter, sorts by urgency, and
degrades gracefully when the ``maturity_date`` column is absent from
the schema (current state on ``main`` as of 2026-06-09).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import ClassVar
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ConfigDict

from aegis.api.app import create_app
from aegis.api.deps import reset_dependency_caches
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    list_upcoming_renewals,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Plain TestClient backed by the default in-memory repositories.

    The empty-state branch (schema-absent) is the happy path on main
    today, so most tests run against this. Tests that need populated
    rows use the dependency override below.
    """
    reset_dependency_caches()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_dependency_caches()


class _MerchantWithMaturity(MerchantRow):
    """Pydantic subclass that carries a ``maturity_date`` field.

    Simulates the post-schema-augmentation shape so the route + accessor
    can be tested end-to-end without writing a migration. The production
    ``MerchantRow`` will grow this field in an additive change once the
    operator approves the column add; the accessor reads it via
    ``getattr`` so it picks up the field automatically.
    """

    # Override ``extra`` to "allow" so the synthetic ``maturity_date``
    # column survives the StrictModel's default ``extra="forbid"`` from
    # the parent. ``ClassVar[ConfigDict]`` keeps mypy --strict happy
    # against MerchantRow's typed slot.
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="allow",
        validate_assignment=True,
        str_strip_whitespace=True,
    )
    maturity_date: date | None = None


def _merchant(
    *,
    business_name: str,
    state: str | None,
    is_renewal: bool,
    maturity_date: date | None,
    industry_naics: str | None = None,
) -> _MerchantWithMaturity:
    return _MerchantWithMaturity(
        id=uuid4(),
        business_name=business_name,
        state=state,
        is_renewal=is_renewal,
        maturity_date=maturity_date,
        industry_naics=industry_naics,
    )


@pytest.fixture
def repo_with_renewals() -> InMemoryMerchantRepository:
    """In-memory repo seeded with a known mix of renewals + non-renewals."""
    repo = InMemoryMerchantRepository()
    today = date(2026, 6, 9)
    # NY renewal — 25 days out; well under the 30-day NY deadline.
    repo.upsert(
        _merchant(
            business_name="NY Pizza LLC",
            state="NY",
            is_renewal=True,
            maturity_date=today + timedelta(days=25),
            industry_naics="722511",
        )
    )
    # CA renewal — 50 days out; under the 60-day CA deadline.
    repo.upsert(
        _merchant(
            business_name="CA Salon Inc",
            state="CA",
            is_renewal=True,
            maturity_date=today + timedelta(days=50),
        )
    )
    # FL renewal — 80 days out; no state-deadline column applies (N/A).
    repo.upsert(
        _merchant(
            business_name="FL Auto LLC",
            state="FL",
            is_renewal=True,
            maturity_date=today + timedelta(days=80),
        )
    )
    # Renewal beyond the default window — filtered out at default 90.
    repo.upsert(
        _merchant(
            business_name="GA Roofing LLC",
            state="GA",
            is_renewal=True,
            maturity_date=today + timedelta(days=120),
        )
    )
    # Not a renewal — filtered.
    repo.upsert(
        _merchant(
            business_name="NY New Deal Inc",
            state="NY",
            is_renewal=False,
            maturity_date=today + timedelta(days=10),
        )
    )
    # Renewal but maturity already past — filtered.
    repo.upsert(
        _merchant(
            business_name="Past Maturity LLC",
            state="CA",
            is_renewal=True,
            maturity_date=today - timedelta(days=5),
        )
    )
    return repo


@pytest.fixture
def client_with_renewals(
    repo_with_renewals: InMemoryMerchantRepository,
) -> Iterator[TestClient]:
    """TestClient whose ``MerchantRepository`` dependency returns the
    populated repo above. Overrides via ``app.dependency_overrides`` so
    every request in the test hits the same in-memory state."""
    reset_dependency_caches()
    app = create_app()
    from aegis.api.deps import get_merchant_repository

    app.dependency_overrides[get_merchant_repository] = lambda: repo_with_renewals
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Empty-state — schema augmentation pending (current main)
# ---------------------------------------------------------------------------


def test_renewals_route_renders_200_with_banner_on_empty_schema(
    client: TestClient,
) -> None:
    resp = client.get("/ui/renewals")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The operator-visibility banner is present regardless of data state.
    assert "operator-visibility" in body.lower() or "operator visibility" in body.lower()
    assert "funder owns" in body.lower()
    # Empty-state copy surfaces the schema gap honestly.
    assert "maturity" in body.lower()


def test_renewals_route_empty_state_when_no_renewals_match(
    client_with_renewals: TestClient,
    repo_with_renewals: InMemoryMerchantRepository,
) -> None:
    """With a real ``maturity_date`` field on rows but ``window_days=1``,
    no row matches — verifies the "no renewals approaching" branch is
    distinct from the schema-missing branch."""
    # All seeded renewals are >= 25 days out, so a 1-day window filters
    # everything. Schema IS present so the banner should NOT mention
    # "schema augmentation pending."
    resp = client_with_renewals.get("/ui/renewals?window_days=1")
    assert resp.status_code == 200, resp.text
    assert "No renewals approaching maturity" in resp.text


# ---------------------------------------------------------------------------
# Populated rows
# ---------------------------------------------------------------------------


def test_renewals_route_lists_matching_rows_sorted_by_urgency(
    client_with_renewals: TestClient,
) -> None:
    """Default 90-day window picks up NY 25d, CA 50d, FL 80d — and skips
    the 120d GA, the non-renewal, and the past-maturity row. Sorted by
    days_until_maturity ascending: NY first, FL last."""
    resp = client_with_renewals.get("/ui/renewals")
    assert resp.status_code == 200, resp.text
    body = resp.text
    ny_idx = body.find("NY Pizza LLC")
    ca_idx = body.find("CA Salon Inc")
    fl_idx = body.find("FL Auto LLC")
    assert ny_idx != -1, "NY renewal row missing"
    assert ca_idx != -1, "CA renewal row missing"
    assert fl_idx != -1, "FL renewal row missing"
    assert ny_idx < ca_idx < fl_idx, "rows not sorted most-urgent first"
    # Filtered rows are absent.
    assert "GA Roofing LLC" not in body  # beyond window
    assert "NY New Deal Inc" not in body  # not a renewal
    assert "Past Maturity LLC" not in body  # already past


def test_renewals_route_narrows_via_window_query_param(
    client_with_renewals: TestClient,
) -> None:
    """``window_days=40`` keeps only the 25-day NY renewal; drops the
    50-day CA and the 80-day FL."""
    resp = client_with_renewals.get("/ui/renewals?window_days=40")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "NY Pizza LLC" in body
    assert "CA Salon Inc" not in body
    assert "FL Auto LLC" not in body


def test_renewals_route_rejects_out_of_range_window(
    client: TestClient,
) -> None:
    """FastAPI's Query(ge=1, le=365) returns 422 on invalid input."""
    resp = client.get("/ui/renewals?window_days=0")
    assert resp.status_code == 422
    resp = client.get("/ui/renewals?window_days=10000")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Accessor-level: state-deadline arithmetic
# ---------------------------------------------------------------------------


def test_list_upcoming_renewals_returns_empty_when_no_maturity_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Migration 039 landed the ``maturity_date`` column. A renewing row
    with the column unset (``None``) is filtered out by the accessor and
    triggers the "no renewals in window" INFO line."""
    repo = InMemoryMerchantRepository()
    repo.upsert(
        MerchantRow(
            business_name="Acme Renewal LLC",
            state="CA",
            is_renewal=True,
        )
    )
    import logging

    with caplog.at_level(logging.INFO, logger="aegis.merchants.repository"):
        result = list_upcoming_renewals(repo)
    assert result == []
    assert any(
        "no renewals in window" in r.message for r in caplog.records
    )


def test_list_upcoming_renewals_ny_state_deadline_uses_30_day_lead() -> None:
    """NY merchant 45 days out: 30-day pre-maturity deadline → 15 days
    until the funder's NY § 600.17 disclosure deadline."""
    today = date(2026, 6, 9)
    repo = InMemoryMerchantRepository()
    repo.upsert(
        _merchant(
            business_name="NY Test LLC",
            state="NY",
            is_renewal=True,
            maturity_date=today + timedelta(days=45),
        )
    )
    rows = list_upcoming_renewals(repo, today=today)
    assert len(rows) == 1
    row = rows[0]
    assert row.days_until_maturity == 45
    assert row.days_until_state_deadline == 15  # 45 - 30


def test_list_upcoming_renewals_ca_state_deadline_uses_60_day_lead() -> None:
    """CA merchant 70 days out: 60-day pre-maturity deadline → 10 days
    until the funder's CA SB 362 disclosure deadline."""
    today = date(2026, 6, 9)
    repo = InMemoryMerchantRepository()
    repo.upsert(
        _merchant(
            business_name="CA Test LLC",
            state="CA",
            is_renewal=True,
            maturity_date=today + timedelta(days=70),
        )
    )
    rows = list_upcoming_renewals(repo, today=today)
    assert len(rows) == 1
    assert rows[0].days_until_maturity == 70
    assert rows[0].days_until_state_deadline == 10  # 70 - 60


def test_list_upcoming_renewals_state_with_no_lead_returns_none() -> None:
    """Non-CA/NY merchant has no AEGIS-tracked state deadline → None."""
    today = date(2026, 6, 9)
    repo = InMemoryMerchantRepository()
    repo.upsert(
        _merchant(
            business_name="FL Test LLC",
            state="FL",
            is_renewal=True,
            maturity_date=today + timedelta(days=45),
        )
    )
    rows = list_upcoming_renewals(repo, today=today)
    assert len(rows) == 1
    assert rows[0].days_until_state_deadline is None


def test_list_upcoming_renewals_status_always_funder_owns() -> None:
    """Per the SCOPE NOTE, AEGIS has no signal on funder-side
    transmission status today, so every row defaults to
    ``not_required_funder_owns``."""
    today = date(2026, 6, 9)
    repo = InMemoryMerchantRepository()
    for st in ("CA", "NY", "FL", "GA"):
        repo.upsert(
            _merchant(
                business_name=f"{st} Test LLC",
                state=st,
                is_renewal=True,
                maturity_date=today + timedelta(days=20),
            )
        )
    rows = list_upcoming_renewals(repo, today=today)
    assert len(rows) == 4
    assert all(r.renewal_status == "not_required_funder_owns" for r in rows)


def test_list_upcoming_renewals_rejects_invalid_window() -> None:
    with pytest.raises(ValueError, match="window_days must be positive"):
        list_upcoming_renewals(InMemoryMerchantRepository(), window_days=0)
