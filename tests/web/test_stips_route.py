"""Tests for the ``/ui/merchants/{id}/stips`` router (migration 104 / P3).

Covers:

  * ``GET /ui/merchants/{id}/stips`` — 200 + section header + empty state.
  * ``POST /ui/merchants/{id}/stips`` — creates a stip via form-encoded
    body, returns the refreshed section HTML.
  * ``PATCH /ui/merchants/{id}/stips/{stip_id}`` — outstanding →
    received status flip; audit row written.
  * ``PATCH`` — invalid status returns 400.
  * ``DELETE /ui/merchants/{id}/stips/{stip_id}`` — removes the row.
  * "All stips received" audit row fires when the last outstanding
    stip is marked received.
  * ``GET /ui/merchants/{id}/stips/add-form`` — returns the add-form
    partial with the STIP_TEMPLATES dropdown.

Uses ``InMemoryStipRepository`` via FastAPI's ``dependency_overrides``
so the tests exercise the router surface end-to-end without touching
Supabase. Follows the same TestClient pattern as
``tests/web/test_admin_health_route.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import get_audit, reset_dependency_caches
from aegis.audit import InMemoryAuditLog
from aegis.stips import InMemoryStipRepository
from aegis.web.routers.stips import get_stip_repository

_MERCHANT_ID = UUID("12345678-1234-5678-1234-567812345678")


@pytest.fixture
def stips_client() -> Iterator[tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog]]:
    """TestClient with the stips repository + audit log pinned in-memory."""
    reset_dependency_caches()
    app = create_app()

    repo = InMemoryStipRepository()
    audit = InMemoryAuditLog()

    app.dependency_overrides[get_stip_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit

    with TestClient(app) as client:
        yield client, repo, audit

    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_get_stips_empty_returns_200_and_section_header(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, _repo, _audit = stips_client
    r = client.get(f"/ui/merchants/{_MERCHANT_ID}/stips")
    assert r.status_code == 200
    body = r.text
    assert "Stipulations" in body
    assert "stips-badge" in body
    assert "All clear" in body


def test_get_add_form_returns_templates_dropdown(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, _repo, _audit = stips_client
    r = client.get(f"/ui/merchants/{_MERCHANT_ID}/stips/add-form")
    assert r.status_code == 200
    body = r.text
    assert "Voided check" in body
    assert "Signed ISO agreement" in body
    assert 'name="stip_type"' in body
    assert 'name="description"' in body


def test_post_creates_stip_and_returns_refreshed_section(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, repo, audit = stips_client
    r = client.post(
        f"/ui/merchants/{_MERCHANT_ID}/stips",
        data={"stip_type": "document", "description": "Voided check"},
    )
    assert r.status_code == 200
    assert "Voided check" in r.text
    assert "1 outstanding" in r.text

    rows = repo.list_for_merchant(_MERCHANT_ID)
    assert len(rows) == 1
    assert rows[0].description == "Voided check"
    assert rows[0].stip_type == "document"
    assert rows[0].status == "outstanding"

    actions = [e["action"] for e in audit.entries]
    assert "stip.created" in actions


def test_post_rejects_invalid_stip_type(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, _repo, _audit = stips_client
    r = client.post(
        f"/ui/merchants/{_MERCHANT_ID}/stips",
        data={"stip_type": "bogus", "description": "x"},
    )
    assert r.status_code == 400


def test_post_rejects_empty_description(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, _repo, _audit = stips_client
    r = client.post(
        f"/ui/merchants/{_MERCHANT_ID}/stips",
        data={"stip_type": "document", "description": "   "},
    )
    assert r.status_code == 400


def test_patch_marks_stip_received(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, repo, audit = stips_client
    row = repo.create(
        merchant_id=_MERCHANT_ID,
        stip_type="document",
        description="Voided check",
    )

    r = client.patch(
        f"/ui/merchants/{_MERCHANT_ID}/stips/{row.id}",
        data={"status": "received"},
    )
    assert r.status_code == 200

    refreshed = repo.get(_MERCHANT_ID, row.id)
    assert refreshed.status == "received"
    assert refreshed.received_at is not None

    actions = [e["action"] for e in audit.entries]
    assert "stip.status_changed" in actions
    # Only outstanding stip cleared -> all_stips_received also fires.
    assert "merchant.all_stips_received" in actions


def test_patch_all_stips_received_only_when_zero_remaining(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, repo, audit = stips_client
    row1 = repo.create(
        merchant_id=_MERCHANT_ID,
        stip_type="document",
        description="Voided check",
    )
    repo.create(
        merchant_id=_MERCHANT_ID,
        stip_type="document",
        description="Bank statements",
    )

    # First mark leaves one outstanding — no notification.
    r = client.patch(
        f"/ui/merchants/{_MERCHANT_ID}/stips/{row1.id}",
        data={"status": "received"},
    )
    assert r.status_code == 200
    actions = [e["action"] for e in audit.entries]
    assert "merchant.all_stips_received" not in actions


def test_patch_rejects_invalid_status(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, repo, _audit = stips_client
    row = repo.create(
        merchant_id=_MERCHANT_ID,
        stip_type="document",
        description="Voided check",
    )

    r = client.patch(
        f"/ui/merchants/{_MERCHANT_ID}/stips/{row.id}",
        data={"status": "not_a_status"},
    )
    assert r.status_code == 400


def test_patch_returns_404_for_missing_stip(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, _repo, _audit = stips_client
    r = client.patch(
        f"/ui/merchants/{_MERCHANT_ID}/stips/{uuid4()}",
        data={"status": "received"},
    )
    assert r.status_code == 404


def test_delete_removes_stip(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, repo, audit = stips_client
    row = repo.create(
        merchant_id=_MERCHANT_ID,
        stip_type="document",
        description="Voided check",
    )
    assert len(repo.list_for_merchant(_MERCHANT_ID)) == 1

    r = client.delete(f"/ui/merchants/{_MERCHANT_ID}/stips/{row.id}")
    assert r.status_code == 200
    assert len(repo.list_for_merchant(_MERCHANT_ID)) == 0

    actions = [e["action"] for e in audit.entries]
    assert "stip.deleted" in actions


def test_delete_returns_404_for_missing_stip(
    stips_client: tuple[TestClient, InMemoryStipRepository, InMemoryAuditLog],
) -> None:
    client, _repo, _audit = stips_client
    r = client.delete(f"/ui/merchants/{_MERCHANT_ID}/stips/{uuid4()}")
    assert r.status_code == 404
