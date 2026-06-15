"""Tests for ``PATCH /ui/submissions/{submission_id}/status``.

Covers:

* Happy path — operator marks a submission ``approved`` with offer
  fields; row updates, ``submission.status_updated`` audit row lands,
  response body is the rendered partial.
* Status validation — bogus status returns 400.
* Empty optional inputs leave stored values alone.
* 404 when the submission_id is unknown.
* responded_at stamped exactly once on the first non-pending edit.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_note_submission_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.funder_note_submissions.models import FunderNoteSubmissionRow
from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def subs_repo() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    subs_repo: InMemoryFunderNoteSubmissionRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: subs_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_pending(
    repo: InMemoryFunderNoteSubmissionRepository,
) -> FunderNoteSubmissionRow:
    return repo.create(
        merchant_id=uuid4(),
        funder_id=uuid4(),
        funder_note="test note",
        submitted_by="dashboard",
    )


def test_happy_path_approved_with_offer_fields(
    client: TestClient,
    subs_repo: InMemoryFunderNoteSubmissionRepository,
    audit: InMemoryAuditLog,
) -> None:
    row = _seed_pending(subs_repo)
    resp = client.patch(
        f"/ui/submissions/{row.id}/status",
        data={
            "status": "approved",
            "offer_amount": "50000.00",
            "offer_factor": "1.300",
            "offer_holdback": "0.1500",
            "notes": "Funder approved over phone",
        },
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200, resp.text

    updated = subs_repo.get(row.id)
    assert updated.status == "approved"
    assert updated.offer_amount == Decimal("50000.00")
    assert updated.offer_factor == Decimal("1.300")
    assert updated.offer_holdback == Decimal("0.1500")
    assert updated.notes == "Funder approved over phone"
    assert updated.responded_at is not None

    # Audit row landed with operator email + char count (no PII)
    actions = [e["action"] for e in audit.entries]
    assert "submission.status_updated" in actions
    audit_row = next(e for e in audit.entries if e["action"] == "submission.status_updated")
    assert audit_row["actor_email"] == "filip@commerafunding.com"
    assert audit_row["details"]["status"] == "approved"
    # PII discipline — full notes text not in audit, only char count
    assert audit_row["details"]["notes_chars"] == len("Funder approved over phone")
    assert "Funder approved" not in str(audit_row["details"])

    # Response body is the swap partial
    assert 'data-test-id="submission-row"' in resp.text
    assert f'data-submission-id="{row.id}"' in resp.text


def test_status_must_be_valid_value(
    client: TestClient,
    subs_repo: InMemoryFunderNoteSubmissionRepository,
) -> None:
    row = _seed_pending(subs_repo)
    resp = client.patch(
        f"/ui/submissions/{row.id}/status",
        data={"status": "maybe-later"},
    )
    assert resp.status_code == 400


def test_empty_optional_inputs_preserve_stored_values(
    client: TestClient,
    subs_repo: InMemoryFunderNoteSubmissionRepository,
) -> None:
    """Pre-populate offer fields, then transition to approved with
    empty offer inputs. Stored offer values must survive — operator
    might be just bumping status without rewriting numbers."""
    row = subs_repo.create(
        merchant_id=uuid4(),
        funder_id=uuid4(),
        funder_note="x",
        submitted_by="dashboard",
    )
    subs_repo.update_status(
        row.id,
        status="pending",
        offer_amount=Decimal("75000.00"),
        offer_factor=Decimal("1.250"),
    )
    resp = client.patch(
        f"/ui/submissions/{row.id}/status",
        data={
            "status": "approved",
            "offer_amount": "",
            "offer_factor": "",
            "offer_holdback": "",
            "notes": "",
        },
    )
    assert resp.status_code == 200
    after = subs_repo.get(row.id)
    assert after.offer_amount == Decimal("75000.00")
    assert after.offer_factor == Decimal("1.250")
    assert after.status == "approved"


def test_unknown_submission_returns_404(client: TestClient) -> None:
    bogus = uuid4()
    resp = client.patch(
        f"/ui/submissions/{bogus}/status",
        data={"status": "approved"},
    )
    assert resp.status_code == 404


def test_responded_at_stamped_once_on_first_decision(
    client: TestClient,
    subs_repo: InMemoryFunderNoteSubmissionRepository,
) -> None:
    """First pending -> non-pending edit stamps responded_at; later
    non-pending edits (operator correction) leave it intact."""
    row = _seed_pending(subs_repo)
    r1 = client.patch(
        f"/ui/submissions/{row.id}/status",
        data={"status": "approved"},
    )
    assert r1.status_code == 200
    first_responded = subs_repo.get(row.id).responded_at
    assert first_responded is not None

    r2 = client.patch(
        f"/ui/submissions/{row.id}/status",
        data={"status": "countered", "notes": "operator typo correction"},
    )
    assert r2.status_code == 200
    after = subs_repo.get(row.id)
    assert after.responded_at == first_responded  # unchanged
    assert after.status == "countered"


def test_bad_decimal_input_returns_400(
    client: TestClient,
    subs_repo: InMemoryFunderNoteSubmissionRepository,
) -> None:
    row = _seed_pending(subs_repo)
    resp = client.patch(
        f"/ui/submissions/{row.id}/status",
        data={"status": "approved", "offer_amount": "not-a-number"},
    )
    assert resp.status_code == 400
