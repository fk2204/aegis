"""POST /ui/funders/{funder_id}/operator-status — operator-status save route.

Verifies:
  * happy save updates operator_status + writes audit row
  * no-change save does NOT write audit row but still re-renders chip
  * 400 on invalid status value
  * 404 on unknown funder
  * all 4 valid statuses round-trip
  * detail page renders the chip + dropdown inline
  * funders list renders the chip column + Hide-paused toggle
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_note_submission_repository,
    get_funder_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository


@pytest.fixture
def funder() -> FunderRow:
    return FunderRow(name="Status Capital", active=True)


@pytest.fixture
def funder_repo(funder: FunderRow) -> InMemoryFunderRepository:
    repo = InMemoryFunderRepository()
    repo.upsert(funder)
    return repo


@pytest.fixture
def audit_log() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def submissions_repo() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def client(
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_audit] = lambda: audit_log
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: submissions_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_save_updates_operator_status_and_writes_audit(
    client: TestClient,
    funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> None:
    resp = client.post(
        f"/ui/funders/{funder.id}/operator-status",
        data={"operator_status": "paused"},
    )
    assert resp.status_code == 200, resp.text
    assert "Paused" in resp.text
    assert "✓ Updated" in resp.text

    after = funder_repo.get(funder.id)
    assert after.operator_status == "paused"

    rows = [e for e in audit_log.entries if e["action"] == "funder.operator_status_updated"]
    assert len(rows) == 1
    d = rows[0]["details"]
    assert d["funder_name"] == "Status Capital"
    assert d["before"] == "active"
    assert d["after"] == "paused"


def test_no_change_save_does_not_write_audit_row(
    client: TestClient,
    funder: FunderRow,
    audit_log: InMemoryAuditLog,
) -> None:
    resp = client.post(
        f"/ui/funders/{funder.id}/operator-status",
        data={"operator_status": "active"},  # already active
    )
    assert resp.status_code == 200
    # Chip still re-renders with the active state.
    assert "Active" in resp.text
    rows = [e for e in audit_log.entries if e["action"] == "funder.operator_status_updated"]
    assert rows == []


def test_invalid_status_rejected_with_400(
    client: TestClient,
    funder: FunderRow,
) -> None:
    resp = client.post(
        f"/ui/funders/{funder.id}/operator-status",
        data={"operator_status": "not-a-real-status"},
    )
    assert resp.status_code == 400
    assert "operator_status" in resp.text.lower()


def test_unknown_funder_returns_404(client: TestClient) -> None:
    resp = client.post(
        f"/ui/funders/{uuid4()}/operator-status",
        data={"operator_status": "paused"},
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "value",
    ["active", "paused", "first_position_only", "selective"],
)
def test_all_four_values_round_trip(
    client: TestClient,
    funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
    value: str,
) -> None:
    resp = client.post(
        f"/ui/funders/{funder.id}/operator-status",
        data={"operator_status": value},
    )
    assert resp.status_code == 200, resp.text
    assert funder_repo.get(funder.id).operator_status == value


def test_normalizes_whitespace_and_case(
    client: TestClient,
    funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    resp = client.post(
        f"/ui/funders/{funder.id}/operator-status",
        data={"operator_status": "  PAUSED  "},
    )
    assert resp.status_code == 200
    assert funder_repo.get(funder.id).operator_status == "paused"


def test_detail_page_renders_status_chip_and_dropdown(
    client: TestClient,
    funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    funder_repo.set_operator_status(funder.id, "selective")
    resp = client.get(f"/ui/funders/{funder.id}")
    assert resp.status_code == 200
    assert 'data-test-id="funder-operator-status"' in resp.text
    assert 'data-test-id="op-status-select"' in resp.text
    # Selected state matches the persisted value.
    assert 'value="selective"' in resp.text
    assert "Selective" in resp.text


def test_funders_list_renders_op_status_chip_and_hide_paused_toggle(
    client: TestClient,
    funder_repo: InMemoryFunderRepository,
) -> None:
    a = FunderRow(name="Alpha Co", active=True)
    b = FunderRow(name="Bravo Co", active=True)
    funder_repo.upsert(a)
    funder_repo.upsert(b)
    funder_repo.set_operator_status(a.id, "paused")
    funder_repo.set_operator_status(b.id, "active")

    resp = client.get("/ui/funders")
    assert resp.status_code == 200

    # Hide-paused toggle is rendered (checked by default).
    assert 'data-test-id="funders-hide-paused"' in resp.text
    # Both funder rows are rendered (filter is client-side JS — server
    # always returns the full set).
    assert 'data-op-status="paused"' in resp.text
    assert 'data-op-status="active"' in resp.text
    # Chip column is rendered.
    assert 'data-test-id="funders-op-status-chip"' in resp.text
