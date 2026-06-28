"""Tests for POST ``/ui/merchants/{id}/verify-ucc`` (Phase D / migration 086).

Covers:
* Admin / Underwriter can verify; Viewer gets 403 from the role gate.
* Verification flips ``ucc_operator_verified`` to True and stamps
  ``ucc_verified_at``.
* A single ``merchant.ucc_verified_manually`` audit row lands with the
  operator's email + UCC portal URL.
* The HTMX response re-renders the background-checks section partial
  and includes the "✓ Verified" chip rather than the button.
* 404 on unknown merchant.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
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
from aegis.ops.operators import Operator, OperatorRole

_CF_HEADER = "cf-access-authenticated-user-email"


@pytest.fixture
def env() -> Iterator[
    tuple[
        TestClient,
        InMemoryAuditLog,
        InMemoryMerchantRepository,
        MerchantRow,
        Operator,
        Operator,
        Operator,
    ]
]:
    reset_dependency_caches()

    audit = InMemoryAuditLog()
    merchants_repo = InMemoryMerchantRepository()
    operators = InMemoryOperatorRepository()

    admin = Operator(email="admin@aegis.test", display_name="Admin", role=OperatorRole.ADMIN)
    uw = Operator(email="uw@aegis.test", display_name="UW", role=OperatorRole.UNDERWRITER)
    viewer = Operator(email="viewer@aegis.test", display_name="Viewer", role=OperatorRole.VIEWER)
    operators._seed(admin)
    operators._seed(uw)
    operators._seed(viewer)

    # The verify chip + button live inside the populated UCC branch
    # of the dossier section — set ``ucc_checked_at`` so the template
    # renders that branch instead of the empty-state Run-now button.
    merchant = MerchantRow(
        id=uuid4(),
        business_name="Acme LLC",
        state="WY",
        ucc_checked_at=datetime.now(UTC),
        ucc_filings=["OnDeck Capital"],
    )
    merchants_repo.upsert(merchant)

    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchants_repo
    app.dependency_overrides[get_operator_repository] = lambda: operators

    with TestClient(app) as client:
        yield client, audit, merchants_repo, merchant, admin, uw, viewer

    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_admin_verify_flips_persisted_state_and_renders_chip(
    env: tuple[
        TestClient,
        InMemoryAuditLog,
        InMemoryMerchantRepository,
        MerchantRow,
        Operator,
        Operator,
        Operator,
    ],
) -> None:
    client, _audit, repo, merchant, admin, _uw, _viewer = env

    resp = client.post(
        f"/ui/merchants/{merchant.id}/verify-ucc",
        headers={_CF_HEADER: admin.email},
    )

    assert resp.status_code == 200, resp.text
    updated = repo.get(merchant.id)
    assert updated.ucc_operator_verified is True
    assert updated.ucc_verified_at is not None

    body = resp.text
    assert 'data-test-id="dossier-background-checks"' in body
    assert 'data-test-id="bg-check-ucc-verified"' in body
    # Button must NOT be present after verification.
    assert 'data-test-id="bg-check-ucc-verify"' not in body


def test_underwriter_can_verify(
    env: tuple[
        TestClient,
        InMemoryAuditLog,
        InMemoryMerchantRepository,
        MerchantRow,
        Operator,
        Operator,
        Operator,
    ],
) -> None:
    client, _audit, _repo, merchant, _admin, uw, _viewer = env
    resp = client.post(
        f"/ui/merchants/{merchant.id}/verify-ucc",
        headers={_CF_HEADER: uw.email},
    )
    assert resp.status_code == 200


def test_viewer_gets_403(
    env: tuple[
        TestClient,
        InMemoryAuditLog,
        InMemoryMerchantRepository,
        MerchantRow,
        Operator,
        Operator,
        Operator,
    ],
) -> None:
    client, _audit, _repo, merchant, _admin, _uw, viewer = env
    resp = client.post(
        f"/ui/merchants/{merchant.id}/verify-ucc",
        headers={_CF_HEADER: viewer.email},
    )
    assert resp.status_code == 403


def test_audit_row_written_with_operator_email_and_portal_url(
    env: tuple[
        TestClient,
        InMemoryAuditLog,
        InMemoryMerchantRepository,
        MerchantRow,
        Operator,
        Operator,
        Operator,
    ],
) -> None:
    client, audit, _repo, merchant, admin, _uw, _viewer = env
    resp = client.post(
        f"/ui/merchants/{merchant.id}/verify-ucc",
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 200

    actions = [e["action"] for e in audit.entries]
    assert "merchant.ucc_verified_manually" in actions
    row = next(e for e in audit.entries if e["action"] == "merchant.ucc_verified_manually")
    assert row["actor"] == f"operator:{admin.email}"
    # The operator email lives in the top-level ``actor_email`` slot
    # (PII-masker bypasses for that key) — NOT inside ``details`` where
    # the logger's keyword mask would redact it.
    assert row["actor_email"] == admin.email
    assert "verified_at" in row["details"]
    assert row["details"]["ucc_portal_url"]


def test_unknown_merchant_returns_404(
    env: tuple[
        TestClient,
        InMemoryAuditLog,
        InMemoryMerchantRepository,
        MerchantRow,
        Operator,
        Operator,
        Operator,
    ],
) -> None:
    client, _audit, _repo, _merchant, admin, _uw, _viewer = env
    resp = client.post(
        f"/ui/merchants/{uuid4()}/verify-ucc",
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 404
