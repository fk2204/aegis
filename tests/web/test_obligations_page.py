"""/ui/compliance/obligations page renders without 500 (mp Phase 7 §17)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import reset_dependency_caches
from aegis.compliance import obligations as obligations_mod


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient backed by the in-memory obligations repo so we don't
    need a live Supabase. Two seeded rows give the template enough
    fixture data to render every branch."""

    today = date.today()
    seeded: list[dict[str, Any]] = [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "obligation_type": "registration",
            "state_code": "TX",
            "authority": "TX OCCC",
            "description": "Sales-based financing broker registration.",
            "deadline": (today + timedelta(days=20)).isoformat(),
            "recurrence": "annual",
            "status": "not_started",
            "next_due_date": (today + timedelta(days=20)).isoformat(),
            "notes": "Required regardless of deal size.",
        },
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "obligation_type": "license_renewal",
            "state_code": "VA",
            "authority": "VA SCC",
            "description": "Sales-based financing broker registration.",
            "deadline": None,
            "recurrence": "annual",
            "status": "submitted",
            "next_due_date": (today + timedelta(days=200)).isoformat(),
            "notes": None,
        },
    ]

    def _factory() -> obligations_mod.ObligationsRepository:
        return obligations_mod.InMemoryObligationsRepository(rows=seeded)

    monkeypatch.setattr(
        obligations_mod, "get_obligations_repository", _factory
    )

    reset_dependency_caches()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_dependency_caches()


def test_obligations_page_renders(client: TestClient) -> None:
    resp = client.get("/ui/compliance/obligations")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # State rows surface in the page.
    assert "TX OCCC" in body
    assert "VA SCC" in body
    # Summary tile labels.
    assert "Overdue" in body
    assert "Due soon" in body


def test_obligations_page_shows_due_soon_pill_for_near_deadline(
    client: TestClient,
) -> None:
    resp = client.get("/ui/compliance/obligations")
    assert resp.status_code == 200
    # The TX row is 20 days out -> due_soon pill.
    assert "DUE SOON" in resp.text


def test_obligations_page_shows_submitted_status(client: TestClient) -> None:
    resp = client.get("/ui/compliance/obligations")
    assert resp.status_code == 200
    assert "SUBMITTED" in resp.text
