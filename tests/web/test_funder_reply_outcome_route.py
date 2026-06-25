"""Tests for the manual funder-reply-outcome capture routes.

* ``GET /ui/funder-replies/outcome-modal`` — returns the HTMX modal
  partial with the funder name + identity fields pre-populated.
* ``POST /ui/funder-replies/outcome`` — persists a funder_replies row
  with the new outcome columns (migration 071), writes the
  ``funder_reply.outcome_recorded`` audit row, and returns the
  refreshed submission row HTML for the HTMX swap.
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
    get_funder_reply_repository,
    get_funder_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.funder_note_submissions.models import FunderNoteSubmissionRow
from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.replies import InMemoryFunderReplyRepository
from aegis.funders.repository import InMemoryFunderRepository
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def subs_repo() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def reply_repo() -> InMemoryFunderReplyRepository:
    return InMemoryFunderReplyRepository()


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def funder(funder_repo: InMemoryFunderRepository) -> FunderRow:
    row = FunderRow(
        id=uuid4(),
        name="Acme Capital",
        active=True,
    )
    funder_repo.upsert(row)
    return row


@pytest.fixture
def submission(
    subs_repo: InMemoryFunderNoteSubmissionRepository,
    funder: FunderRow,
) -> FunderNoteSubmissionRow:
    return subs_repo.create(
        merchant_id=uuid4(),
        funder_id=funder.id,
        funder_note="initial submission text",
        submitted_by="dashboard",
    )


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    subs_repo: InMemoryFunderNoteSubmissionRepository,
    reply_repo: InMemoryFunderReplyRepository,
    funder_repo: InMemoryFunderRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: subs_repo
    app.dependency_overrides[get_funder_reply_repository] = lambda: reply_repo
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# GET modal endpoint
# ---------------------------------------------------------------------------


def test_modal_endpoint_returns_form_with_funder_name(
    client: TestClient,
    submission: FunderNoteSubmissionRow,
    funder: FunderRow,
) -> None:
    """GET /ui/funder-replies/outcome-modal returns 200 + the form
    pre-populated with the funder name and identity hidden fields."""
    resp = client.get(
        "/ui/funder-replies/outcome-modal",
        params={
            "merchant_id": str(submission.merchant_id),
            "funder_id": str(funder.id),
            "submission_id": str(submission.id),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Acme Capital" in body
    # Hidden identity fields
    assert f'value="{submission.merchant_id}"' in body
    assert f'value="{funder.id}"' in body
    assert f'value="{submission.id}"' in body
    # Form anchors
    assert 'data-test-id="record-outcome-form"' in body
    assert 'data-test-id="outcome-select"' in body
    # All four outcomes available
    for outcome in ("approved", "declined", "countered", "no_response"):
        assert f'value="{outcome}"' in body


# ---------------------------------------------------------------------------
# POST outcome — happy paths
# ---------------------------------------------------------------------------


def test_post_outcome_approved_persists_row_and_writes_audit(
    client: TestClient,
    submission: FunderNoteSubmissionRow,
    funder: FunderRow,
    reply_repo: InMemoryFunderReplyRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Operator records approved outcome with the full offer triple;
    the funder_replies row carries every field with the right type and
    the audit row lands with operator email + identity."""
    resp = client.post(
        "/ui/funder-replies/outcome",
        data={
            "merchant_id": str(submission.merchant_id),
            "funder_id": str(funder.id),
            "submission_id": str(submission.id),
            "outcome": "approved",
            "outcome_amount": "50000.00",
            "outcome_factor_rate": "1.3000",
            "outcome_term_days": "120",
            "outcome_notes": "Approved over phone",
        },
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200, resp.text

    # funder_replies row landed with the right field shapes.
    rows = reply_repo.replies()
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "approved"
    assert row["status"] == "approved"  # mirrored for compat
    assert row["outcome_amount"] == Decimal("50000.00")
    assert isinstance(row["outcome_amount"], Decimal)
    assert row["outcome_factor_rate"] == Decimal("1.3000")
    assert row["outcome_term_days"] == 120
    assert row["outcome_notes"] == "Approved over phone"
    assert row["outcome_recorded_by"] == "filip@commerafunding.com"
    assert row["outcome_recorded_at"] is not None

    # Audit row landed with the right shape.
    matching = [e for e in audit.entries if e["action"] == "funder_reply.outcome_recorded"]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["actor_email"] == "filip@commerafunding.com"
    assert entry["subject_type"] == "funder_note_submission"
    assert entry["subject_id"] == str(submission.id)
    assert entry["details"]["outcome"] == "approved"
    assert entry["details"]["funder_id"] == str(funder.id)

    # Response body is the submission-row partial swap.
    assert 'data-test-id="submission-row"' in resp.text
    assert f'data-submission-id="{submission.id}"' in resp.text


def test_post_outcome_no_response_omits_offer_fields(
    client: TestClient,
    submission: FunderNoteSubmissionRow,
    funder: FunderRow,
    reply_repo: InMemoryFunderReplyRepository,
) -> None:
    """no_response — funder didn't reply. Even if the operator's POST
    accidentally includes offer fields (stale JS), they're dropped."""
    resp = client.post(
        "/ui/funder-replies/outcome",
        data={
            "merchant_id": str(submission.merchant_id),
            "funder_id": str(funder.id),
            "submission_id": str(submission.id),
            "outcome": "no_response",
            # The form JS clears these on outcome change, but a hand-
            # crafted POST might not. The route drops them before
            # the FunderReplyOutcomePayload Pydantic validator runs.
            "outcome_amount": "1000.00",
            "outcome_factor_rate": "1.500",
            "outcome_term_days": "60",
            "outcome_notes": "Funder ghosted after 14 days",
        },
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200, resp.text

    row = reply_repo.replies()[0]
    assert row["outcome"] == "no_response"
    assert row["status"] is None
    # Offer fields dropped server-side.
    assert row["outcome_amount"] is None
    assert row["outcome_factor_rate"] is None
    assert row["outcome_term_days"] is None
    assert row["outcome_notes"] == "Funder ghosted after 14 days"


def test_post_outcome_falls_back_to_dashboard_actor_without_sso_header(
    client: TestClient,
    submission: FunderNoteSubmissionRow,
    funder: FunderRow,
    reply_repo: InMemoryFunderReplyRepository,
) -> None:
    """Local dev / test client without the CF-Access header records
    ``dashboard`` as the actor — must NOT 500."""
    resp = client.post(
        "/ui/funder-replies/outcome",
        data={
            "merchant_id": str(submission.merchant_id),
            "funder_id": str(funder.id),
            "submission_id": str(submission.id),
            "outcome": "declined",
        },
    )
    assert resp.status_code == 200, resp.text
    row = reply_repo.replies()[0]
    assert row["outcome_recorded_by"] == "dashboard"


# ---------------------------------------------------------------------------
# POST outcome — failure modes
# ---------------------------------------------------------------------------


def test_post_outcome_unknown_submission_returns_404(
    client: TestClient,
    funder: FunderRow,
) -> None:
    bogus = uuid4()
    resp = client.post(
        "/ui/funder-replies/outcome",
        data={
            "merchant_id": str(uuid4()),
            "funder_id": str(funder.id),
            "submission_id": str(bogus),
            "outcome": "approved",
        },
    )
    assert resp.status_code == 404


def test_post_outcome_inconsistent_merchant_or_funder_returns_400(
    client: TestClient,
    submission: FunderNoteSubmissionRow,
    funder: FunderRow,
) -> None:
    """A POST that targets a different merchant/funder than the
    submission row references is rejected (defensive — stale modal or
    hand-crafted POST)."""
    other_funder = uuid4()
    resp = client.post(
        "/ui/funder-replies/outcome",
        data={
            "merchant_id": str(submission.merchant_id),
            "funder_id": str(other_funder),  # mismatch
            "submission_id": str(submission.id),
            "outcome": "approved",
        },
    )
    assert resp.status_code == 400
    assert "inconsistent" in resp.text.lower()


def test_post_outcome_bad_dropdown_value_returns_400(
    client: TestClient,
    submission: FunderNoteSubmissionRow,
    funder: FunderRow,
) -> None:
    resp = client.post(
        "/ui/funder-replies/outcome",
        data={
            "merchant_id": str(submission.merchant_id),
            "funder_id": str(funder.id),
            "submission_id": str(submission.id),
            "outcome": "maybe-later",
        },
    )
    assert resp.status_code == 400


def test_post_outcome_bad_decimal_amount_returns_400(
    client: TestClient,
    submission: FunderNoteSubmissionRow,
    funder: FunderRow,
) -> None:
    resp = client.post(
        "/ui/funder-replies/outcome",
        data={
            "merchant_id": str(submission.merchant_id),
            "funder_id": str(funder.id),
            "submission_id": str(submission.id),
            "outcome": "approved",
            "outcome_amount": "not-a-number",
        },
    )
    assert resp.status_code == 400
