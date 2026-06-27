"""Router tests for the migration-065 merchant soft-delete flow.

Covers ``POST /ui/merchants/{merchant_id}/delete``:

* Happy path — repo state flips, audit row landed, 303 redirect to list.
* The dossier 404s after delete (read filter wired through).
* The merchants list excludes the deleted row (read filter wired through).
* Unknown merchant id → 404.
* Already-deleted merchant id → 404 (the repo's double-delete guard
  surfaces through the route).
* Dossier renders a delete form on the action toolbar (header wiring
  proof so the button can't silently disappear from a future Wave-2
  refactor of the dossier).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_operator_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.operator_repository import InMemoryOperatorRepository
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER, Operator, OperatorRole


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchant_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def operator_repo() -> InMemoryOperatorRepository:
    """Pre-seed ``filip@commerafunding.com`` as admin so the soft-delete
    route (admin-gated) accepts the test request.
    """
    repo = InMemoryOperatorRepository()
    repo._seed(
        Operator(
            email="filip@commerafunding.com",
            display_name="Filip",
            role=OperatorRole.ADMIN,
        )
    )
    return repo


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    merchant_repo: InMemoryMerchantRepository,
    operator_repo: InMemoryOperatorRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_operator_repository] = lambda: operator_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    business_name: str = "Deletable LLC",
) -> MerchantRow:
    m = MerchantRow(
        id=uuid4(),
        business_name=business_name,
        owner_name="Operator Owner",
        state="CA",
    )
    repo.upsert(m)
    return m


def test_delete_happy_path_flips_repo_and_writes_audit(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo, business_name="ToDelete Co")

    resp = client.post(
        f"/ui/merchants/{m.id}/delete",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/merchants"

    # Repo state: row still present internally (history preserved),
    # but ``get`` now 404s and ``list_all`` skips it.
    raw = merchant_repo._by_id[m.id]
    assert raw.deleted_at is not None
    assert isinstance(raw.deleted_at, datetime)
    assert raw.deleted_at.tzinfo is not None

    # Audit row: exactly one ``merchant.deleted`` entry. The audit
    # layer masks PII keys (``business_name`` is one of them; see
    # ``aegis.logger._PII_KEYS``) so the on-disk value is ``***``.
    # That's the correct posture — we still verify the KEY landed so
    # an audit reader knows the field was captured even though the
    # value is masked.
    deletes = [e for e in audit.entries if e["action"] == "merchant.deleted"]
    assert len(deletes) == 1
    row = deletes[0]
    assert row["actor"] == "operator"
    assert row["actor_email"] == "filip@commerafunding.com"
    assert row["subject_type"] == "merchant"
    assert row["subject_id"] == str(m.id)
    assert "business_name" in row["details"]
    assert row["details"]["business_name"] == "***"


def test_dossier_404s_after_delete(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    m = _seed_merchant(merchant_repo)
    client.post(f"/ui/merchants/{m.id}/delete", follow_redirects=False)

    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 404


def test_merchants_list_excludes_deleted(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    keep = _seed_merchant(merchant_repo, business_name="Active Co")
    drop = _seed_merchant(merchant_repo, business_name="Tombstone Co")

    client.post(f"/ui/merchants/{drop.id}/delete", follow_redirects=False)

    resp = client.get("/ui/merchants")
    assert resp.status_code == 200
    assert "Active Co" in resp.text
    assert "Tombstone Co" not in resp.text
    # Confirm the kept merchant's id (UUID format truncated to 8 chars
    # in the template — match on the row href instead which is exact).
    assert f"/ui/merchants/{keep.id}" in resp.text


def test_delete_404_on_unknown_merchant(client: TestClient) -> None:
    bogus = uuid4()
    resp = client.post(
        f"/ui/merchants/{bogus}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_delete_404_on_already_deleted_merchant(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo)

    first = client.post(f"/ui/merchants/{m.id}/delete", follow_redirects=False)
    assert first.status_code == 303

    second = client.post(f"/ui/merchants/{m.id}/delete", follow_redirects=False)
    assert second.status_code == 404

    # Audit log carries exactly one merchant.deleted row — the double-
    # submit was rejected before the audit write.
    deletes = [e for e in audit.entries if e["action"] == "merchant.deleted"]
    assert len(deletes) == 1


def test_dossier_renders_delete_form_on_header_toolbar(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """Regression guard — the Delete button must remain wired on the
    dossier's top action toolbar. A future template refactor that drops
    the button would silently break the delete flow; the test fails
    fast on either disappearance OR a route-change."""
    m = _seed_merchant(merchant_repo)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text
    assert 'data-test-id="merchant-delete-form"' in html
    assert 'data-test-id="merchant-delete-button"' in html
    assert f'action="/ui/merchants/{m.id}/delete"' in html
    # Native confirm() — the codebase's de-facto destructive-action
    # pattern on plain form posts.
    assert "onsubmit=" in html
    assert "confirm(" in html
