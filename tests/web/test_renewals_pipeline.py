"""Tests for the renewal-pipeline queue on ``GET /ui/renewals`` (Feature 3).

Operator-decision queue (NOT the attestation calendar): merchants
within 14 days of maturity (including overdue rows). Implementation
choice (b) — derive from ``maturity_date`` rather than ``funding_date
+ term_days`` (the two are equivalent; this avoids an extra DB
column). See ``aegis.merchants.repository.list_renewal_pipeline``.

Covers:

* Merchant with maturity 5 days out appears with days_until_maturity=5.
* Merchant with maturity 20 days out does NOT appear (outside window).
* Merchant with maturity in the past appears with NEGATIVE
  days_until_maturity (overdue rows surface as the highest urgency).
* Merchant with NO maturity_date does not appear.
* Empty pipeline renders the explicit empty-state copy.
* Ordering — maturity-date-ascending (overdue first, then soonest).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from pydantic import ConfigDict

from aegis.api.app import create_app
from aegis.api.deps import (
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    list_renewal_pipeline,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MerchantWithMaturity(MerchantRow):
    """Test subclass that survives Pydantic-strict ``extra="forbid"``
    when older fixtures attach a synthetic ``maturity_date`` — same
    shape ``tests/web/test_renewals_route.py`` uses.

    The production ``MerchantRow`` already has ``maturity_date`` (added
    in migration 039), so this subclass is functionally identical here;
    it stays around to keep the test surface consistent with the
    sibling renewals test file.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="allow",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


def _merchant(
    *,
    business_name: str,
    maturity_date: date | None,
) -> _MerchantWithMaturity:
    return _MerchantWithMaturity(
        business_name=business_name,
        state="CA",
        status="finalized",
        is_renewal=True,
        maturity_date=maturity_date,
    )


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def client(
    merchants: InMemoryMerchantRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Pure-function tests — exercise the projection without booting the app.
# ---------------------------------------------------------------------------


def test_merchant_5_days_out_appears_in_pipeline(
    merchants: InMemoryMerchantRepository,
) -> None:
    today = date(2026, 6, 15)
    merchant = _merchant(
        business_name="Acme Painting",
        maturity_date=today + timedelta(days=5),
    )
    merchants.upsert(merchant)

    rows = list_renewal_pipeline(merchants, today=today)

    assert len(rows) == 1
    assert rows[0].merchant_id == merchant.id
    assert rows[0].days_until_maturity == 5
    assert rows[0].business_name == "Acme Painting"


def test_merchant_20_days_out_not_in_pipeline_outside_window(
    merchants: InMemoryMerchantRepository,
) -> None:
    today = date(2026, 6, 15)
    merchants.upsert(_merchant(business_name="Future Co", maturity_date=today + timedelta(days=20)))

    rows = list_renewal_pipeline(merchants, today=today)

    assert rows == []


def test_overdue_merchant_appears_with_negative_days(
    merchants: InMemoryMerchantRepository,
) -> None:
    """Operator wants visibility on past-maturity merchants nobody
    followed up on — they're the highest-urgency renewals."""
    today = date(2026, 6, 15)
    merchant = _merchant(
        business_name="Overdue Inc",
        maturity_date=today - timedelta(days=10),
    )
    merchants.upsert(merchant)

    rows = list_renewal_pipeline(merchants, today=today)

    assert len(rows) == 1
    assert rows[0].merchant_id == merchant.id
    assert rows[0].days_until_maturity == -10


def test_merchant_with_no_maturity_date_does_not_appear(
    merchants: InMemoryMerchantRepository,
) -> None:
    today = date(2026, 6, 15)
    merchants.upsert(_merchant(business_name="No Maturity", maturity_date=None))

    rows = list_renewal_pipeline(merchants, today=today)

    assert rows == []


def test_pipeline_orders_by_maturity_date_ascending(
    merchants: InMemoryMerchantRepository,
) -> None:
    today = date(2026, 6, 15)
    # Insert intentionally out-of-order so the sort is testable.
    merchants.upsert(_merchant(business_name="Soonest", maturity_date=today + timedelta(days=3)))
    merchants.upsert(_merchant(business_name="Overdue", maturity_date=today - timedelta(days=5)))
    merchants.upsert(_merchant(business_name="Tomorrow", maturity_date=today + timedelta(days=1)))

    rows = list_renewal_pipeline(merchants, today=today)

    # Overdue (-5) first, then +1, then +3.
    assert [r.days_until_maturity for r in rows] == [-5, 1, 3]
    assert [r.business_name for r in rows] == ["Overdue", "Tomorrow", "Soonest"]


def test_pipeline_includes_only_merchants_inside_lookahead(
    merchants: InMemoryMerchantRepository,
) -> None:
    today = date(2026, 6, 15)
    inside = _merchant(
        business_name="Inside",
        maturity_date=today + timedelta(days=14),
    )
    outside = _merchant(
        business_name="Outside",
        maturity_date=today + timedelta(days=15),
    )
    merchants.upsert(inside)
    merchants.upsert(outside)

    rows = list_renewal_pipeline(merchants, today=today)

    ids = {r.merchant_id for r in rows}
    assert inside.id in ids
    assert outside.id not in ids


# ---------------------------------------------------------------------------
# Route-level tests — render through /ui/renewals.
# ---------------------------------------------------------------------------


def test_renewals_pipeline_renders_section_with_qualifying_merchant(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
) -> None:
    today = date.today()
    merchants.upsert(
        _merchant(
            business_name="Pipeline Painting LLC",
            maturity_date=today + timedelta(days=7),
        )
    )

    resp = client.get("/ui/renewals")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Section header + table row visible.
    assert "Renewal pipeline" in body
    assert "Pipeline Painting LLC" in body
    assert 'data-test-id="renewal-pipeline-table"' in body


def test_renewals_pipeline_renders_empty_state_when_no_qualifying_merchant(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
) -> None:
    # No merchants seeded.
    resp = client.get("/ui/renewals")
    assert resp.status_code == 200, resp.text
    assert 'data-test-id="renewal-pipeline-empty"' in resp.text
    assert "No merchants are within 14 days of maturity" in resp.text


def test_renewals_pipeline_does_not_include_merchant_outside_window(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
) -> None:
    today = date.today()
    merchants.upsert(
        _merchant(
            business_name="Far Future Co",
            maturity_date=today + timedelta(days=30),
        )
    )

    resp = client.get("/ui/renewals")
    assert resp.status_code == 200
    body = resp.text
    # The pipeline section must render its empty state — the merchant
    # is outside the 14-day window. (The 90-day attestation calendar
    # below renders the same merchant; the pure-function test above
    # pins the filter contract independent of route layout.)
    assert 'data-test-id="renewal-pipeline-empty"' in body
    # Confirm the merchant doesn't appear inside the pipeline section
    # by checking that the pipeline-table marker is absent.
    assert 'data-test-id="renewal-pipeline-table"' not in body
