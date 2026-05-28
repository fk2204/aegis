"""POST /ui/merchants/{merchant_id}/close-rescan — manual rescan button.

Covers:
* Happy path enqueues with trigger='rescan' and operator's email
* ?override_cap=true threads through to the enqueue
* 404 when merchant has no close_lead_id
* 404 when merchant doesn't exist
* Cap-override button visibility (rendered iff latest close.orchestration.complete
  audit row carried capped=true)
* close.orchestration.manual_rescan audit row written
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchant_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    merchant_repo: InMemoryMerchantRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    close_lead_id: str | None = "lead_abc",
) -> MerchantRow:
    m = MerchantRow(
        id=uuid4(),
        business_name="Rescan Merchant",
        owner_name="Owner",
        state="CA",
        close_lead_id=close_lead_id,
    )
    repo.upsert(m)
    return m


def test_rescan_happy_path_enqueues_with_operator_email(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo)
    resp = client.post(
        f"/ui/merchants/{m.id}/close-rescan",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ui/merchants/{m.id}"

    pending = getattr(
        client.app.state, "pending_close_orchestration_jobs", []  # type: ignore[attr-defined]
    )
    assert pending == [{
        "close_lead_id": "lead_abc",
        "trigger": "rescan",
        "actor_email": "filip@commerafunding.com",
        "override_cap": False,
    }]

    actions = [e["action"] for e in audit.entries]
    assert "close.orchestration.enqueued" in actions
    assert "close.orchestration.manual_rescan" in actions

    manual = next(
        e for e in audit.entries
        if e["action"] == "close.orchestration.manual_rescan"
    )
    assert manual["actor_email"] == "filip@commerafunding.com"
    assert manual["details"]["override_cap"] is False
    assert manual["details"]["close_lead_id"] == "lead_abc"


def test_rescan_with_override_cap_threads_through(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo)
    resp = client.post(
        f"/ui/merchants/{m.id}/close-rescan?override_cap=true",
        headers={CF_ACCESS_EMAIL_HEADER: "dima@commerafunding.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    pending = getattr(
        client.app.state, "pending_close_orchestration_jobs", []  # type: ignore[attr-defined]
    )
    assert pending[0]["override_cap"] is True
    assert pending[0]["actor_email"] == "dima@commerafunding.com"
    assert pending[0]["trigger"] == "rescan"

    manual = next(
        e for e in audit.entries
        if e["action"] == "close.orchestration.manual_rescan"
    )
    assert manual["details"]["override_cap"] is True


def test_rescan_404_when_no_close_lead_id(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    m = _seed_merchant(merchant_repo, close_lead_id=None)
    resp = client.post(
        f"/ui/merchants/{m.id}/close-rescan",
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert "no close_lead_id" in resp.json()["detail"]


def test_rescan_404_when_merchant_missing(
    client: TestClient,
) -> None:
    bogus = uuid4()
    resp = client.post(
        f"/ui/merchants/{bogus}/close-rescan",
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_rescan_button_hidden_when_no_close_lead_id(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """Merchant detail must not render the rescan form when there's no
    linked Close Lead."""
    m = _seed_merchant(merchant_repo, close_lead_id=None)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Rescan Close attachments" not in resp.text


def test_rescan_button_visible_when_close_lead_set_no_cap(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """Primary rescan button visible; cap-override button hidden when
    latest orchestration didn't hit the cap."""
    m = _seed_merchant(merchant_repo)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Rescan Close attachments" in resp.text
    assert "Rescan all (override cap)" not in resp.text


def test_cap_override_button_visible_after_capped_run(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """When the most recent close.orchestration.complete row has
    capped=true, the merchant detail surfaces the override button."""
    m = _seed_merchant(merchant_repo)
    # Simulate a prior capped orchestration run via direct audit insert.
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={
            "trigger": "webhook",
            "close_lead_id": "lead_abc",
            "total": 17,
            "fetched": 15,
            "skipped": 0,
            "failed": 0,
            "duplicates": 0,
            "capped": True,
            "override_cap": False,
        },
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Rescan all (override cap)" in resp.text


def test_cap_override_button_hidden_after_uncapped_run(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """A capped:false summary on the most recent row → no override button.
    Even if an OLDER row was capped, the latest one wins."""
    m = _seed_merchant(merchant_repo)
    # Older row was capped...
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "webhook", "capped": True},
    )
    # ... but the latest row is fine. _close_orchestration_last_capped
    # walks history newest-first; this is the row it must see.
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "rescan", "capped": False},
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Rescan all (override cap)" not in resp.text
