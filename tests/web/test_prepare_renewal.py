"""POST /ui/merchants/{merchant_id}/prepare-renewal route tests (Sprint 7B).

Covers:
  * Renewal note carries the RENEWAL header when a prior approved
    funder_note_submission exists; the header reflects that submission's
    ``submitted_at`` date + ``offer_amount`` dollar figure.
  * Scoring reruns on the latest statements — the audit row carries the
    fresh score / tier (proxy for "score_deal ran on the rebuilt input").
  * Note posts to Close — ``post_note`` called once with the merchant's
    ``close_lead_id`` and the rendered note body; the audit row's
    ``close_note_id`` matches the stub's returned id.
  * No prior approved submission — route still 200s, note has NO
    RENEWAL header, ``original_funding_date`` + ``original_amount`` are
    None on the audit row.
  * Missing ``close_lead_id`` returns 400.
  * HTMX swap response is ``text/html``, contains "Renewal package
    ready", and carries the ``renewal-row-{merchant_id}`` id so the
    outerHTML swap targets correctly.
  * Renewal recorded as a new ``funder_note_submissions`` row framed
    against the top matched funder.

Mirrors the in-memory pattern from ``test_submit_to_funder_route.py``
verbatim: force-set env vars in ``conftest.py``, in-memory repos,
``CloseClient`` driven by an ``httpx.MockTransport`` that captures
post_note calls and returns a fake activity id.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.config import get_settings
from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
from tests.test_storage import _make_pipeline_result

_CLOSE_NOTE_ID = "acti_renewal_test_456"


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def funder_note_subs() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def close_post_calls() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def close_client(
    monkeypatch: pytest.MonkeyPatch,
    close_post_calls: list[dict[str, Any]],
) -> CloseClient:
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    def transport(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "activity/note" in request.url.path:
            close_post_calls.append(
                {
                    "url": str(request.url),
                    "body": request.content.decode("utf-8"),
                }
            )
            return httpx.Response(
                200,
                json={"id": _CLOSE_NOTE_ID, "_type": "Note"},
            )
        return httpx.Response(405)

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: funder_note_subs
    app.dependency_overrides[get_close_client] = lambda: close_client
    app.dependency_overrides[get_ofac_client] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_analyzed_merchant(
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    *,
    close_lead_id: str | None = "lead_abc",
    status: str = "finalized",
    is_renewal: bool = True,
) -> MerchantRow:
    merchant = MerchantRow(
        business_name="Acme Renewal LLC",
        owner_name="Renee Owner",
        state="CA",
        close_lead_id=close_lead_id,
        status=status,
        is_renewal=is_renewal,
    )
    merchants.upsert(merchant)
    doc = docs.create_document(
        file_hash=uuid4().hex + uuid4().hex,
        byte_size=1024,
        original_filename="stmt.pdf",
    )
    doc = doc.model_copy(update={"merchant_id": merchant.id})
    docs._docs[doc.id] = doc
    docs.persist_parse_result(doc.id, result=_make_pipeline_result(), merchant_id=merchant.id)
    return merchant


def _seed_matching_funder(funder_repo: InMemoryFunderRepository) -> FunderRow:
    """Broad funder so the in-memory matcher accepts the seeded merchant
    (CA, low monthly-revenue floor, no excluded industries)."""
    funder = FunderRow(
        name="Wide Net Capital",
        min_monthly_revenue=Decimal("1000"),
        max_positions=10,
        active=True,
    )
    funder_repo.upsert(funder)
    return funder


def _seed_approved_submission(
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    merchant: MerchantRow,
    funder: FunderRow,
    *,
    submitted_at: datetime,
    offer_amount: Decimal,
) -> None:
    """Create a prior approved submission so the renewal route reads it
    as the "original funding" anchor.

    Two-step: ``create`` writes a pending row, ``update_status`` flips it
    to approved + stamps offer_amount. Mirrors the production flow on
    the funder-response capture surface (the row is born pending, the
    operator promotes it to approved with the funded amount).
    """
    row = funder_note_subs.create(
        merchant_id=merchant.id,
        funder_id=funder.id,
        funder_note="(original funding note)",
        submitted_by="dashboard",
    )
    # Backdate submitted_at so the renewal header references a date
    # different from now() — makes the assertion meaningful.
    funder_note_subs._by_id[row.id] = row.model_copy(update={"submitted_at": submitted_at})
    funder_note_subs.update_status(
        row.id,
        status="approved",
        offer_amount=offer_amount,
    )


def test_prepare_renewal_header_includes_original_funding_when_prior_approval_exists(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo)

    original_dt = datetime(2025, 12, 1, 9, 30, tzinfo=UTC)
    original_amt = Decimal("50000.00")
    _seed_approved_submission(
        funder_note_subs,
        merchant,
        funder,
        submitted_at=original_dt,
        offer_amount=original_amt,
    )

    resp = client.post(f"/ui/merchants/{merchant.id}/prepare-renewal")
    assert resp.status_code == 200, resp.text

    assert len(close_post_calls) == 1
    body = close_post_calls[0]["body"]
    # The Close API body is a JSON envelope; the note text lives in the
    # ``note`` field. We assert against the raw JSON string — it carries
    # the exact rendered note.
    assert "RENEWAL — Previously funded 2025-12-01 for $50,000" in body
    assert "months since original funding" in body


def test_prepare_renewal_reruns_scoring_and_audit_carries_fresh_score(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
) -> None:
    """The route must re-run scoring on the merchant's latest statements;
    we assert via the downstream effect — the audit row carries a
    non-None ``score`` + ``tier`` derived from ``score_deal``. The
    in-memory pipeline result yields a deterministic tier; the test
    pins shape (presence + types) rather than the exact value, which
    keeps it stable across scoring-rules changes."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    _seed_matching_funder(funder_repo)

    resp = client.post(f"/ui/merchants/{merchant.id}/prepare-renewal")
    assert resp.status_code == 200, resp.text

    prepared = [e for e in audit.entries if e["action"] == "deal.renewal_prepared"]
    assert len(prepared) == 1
    details = prepared[0]["details"]
    assert isinstance(details["score"], int)
    assert isinstance(details["tier"], str)
    assert details["tier"]  # non-empty tier proves score_deal ran
    # The renewal flag is the cross-session marker the operator greps for.
    assert details["is_renewal"] is True


def test_prepare_renewal_posts_to_close_and_audit_records_close_note_id(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs)
    _seed_matching_funder(funder_repo)

    resp = client.post(f"/ui/merchants/{merchant.id}/prepare-renewal")
    assert resp.status_code == 200, resp.text

    assert len(close_post_calls) == 1
    # Confirm the Close POST hit the merchant's lead id.
    assert merchant.close_lead_id is not None
    assert merchant.close_lead_id in close_post_calls[0]["body"]

    prepared = [e for e in audit.entries if e["action"] == "deal.renewal_prepared"]
    assert len(prepared) == 1
    details = prepared[0]["details"]
    assert details["close_note_id"] == _CLOSE_NOTE_ID
    assert details["close_lead_id"] == merchant.close_lead_id


def test_prepare_renewal_without_prior_approval_omits_header(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    """No prior approved submission ⇒ no RENEWAL header on the note,
    no original_funding_date / original_amount on the audit row. The
    route still 200s and still flags ``is_renewal=True`` so the
    operator can grep history."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo)

    # Seed ONE pending-only submission so list_for_merchant is non-empty
    # but no approved row exists. The route must NOT use it as the
    # original funding anchor.
    funder_note_subs.create(
        merchant_id=merchant.id,
        funder_id=funder.id,
        funder_note="(pending response)",
        submitted_by="dashboard",
    )

    resp = client.post(f"/ui/merchants/{merchant.id}/prepare-renewal")
    assert resp.status_code == 200, resp.text

    assert len(close_post_calls) == 1
    body = close_post_calls[0]["body"]
    assert "RENEWAL — Previously funded" not in body

    prepared = [e for e in audit.entries if e["action"] == "deal.renewal_prepared"]
    assert len(prepared) == 1
    details = prepared[0]["details"]
    assert details["original_funding_date"] is None
    assert details["original_amount"] is None
    assert details["months_since_funding"] is None
    assert details["is_renewal"] is True


def test_prepare_renewal_400_when_no_close_lead_id(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs, close_lead_id=None)
    resp = client.post(f"/ui/merchants/{merchant.id}/prepare-renewal")
    assert resp.status_code == 400
    assert "close_lead_id" in resp.json()["detail"]
    assert close_post_calls == []
    assert funder_note_subs.list_for_merchant(merchant.id) == []


def test_prepare_renewal_htmx_swap_response_shape(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """HTMX swap target. The outerHTML response must:
    * carry the row's stable id ``renewal-row-{merchant_id}`` so the
      ``hx-target="#renewal-row-{id}"`` selector hits exactly the row
      that fired the POST;
    * include the "Renewal package ready" affirmation copy;
    * be served as ``text/html`` so HTMX swaps it without JSON parsing.
    """
    merchant = _seed_analyzed_merchant(merchants, docs)
    _seed_matching_funder(funder_repo)

    resp = client.post(f"/ui/merchants/{merchant.id}/prepare-renewal")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert f'id="renewal-row-{merchant.id}"' in body
    assert "Renewal package ready" in body


def test_prepare_renewal_creates_funder_note_submission_row(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
) -> None:
    """A successful renewal must land in the submissions table framed
    against the top matched funder, so the merchant's history shows
    the renewal alongside any prior submissions."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo)

    pre_existing = len(funder_note_subs.list_for_merchant(merchant.id))
    resp = client.post(f"/ui/merchants/{merchant.id}/prepare-renewal")
    assert resp.status_code == 200, resp.text

    rows = funder_note_subs.list_for_merchant(merchant.id)
    assert len(rows) == pre_existing + 1
    # Newest-first ordering — the new renewal row is at index 0.
    renewal_row = rows[0]
    assert renewal_row.funder_id == funder.id
    assert renewal_row.merchant_id == merchant.id
    assert renewal_row.status == "pending"
    assert renewal_row.funder_note is not None
    assert renewal_row.funder_note.strip()


def test_prepare_renewal_months_since_funding_uses_30_day_divisor(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    """Sanity-check the months-since computation. With submitted_at
    exactly 180 days ago, months_since_funding == 6."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo)

    one_eighty_days_ago = datetime.now(UTC) - timedelta(days=180)
    _seed_approved_submission(
        funder_note_subs,
        merchant,
        funder,
        submitted_at=one_eighty_days_ago,
        offer_amount=Decimal("30000.00"),
    )

    resp = client.post(f"/ui/merchants/{merchant.id}/prepare-renewal")
    assert resp.status_code == 200, resp.text

    prepared = [e for e in audit.entries if e["action"] == "deal.renewal_prepared"]
    assert prepared[0]["details"]["months_since_funding"] == 6
    body = close_post_calls[0]["body"]
    assert "6 months since original funding" in body
