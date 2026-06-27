"""Tests for POST ``/ui/bank-coverage/{bank_name}/reparse-manual-review``.

Covers:

* Admin POST → 200, returns the swap-target span with enqueued count.
* Underwriter POST → 200.
* Viewer POST → 403 (role gate from ``aegis.web._role_gate``).
* Empty candidate list (no manual_review docs for the bank) → 200 +
  "No manual_review candidates" body.
* ``bank_layouts.reparse_operator_triggered`` audit row lands FIRST
  (before the enqueue helper runs) so the operator action is durable
  even if the enqueue throws.

The endpoint's pool resolution reads from ``request.app.state.arq_pool``;
none is configured in the test app so ``enqueue_bank_reparse``
short-circuits to 0 with a clean audit trail — that's the verified
contract the test asserts on, NOT a bug.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_operator_repository,
    get_pdf_store_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.bank_layouts import reparse as reparse_mod
from aegis.ops.operator_repository import InMemoryOperatorRepository
from aegis.ops.operators import Operator, OperatorRole
from aegis.pdf_store.repository import InMemoryPdfStoreRepository

_CF_HEADER = "cf-access-authenticated-user-email"


@pytest.fixture
def env() -> Iterator[tuple[TestClient, InMemoryAuditLog, Operator, Operator, Operator]]:
    """TestClient with admin / uw / viewer pre-seeded + in-memory deps."""
    reset_dependency_caches()

    audit = InMemoryAuditLog()
    operators = InMemoryOperatorRepository()
    pdf_store = InMemoryPdfStoreRepository()

    admin = Operator(
        email="admin@aegis.test",
        display_name="Admin",
        role=OperatorRole.ADMIN,
    )
    uw = Operator(
        email="uw@aegis.test",
        display_name="UW",
        role=OperatorRole.UNDERWRITER,
    )
    viewer = Operator(
        email="viewer@aegis.test",
        display_name="Viewer",
        role=OperatorRole.VIEWER,
    )
    operators._seed(admin)
    operators._seed(uw)
    operators._seed(viewer)

    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_operator_repository] = lambda: operators
    app.dependency_overrides[get_pdf_store_repository] = lambda: pdf_store

    with TestClient(app) as client:
        yield client, audit, admin, uw, viewer

    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_admin_post_returns_200_and_no_candidates_body(
    env: tuple[TestClient, InMemoryAuditLog, Operator, Operator, Operator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin can POST. No candidates → "No manual_review candidates" body."""
    client, _audit, admin, _uw, _viewer = env
    # No candidates for the test bank.
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: [],
    )

    resp = client.post(
        "/ui/bank-coverage/Chase/reparse-manual-review",
        headers={_CF_HEADER: admin.email},
    )

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "No manual_review candidates" in body
    assert 'data-test-id="reparse-result"' in body
    assert 'data-bank="Chase"' in body
    assert 'data-enqueued="0"' in body


def test_underwriter_post_also_allowed(
    env: tuple[TestClient, InMemoryAuditLog, Operator, Operator, Operator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Underwriter is in the role-gate allowlist."""
    client, _audit, _admin, uw, _viewer = env
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: [],
    )

    resp = client.post(
        "/ui/bank-coverage/Chase/reparse-manual-review",
        headers={_CF_HEADER: uw.email},
    )
    assert resp.status_code == 200


def test_viewer_post_returns_403(
    env: tuple[TestClient, InMemoryAuditLog, Operator, Operator, Operator],
) -> None:
    """Viewer role cannot trigger reparse — non-trivial Bedrock cost."""
    client, _audit, _admin, _uw, viewer = env

    resp = client.post(
        "/ui/bank-coverage/Chase/reparse-manual-review",
        headers={_CF_HEADER: viewer.email},
    )

    assert resp.status_code == 403


def test_operator_triggered_audit_row_lands_before_enqueue(
    env: tuple[TestClient, InMemoryAuditLog, Operator, Operator, Operator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bank_layouts.reparse_operator_triggered`` is the FIRST audit
    row — written before the helper runs so operator intent survives
    a mid-batch exception."""
    client, audit, admin, _uw, _viewer = env
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: [],
    )

    resp = client.post(
        "/ui/bank-coverage/Chase/reparse-manual-review",
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 200

    actions = [e["action"] for e in audit.entries]
    # The operator-triggered row is written BEFORE the helper runs.
    # The helper itself short-circuits to 0 with no audit row when
    # ``request.app.state.arq_pool`` is unset (test default), so
    # ``batch_complete`` only appears once the FastAPI app wires the
    # pool at startup — out of scope for this unit test.
    assert actions[0] == "bank_layouts.reparse_operator_triggered"
    assert audit.entries[0]["details"]["bank_layout_name"] == "Chase"
    assert audit.entries[0]["actor"] == f"operator:{admin.email}"
    assert audit.entries[0]["actor_email"] == admin.email
