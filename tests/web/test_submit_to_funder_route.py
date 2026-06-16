"""POST /ui/merchants/{merchant_id}/submit-to-funder route tests.

Covers:
  * happy path returns 200, writes one ``deal.funder_note_posted`` audit
    row, creates one ``funder_note_submissions`` row, and the audit row
    cross-references the durable row via ``funder_note_submission_id``.
  * no matched funders branch: route still returns 200 (Close Note has
    been posted) but writes ``submission_skipped_no_matches=True`` on
    the audit row and creates zero submission rows.
  * 400 when merchant has no ``close_lead_id`` (existing route invariant).
  * 400 when merchant is not finalized (existing route invariant).
"""

from __future__ import annotations

from collections.abc import Iterator
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
                json={"id": "acti_test_123", "_type": "Note"},
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
) -> MerchantRow:
    merchant = MerchantRow(
        business_name="Acme Painting LLC",
        owner_name="Jane Owner",
        state="CA",
        close_lead_id=close_lead_id,
        status=status,
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
    """A funder broad enough that the in-memory matcher accepts the seeded
    merchant (CA, low monthly revenue floor, no excluded industries)."""
    funder = FunderRow(
        name="Wide Net Capital",
        min_monthly_revenue=Decimal("1000"),
        max_positions=10,
        active=True,
    )
    funder_repo.upsert(funder)
    return funder


def test_submit_to_funder_happy_path_writes_submission_and_audit_with_cross_ref(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo)

    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder")
    assert resp.status_code == 200, resp.text
    assert "Submitted" in resp.text

    # Close Note POST happened exactly once.
    assert len(close_post_calls) == 1

    # Exactly one funder_note_submissions row, framed against the top
    # matched funder.
    rows = funder_note_subs.list_for_merchant(merchant.id)
    assert len(rows) == 1
    sub_row = rows[0]
    assert sub_row.funder_id == funder.id
    assert sub_row.merchant_id == merchant.id
    assert sub_row.status == "pending"
    assert sub_row.responded_at is None
    assert sub_row.funder_note is not None and sub_row.funder_note.strip()

    # Audit row references the durable row id.
    posted = [e for e in audit.entries if e["action"] == "deal.funder_note_posted"]
    assert len(posted) == 1
    details = posted[0]["details"]
    assert details["funder_note_submission_id"] == str(sub_row.id)
    assert "submission_skipped_no_matches" not in details
    assert details["close_activity_id"] == "acti_test_123"


def test_submit_to_funder_no_matched_funders_skips_submission_row(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    """With zero active funders, the matcher returns []. The Close Note
    still POSTs; the durable row is skipped; the audit row flags the
    branch via ``submission_skipped_no_matches=True``."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    # Intentionally NO funder seeded.

    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder")
    assert resp.status_code == 200, resp.text

    # Close Note still posted.
    assert len(close_post_calls) == 1

    # No durable submission row was created.
    assert funder_note_subs.list_for_merchant(merchant.id) == []

    posted = [e for e in audit.entries if e["action"] == "deal.funder_note_posted"]
    assert len(posted) == 1
    details = posted[0]["details"]
    assert details.get("submission_skipped_no_matches") is True
    assert "funder_note_submission_id" not in details
    assert details["matched_funder_count"] == 0


def test_submit_to_funder_400_when_merchant_not_finalized(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs, status="provisional")
    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder")
    assert resp.status_code == 400
    assert "not finalized" in resp.json()["detail"]
    assert close_post_calls == []
    assert funder_note_subs.list_for_merchant(merchant.id) == []


def test_submit_to_funder_400_when_no_close_lead_id(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs, close_lead_id=None)
    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder")
    assert resp.status_code == 400
    assert "close_lead_id" in resp.json()["detail"]
    assert close_post_calls == []
    assert funder_note_subs.list_for_merchant(merchant.id) == []


def test_submit_to_funder_400_when_top_funder_requires_missing_documents(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    """Stipulations gate (Sprint 6 Track A — supersedes the legacy
    document-completeness gate; same intent, richer payload). The top
    matched funder requires a voided check + 6 months of bank
    statements. The seeded merchant has neither flag set. Submit-to-
    Funder must refuse 400 with a detail dict listing the missing
    stips, and must NOT call Close.
    """
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = FunderRow(
        name="Strict Capital",
        min_monthly_revenue=Decimal("1000"),
        max_positions=10,
        active=True,
        conditional_requirements=(
            "Voided check required at funding",
            "Last 6 months bank statements",
        ),
    )
    funder_repo.upsert(funder)

    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder")
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["error"] == "stipulations_unmet"
    assert body["detail"]["top_funder_name"] == "Strict Capital"
    kinds = {item["kind"] for item in body["detail"]["missing"]}
    assert kinds == {"voided_check", "bank_statements_months"}

    # Close was NOT called and no durable submission row was created.
    assert close_post_calls == []
    assert funder_note_subs.list_for_merchant(merchant.id) == []


def test_submit_to_funder_clears_gate_when_doc_flags_are_set(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    """Same strict funder as the gate-fail case above, but the
    merchant now has the on-file flags set. Route 200s + writes the
    durable row + Close was called exactly once."""
    merchant = MerchantRow(
        business_name="Acme Painting LLC",
        owner_name="Jane Owner",
        state="CA",
        close_lead_id="lead_abc",
        status="finalized",
        voided_check_on_file=True,
        bank_statements_months=6,
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

    funder = FunderRow(
        name="Strict Capital",
        min_monthly_revenue=Decimal("1000"),
        max_positions=10,
        active=True,
        conditional_requirements=(
            "Voided check required at funding",
            "Last 6 months bank statements",
        ),
    )
    funder_repo.upsert(funder)

    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder")
    assert resp.status_code == 200, resp.text
    assert len(close_post_calls) == 1
    assert len(funder_note_subs.list_for_merchant(merchant.id)) == 1
