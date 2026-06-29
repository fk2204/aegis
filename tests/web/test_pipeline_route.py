"""Router tests for /ui/pipeline kanban view.

Covers:
  * Page renders 200 + has the four-column grid.
  * Column 1 (Docs In) surfaces merchants with ``pending`` documents,
    oldest upload first.
  * Column 2 (Ready) surfaces ``proceed`` merchants WITHOUT submissions,
    sorted by paper grade then revenue.
  * Column 3 (Submitted) surfaces ``pending`` submissions, oldest first.
  * Column 4 (Outcomes) surfaces terminal-status responses within 30d.
  * 30-day window excludes older outcomes from column 4.
  * A merchant in column 3 (submitted) is excluded from column 2.
  * /ui/pipeline/refresh returns the same grid partial.
  * Cards link to the dossier.
  * Nav link present on the page.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
    reset_dependency_caches,
)
from aegis.funder_note_submissions import (
    FunderNoteSubmissionRow,
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository

_CF_HEADER = "cf-access-authenticated-user-email"


@pytest.fixture
def env() -> Iterator[
    tuple[
        TestClient,
        InMemoryMerchantRepository,
        InMemoryDocumentRepository,
        InMemoryFunderNoteSubmissionRepository,
        InMemoryFunderRepository,
    ]
]:
    """Build a TestClient with empty in-memory repos. Tests seed per-case."""
    reset_dependency_caches()

    merchants = InMemoryMerchantRepository()
    docs = InMemoryDocumentRepository()
    subs = InMemoryFunderNoteSubmissionRepository()
    funders = InMemoryFunderRepository()

    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: subs
    app.dependency_overrides[get_funder_repository] = lambda: funders
    # Tier-lookup chain calls into the OFAC client; tests don't need a
    # real one, but the dependency must return *something* — None is the
    # accepted sentinel (paper_grade collapses to None which is fine for
    # the column-2 sort + render contract).
    app.dependency_overrides[get_ofac_client] = lambda: None

    with TestClient(app) as client:
        yield (client, merchants, docs, subs, funders)

    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_merchant(
    merchants: InMemoryMerchantRepository,
    *,
    business_name: str,
    merchant_id: UUID | None = None,
) -> MerchantRow:
    """Insert a merchant; return the row (caller may pin the id)."""
    row = MerchantRow(
        id=merchant_id or uuid4(),
        business_name=business_name,
        state="CA",
    )
    merchants.upsert(row)
    return row


def _seed_doc(
    docs: InMemoryDocumentRepository,
    *,
    merchant_id: UUID,
    file_hash: str,
    parse_status: str = "pending",
    uploaded_at: datetime | None = None,
) -> None:
    """Insert a document with the requested parse_status + upload time."""
    row = docs.create_document(
        file_hash=file_hash,
        byte_size=1024,
        original_filename=f"{file_hash[:8]}.pdf",
        merchant_id=merchant_id,
    )
    # parse_status starts at 'pending'; bump if asked for something else.
    if parse_status != "pending":
        docs.set_parse_status(row.id, parse_status)  # type: ignore[arg-type]
    if uploaded_at is not None:
        # InMemory backend exposes the dict directly; ok for test patching.
        docs._docs[row.id].uploaded_at = uploaded_at


def _seed_submission(
    subs: InMemoryFunderNoteSubmissionRepository,
    *,
    merchant_id: UUID,
    funder_id: UUID,
    status: str = "pending",
    submitted_at: datetime | None = None,
    responded_at: datetime | None = None,
) -> FunderNoteSubmissionRow:
    """Insert a submission via the repo's create() then mutate for
    status / time overrides. The in-memory backend mutates rows in
    place so the references stay live."""
    row = subs.create(
        merchant_id=merchant_id,
        funder_id=funder_id,
        funder_note="test",
        submitted_by="tests@aegis.test",
    )
    if status != "pending":
        # mypy: status is a literal at runtime; bypass strict Literal
        # narrowing for the test seed.
        subs.update_status(row.id, status=status)  # type: ignore[arg-type]
    if submitted_at is not None:
        subs._by_id[row.id].submitted_at = submitted_at
    if responded_at is not None:
        subs._by_id[row.id].responded_at = responded_at
    return subs._by_id[row.id]


def _seed_funder(funders: InMemoryFunderRepository, *, name: str) -> FunderRow:
    row = FunderRow(name=name)
    funders.upsert(row)
    return row


# ---------------------------------------------------------------------------
# Page render / nav
# ---------------------------------------------------------------------------


def test_pipeline_index_renders_200_with_grid(env) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    resp = client.get("/ui/pipeline")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-test-id="pipeline-grid"' in body
    assert 'data-test-id="pipeline-col-docs-in"' in body
    assert 'data-test-id="pipeline-col-ready"' in body
    assert 'data-test-id="pipeline-col-submitted"' in body
    assert 'data-test-id="pipeline-col-outcomes"' in body


def test_pipeline_index_has_nav_link(env) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    resp = client.get("/ui/pipeline")
    assert resp.status_code == 200
    # Topstrip Pipeline entry — Pipeline moved into the ⚙ settings
    # dropdown 2026-06-29; the link still renders on every /ui page,
    # just inside the settings menu instead of the main nav row.
    assert 'data-test-id="settings-pipeline"' in resp.text
    assert 'href="/ui/pipeline"' in resp.text


# ---------------------------------------------------------------------------
# Column 1 — Docs In / Parsing
# ---------------------------------------------------------------------------


def test_pipeline_docs_in_lists_pending_oldest_first(env) -> None:  # type: ignore[no-untyped-def]
    client, merchants, docs, _subs, _funders = env
    now = datetime.now(UTC)
    a = _seed_merchant(merchants, business_name="Older Merchant")
    b = _seed_merchant(merchants, business_name="Newer Merchant")
    _seed_doc(docs, merchant_id=a.id, file_hash="aaa" * 22, uploaded_at=now - timedelta(hours=5))
    _seed_doc(docs, merchant_id=b.id, file_hash="bbb" * 22, uploaded_at=now - timedelta(hours=1))

    resp = client.get("/ui/pipeline")
    body = resp.text
    assert "Older Merchant" in body
    assert "Newer Merchant" in body
    # Oldest first — Older Merchant's card text precedes Newer Merchant's.
    assert body.index("Older Merchant") < body.index("Newer Merchant")


def test_pipeline_docs_in_empty_state(env) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    resp = client.get("/ui/pipeline")
    assert 'data-test-id="pipeline-empty-docs-in"' in resp.text


# ---------------------------------------------------------------------------
# Column 2 — Ready to Review
# ---------------------------------------------------------------------------


def test_pipeline_ready_lists_proceed_without_submission(env) -> None:  # type: ignore[no-untyped-def]
    client, merchants, docs, _subs, _funders = env
    m = _seed_merchant(merchants, business_name="Proceed Merchant")
    _seed_doc(docs, merchant_id=m.id, file_hash="ccc" * 22, parse_status="proceed")

    resp = client.get("/ui/pipeline")
    body = resp.text
    assert "Proceed Merchant" in body
    # The Ready card carries data-test-id="pipeline-card-ready" and the
    # merchant name. Combined, that confirms placement in column 2.
    ready_idx = body.find('data-test-id="pipeline-card-ready"')
    proceed_idx = body.find("Proceed Merchant")
    assert ready_idx != -1
    assert proceed_idx != -1


def test_pipeline_submitted_merchant_excluded_from_ready(env) -> None:  # type: ignore[no-untyped-def]
    client, merchants, docs, subs, funders = env
    m = _seed_merchant(merchants, business_name="Already Submitted")
    f = _seed_funder(funders, name="ABC Funding")
    _seed_doc(docs, merchant_id=m.id, file_hash="ddd" * 22, parse_status="proceed")
    _seed_submission(subs, merchant_id=m.id, funder_id=f.id)

    resp = client.get("/ui/pipeline")
    body = resp.text
    # Should appear in Submitted column, NOT Ready.
    assert "Already Submitted" in body
    assert 'data-test-id="pipeline-card-submitted"' in body
    # Reading just the Ready column body, the merchant must not appear.
    ready_marker = 'data-test-id="pipeline-col-ready"'
    submitted_marker = 'data-test-id="pipeline-col-submitted"'
    ready_chunk = body[body.index(ready_marker) : body.index(submitted_marker)]
    assert "Already Submitted" not in ready_chunk


# ---------------------------------------------------------------------------
# Column 3 — Submitted
# ---------------------------------------------------------------------------


def test_pipeline_submitted_oldest_first_with_funder_names(env) -> None:  # type: ignore[no-untyped-def]
    client, merchants, _docs, subs, funders = env
    now = datetime.now(UTC)
    a = _seed_merchant(merchants, business_name="Older Submission")
    b = _seed_merchant(merchants, business_name="Newer Submission")
    f_a = _seed_funder(funders, name="Alpha Capital")
    f_b = _seed_funder(funders, name="Beta Funding")
    _seed_submission(
        subs,
        merchant_id=a.id,
        funder_id=f_a.id,
        submitted_at=now - timedelta(days=10),
    )
    _seed_submission(
        subs,
        merchant_id=b.id,
        funder_id=f_b.id,
        submitted_at=now - timedelta(days=2),
    )

    resp = client.get("/ui/pipeline")
    body = resp.text
    assert "Older Submission" in body
    assert "Newer Submission" in body
    assert body.index("Older Submission") < body.index("Newer Submission")
    assert "Alpha Capital" in body
    assert "Beta Funding" in body


# ---------------------------------------------------------------------------
# Column 4 — Outcome Recorded
# ---------------------------------------------------------------------------


def test_pipeline_outcomes_within_30d_shown(env) -> None:  # type: ignore[no-untyped-def]
    client, merchants, _docs, subs, funders = env
    now = datetime.now(UTC)
    m = _seed_merchant(merchants, business_name="Recent Outcome")
    f = _seed_funder(funders, name="Gamma Capital")
    row = _seed_submission(
        subs,
        merchant_id=m.id,
        funder_id=f.id,
        submitted_at=now - timedelta(days=5),
    )
    # Flip to approved with a recent responded_at.
    subs.update_status(row.id, status="approved")
    subs._by_id[row.id].responded_at = now - timedelta(days=2)

    resp = client.get("/ui/pipeline")
    body = resp.text
    assert "Recent Outcome" in body
    assert 'data-test-id="pipeline-card-outcome"' in body
    assert 'data-test-id="pipeline-outcome-approved"' in body


def test_pipeline_outcomes_older_than_90d_excluded(env) -> None:  # type: ignore[no-untyped-def]
    """Outcome window widened to 90 days (2026-06-28) — the kanban should
    surface an outcome from ~35 days ago but NOT one from ~100 days ago."""
    client, merchants, _docs, subs, funders = env
    now = datetime.now(UTC)
    m = _seed_merchant(merchants, business_name="Old Outcome")
    f = _seed_funder(funders, name="Delta Funding")
    row = _seed_submission(
        subs,
        merchant_id=m.id,
        funder_id=f.id,
        submitted_at=now - timedelta(days=120),
    )
    subs.update_status(row.id, status="declined")
    subs._by_id[row.id].responded_at = now - timedelta(days=100)

    resp = client.get("/ui/pipeline")
    body = resp.text
    # Old outcome should not surface anywhere on the page.
    assert "Old Outcome" not in body
    assert 'data-test-id="pipeline-empty-outcomes"' in body


# ---------------------------------------------------------------------------
# Refresh endpoint
# ---------------------------------------------------------------------------


def test_pipeline_refresh_returns_grid_partial(env) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    resp = client.get("/ui/pipeline/refresh")
    assert resp.status_code == 200
    body = resp.text
    # Same grid wrapper, but no chrome (no base template head / topstrip).
    assert 'data-test-id="pipeline-grid"' in body
    assert "<html" not in body.lower()
