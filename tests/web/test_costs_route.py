"""Tests for ``GET /ui/costs`` (migration 078 surface)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_llm_cost_repository,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.llm_cost_repository import InMemoryLLMCostRepository


@pytest.fixture
def cost_repo() -> InMemoryLLMCostRepository:
    return InMemoryLLMCostRepository()


@pytest.fixture
def merchant_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def client(
    cost_repo: InMemoryLLMCostRepository,
    merchant_repo: InMemoryMerchantRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_llm_cost_repository] = lambda: cost_repo
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_merchant(merchant_repo: InMemoryMerchantRepository, *, business_name: str) -> MerchantRow:
    row = MerchantRow(id=uuid4(), business_name=business_name, status="finalized")
    return merchant_repo.upsert(row)


def test_costs_page_renders_with_empty_state(client: TestClient) -> None:
    resp = client.get("/ui/costs")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-test-id="costs-page"' in body
    assert 'data-test-id="costs-per-merchant-empty"' in body
    assert 'data-test-id="costs-per-document-empty"' in body
    assert 'data-test-id="costs-monthly-trend-empty"' in body


def test_costs_page_renders_per_merchant_with_seeded_data(
    client: TestClient,
    cost_repo: InMemoryLLMCostRepository,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    merchant = _seed_merchant(merchant_repo, business_name="ACME LLC")
    inside_ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    cost_repo.insert(
        merchant_id=merchant.id,
        document_id=uuid4(),
        model_id="us.anthropic.claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        estimated_cost_usd=Decimal("0.010500"),
        call_type="extraction",
        called_at=inside_ts,
    )

    resp = client.get("/ui/costs?month=2026-06")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-test-id="costs-per-merchant-table"' in body
    assert "ACME LLC" in body
    # Cost shown with 2-decimal USD display.
    assert "$0.01" in body
    # Selected month echoed in the form input.
    assert 'value="2026-06"' in body


def test_costs_page_excludes_rows_outside_selected_month(
    client: TestClient,
    cost_repo: InMemoryLLMCostRepository,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    merchant = _seed_merchant(merchant_repo, business_name="OUT-OF-WINDOW LLC")
    # Insert ONLY in May; query June.
    cost_repo.insert(
        merchant_id=merchant.id,
        document_id=None,
        model_id="us.anthropic.claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=Decimal("0.001"),
        call_type="extraction",
        called_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
    )

    resp = client.get("/ui/costs?month=2026-06")
    assert resp.status_code == 200
    body = resp.text
    assert "OUT-OF-WINDOW LLC" not in body
    assert 'data-test-id="costs-per-merchant-empty"' in body


def test_costs_page_renders_monthly_trend(
    client: TestClient,
    cost_repo: InMemoryLLMCostRepository,
) -> None:
    # Two months of data — both should appear in the trend table.
    cost_repo.insert(
        merchant_id=None,
        document_id=None,
        model_id="m",
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=Decimal("0.001"),
        call_type="extraction",
        called_at=datetime(2026, 5, 15, tzinfo=UTC),
    )
    cost_repo.insert(
        merchant_id=None,
        document_id=None,
        model_id="m",
        input_tokens=200,
        output_tokens=100,
        estimated_cost_usd=Decimal("0.002"),
        call_type="extraction",
        called_at=datetime(2026, 6, 15, tzinfo=UTC),
    )

    resp = client.get("/ui/costs?month=2026-06")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-test-id="costs-monthly-trend-table"' in body
    assert "2026-05" in body
    assert "2026-06" in body


def test_costs_page_call_count_summary(
    client: TestClient,
    cost_repo: InMemoryLLMCostRepository,
) -> None:
    inside_ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    for _ in range(3):
        cost_repo.insert(
            merchant_id=None,
            document_id=None,
            model_id="m",
            input_tokens=100,
            output_tokens=50,
            estimated_cost_usd=Decimal("0.001"),
            call_type="extraction",
            called_at=inside_ts,
        )

    resp = client.get("/ui/costs?month=2026-06")
    body = resp.text
    # Summary card shows the call count.
    assert 'data-test-id="costs-call-count"' in body
    assert ">3<" in body  # the cards render `{{ call_count }}` directly
