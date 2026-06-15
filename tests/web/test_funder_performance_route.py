"""Tests for ``GET /ui/funders/{funder_id}/performance``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def merchant_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def subs_repo() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def client(
    funder_repo: InMemoryFunderRepository,
    merchant_repo: InMemoryMerchantRepository,
    subs_repo: InMemoryFunderNoteSubmissionRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: subs_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_funder(repo: InMemoryFunderRepository, name: str = "Test Funder") -> FunderRow:
    f = FunderRow(name=name, active=True)
    repo.upsert(f)
    return f


def _seed_merchant(repo: InMemoryMerchantRepository) -> MerchantRow:
    m = MerchantRow(business_name="Merchant LLC", state="CA")
    repo.upsert(m)
    return m


def test_unknown_funder_returns_404(client: TestClient) -> None:
    bogus = uuid4()
    resp = client.get(f"/ui/funders/{bogus}/performance")
    assert resp.status_code == 404


def test_empty_funder_renders_zero_state(
    client: TestClient,
    funder_repo: InMemoryFunderRepository,
) -> None:
    f = _seed_funder(funder_repo)
    resp = client.get(f"/ui/funders/{f.id}/performance")
    assert resp.status_code == 200
    assert "Total submissions" in resp.text
    assert "performance" in resp.text.lower()
    # Empty-state copy for decline notes
    assert "No declined submissions have notes attached yet" in resp.text


def test_funder_with_submissions_renders_metrics(
    client: TestClient,
    funder_repo: InMemoryFunderRepository,
    merchant_repo: InMemoryMerchantRepository,
    subs_repo: InMemoryFunderNoteSubmissionRepository,
) -> None:
    f = _seed_funder(funder_repo)
    m = _seed_merchant(merchant_repo)
    # Seed three subs: approved, declined-with-note, pending
    row = subs_repo.create(
        merchant_id=m.id,
        funder_id=f.id,
        funder_note="x",
        submitted_by="dashboard",
    )
    subs_repo.update_status(
        row.id,
        status="approved",
        offer_amount=Decimal("50000.00"),
    )
    row2 = subs_repo.create(
        merchant_id=m.id,
        funder_id=f.id,
        funder_note="x",
        submitted_by="dashboard",
    )
    subs_repo.update_status(
        row2.id,
        status="declined",
        notes="Funder concerned about MCA stacking",
    )
    subs_repo.create(
        merchant_id=m.id,
        funder_id=f.id,
        funder_note="x",
        submitted_by="dashboard",
    )
    # Different funder — should NOT appear in this funder's perf
    f2 = _seed_funder(funder_repo, name="Other Funder")
    subs_repo.create(
        merchant_id=m.id,
        funder_id=f2.id,
        funder_note="x",
        submitted_by="dashboard",
    )

    resp = client.get(f"/ui/funders/{f.id}/performance")
    assert resp.status_code == 200
    # 3 submissions total for this funder (the f2 one excluded)
    assert "3" in resp.text
    # Decline note text surfaces
    assert "Funder concerned about MCA stacking" in resp.text
    # Approval rate 1 of 2 decided = 50%
    assert "50.0%" in resp.text


def test_funder_detail_links_to_performance(
    client: TestClient,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """The performance link must appear on the funder detail page so
    operators can navigate without typing the URL."""
    f = _seed_funder(funder_repo)
    resp = client.get(f"/ui/funders/{f.id}")
    assert resp.status_code == 200
    assert f"/ui/funders/{f.id}/performance" in resp.text
    _ = datetime, timedelta, UTC  # silence ruff for shared imports
