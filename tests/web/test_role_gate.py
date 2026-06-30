"""Tests for the role-based permission gate.

Covers the four most operator-facing gates:

  * POST /ui/merchants/{id}/submit-to-funder/{funder_id}  (underwriter+)
  * POST /ui/merchants/{id}/documents/{doc_id}/override    (underwriter+)
  * GET  /ui/compliance/obligations                        (admin only)
  * GET  /ui/calibration                                   (admin only)

Each test exercises 200/302/303 for the allowed role and 403 for the
disallowed role. The 403 surfaces the carried HTML page (not JSON).

In-memory backend is used throughout. The Cloudflare-Access SSO email
header is forged via the ``cf-access-authenticated-user-email`` request
header so the role-gate dependency resolves a deterministic operator
without spinning up Cloudflare.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_operator_repository,
    reset_dependency_caches,
)
from aegis.ops.operator_repository import InMemoryOperatorRepository
from aegis.ops.operators import Operator, OperatorRole


@pytest.fixture
def role_client() -> Iterator[tuple[TestClient, InMemoryOperatorRepository]]:
    """TestClient with the operator repo pinned in-memory + pre-seeded
    rows for each of the three product roles.

    Tests issue requests with ``cf-access-authenticated-user-email`` set
    to one of:
      * ``admin@aegis.test``
      * ``uw@aegis.test``
      * ``viewer@aegis.test``
    """
    reset_dependency_caches()
    operators = InMemoryOperatorRepository()
    operators._seed(
        Operator(
            id=uuid4(),
            email="admin@aegis.test",
            display_name="Admin Operator",
            role=OperatorRole.ADMIN,
        )
    )
    operators._seed(
        Operator(
            id=uuid4(),
            email="uw@aegis.test",
            display_name="Underwriter Operator",
            role=OperatorRole.UNDERWRITER,
        )
    )
    operators._seed(
        Operator(
            id=uuid4(),
            email="viewer@aegis.test",
            display_name="Viewer Operator",
            role=OperatorRole.VIEWER,
        )
    )
    app = create_app()
    app.dependency_overrides[get_operator_repository] = lambda: operators
    with TestClient(app) as c:
        yield c, operators
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _as(email: str) -> dict[str, str]:
    """Forge the CF Access SSO header value."""
    return {"cf-access-authenticated-user-email": email}


# ---------------------------------------------------------------------------
# /ui/compliance/obligations — admin only
# ---------------------------------------------------------------------------


def test_compliance_obligations_blocks_viewer(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    resp = client.get(
        "/ui/compliance/obligations",
        headers=_as("viewer@aegis.test"),
    )
    assert resp.status_code == 403
    assert "Access denied" in resp.text
    assert "admin" in resp.text


def test_compliance_obligations_blocks_underwriter(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    resp = client.get(
        "/ui/compliance/obligations",
        headers=_as("uw@aegis.test"),
    )
    assert resp.status_code == 403
    assert "Access denied" in resp.text


def test_compliance_obligations_allows_admin(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    resp = client.get(
        "/ui/compliance/obligations",
        headers=_as("admin@aegis.test"),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /ui/calibration — admin only
# ---------------------------------------------------------------------------


def test_calibration_blocks_viewer(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    resp = client.get("/ui/calibration", headers=_as("viewer@aegis.test"))
    assert resp.status_code == 403
    assert "Access denied" in resp.text


def test_calibration_blocks_underwriter(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    resp = client.get("/ui/calibration", headers=_as("uw@aegis.test"))
    assert resp.status_code == 403


def test_calibration_allows_admin(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    resp = client.get("/ui/calibration", headers=_as("admin@aegis.test"))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Override route — underwriter+
# ---------------------------------------------------------------------------


def test_dossier_override_blocks_viewer(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    """Viewer 403s before the route's body validation runs.

    Posts with empty form data; the role gate fires first, so a 400
    response would mean the gate didn't catch.
    """
    client, _ = role_client
    merchant_id = uuid4()
    document_id = uuid4()
    resp = client.post(
        f"/ui/merchants/{merchant_id}/documents/{document_id}/override",
        data={},
        headers=_as("viewer@aegis.test"),
    )
    assert resp.status_code == 403
    assert "Access denied" in resp.text


def test_legacy_override_blocks_viewer(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    decision_id = uuid4()
    resp = client.post(
        f"/ui/decisions/{decision_id}/override",
        data={},
        headers=_as("viewer@aegis.test"),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Submit-to-funder route — underwriter+
# ---------------------------------------------------------------------------


def test_submit_to_funder_blocks_viewer(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    merchant_id = uuid4()
    funder_id = uuid4()
    resp = client.post(
        f"/ui/merchants/{merchant_id}/submit-to-funder/{funder_id}",
        data={},
        headers=_as("viewer@aegis.test"),
    )
    assert resp.status_code == 403


def test_submit_aggregate_blocks_viewer(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    merchant_id = uuid4()
    resp = client.post(
        f"/ui/merchants/{merchant_id}/submit",
        data={},
        headers=_as("viewer@aegis.test"),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Delete merchant — admin only
# ---------------------------------------------------------------------------


def test_delete_merchant_blocks_underwriter(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    merchant_id = uuid4()
    resp = client.post(
        f"/ui/merchants/{merchant_id}/delete",
        data={},
        headers=_as("uw@aegis.test"),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Local-dev fallback — no SSO header → synthetic admin
# ---------------------------------------------------------------------------


def test_no_cf_header_falls_back_to_admin(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    """Without the CF Access header the gate synthesizes a local-dev
    admin so the workstation pytest run can exercise admin-gated routes."""
    client, _ = role_client
    resp = client.get("/ui/compliance/obligations")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Topstrip role chip — render verification
# ---------------------------------------------------------------------------


def test_topstrip_renders_role_chip_for_admin(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    resp = client.get("/ui/", headers=_as("admin@aegis.test"))
    assert resp.status_code == 200
    body = resp.text
    # Display name now lives in the avatar tooltip (`title` attr) rather
    # than as visible text. The role-suffix class is still the role hook.
    assert "Admin Operator" in body
    assert "op-avatar--admin" in body


def test_topstrip_renders_role_chip_for_viewer(
    role_client: tuple[TestClient, InMemoryOperatorRepository],
) -> None:
    client, _ = role_client
    resp = client.get("/ui/", headers=_as("viewer@aegis.test"))
    assert resp.status_code == 200
    body = resp.text
    assert "Viewer Operator" in body
    assert "op-avatar--viewer" in body
