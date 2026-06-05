"""Tests for the /ui/close-queue dashboard surface.

Two layers:

1. **Classifier unit tests** for ``_classify_close_pipeline_state`` —
   one per state branch (failed_pull, failed_parse, gated, scored,
   awaiting_pull, parsing, stuck-from-pull, stuck-from-parse).
2. **Route integration tests** for ``GET /ui/close-queue`` — covers
   filtering to Close-sourced merchants, action-button selection
   (retry vs review vs none), and the deck header counts.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import DocumentRow, InMemoryDocumentRepository
from aegis.web.router import _classify_close_pipeline_state

# ---------------------------------------------------------------------
# Classifier unit tests
# ---------------------------------------------------------------------


def _doc(parse_status: str, *, uploaded_at: datetime | None = None) -> DocumentRow:
    """Build a DocumentRow with the given parse_status. uploaded_at
    defaults to "now" so freshness tests can override it explicitly."""
    return DocumentRow.model_validate(
        {
            "id": uuid4(),
            "file_hash": "z" * 64,
            "byte_size": 1024,
            "original_filename": "stmt.pdf",
            "parse_status": parse_status,
            "uploaded_at": uploaded_at or datetime.now(UTC),
        }
    )


def _audit(action: str, *, hours_ago: float = 0.1) -> dict[str, Any]:
    ts = datetime.now(UTC) - timedelta(hours=hours_ago)
    return {"action": action, "created_at": ts.isoformat(), "details": {}}


def test_classify_failed_pull_with_message() -> None:
    """list_failed audit → bad chip, retry action, message surfaced."""
    audit = [
        _audit("close.orchestration.enqueued", hours_ago=0.2),
        {
            "action": "close.orchestration.list_failed",
            "created_at": (
                datetime.now(UTC) - timedelta(hours=0.1)
            ).isoformat(),
            "details": {"message": "close 404: not found", "error": "CloseError"},
        },
    ]
    result = _classify_close_pipeline_state(
        docs=[], audit_rows=audit, now=datetime.now(UTC)
    )
    assert result["state"] == "failed_pull"
    assert result["severity"] == "bad"
    assert result["action"] == "retry"
    assert "404" in result["detail"]


def test_classify_awaiting_pull_recent_enqueue() -> None:
    """0 docs + recent enqueued audit → awaiting (info, no action)."""
    result = _classify_close_pipeline_state(
        docs=[],
        audit_rows=[_audit("close.orchestration.enqueued", hours_ago=0.05)],
        now=datetime.now(UTC),
    )
    assert result["state"] == "awaiting_pull"
    assert result["severity"] == "info"
    assert result["action"] is None


def test_classify_stuck_pull_old_enqueue() -> None:
    """0 docs + enqueued >6h ago and no completion → stuck (warn, retry)."""
    result = _classify_close_pipeline_state(
        docs=[],
        audit_rows=[_audit("close.orchestration.enqueued", hours_ago=8.0)],
        now=datetime.now(UTC),
    )
    assert result["state"] == "stuck"
    assert result["severity"] == "warn"
    assert result["action"] == "retry"
    assert "8h" in result["label"] or "8" in result["label"]


def test_classify_stuck_no_audit_no_docs() -> None:
    """0 docs + 0 orchestration audit → stuck (retry)."""
    result = _classify_close_pipeline_state(
        docs=[], audit_rows=[], now=datetime.now(UTC)
    )
    assert result["state"] == "stuck"
    assert result["action"] == "retry"


def test_classify_parsing_some_pending() -> None:
    """Mix of pending + terminal docs (fresh) → parsing (info)."""
    docs = [
        _doc("proceed"),
        _doc("pending"),
        _doc("pending"),
    ]
    result = _classify_close_pipeline_state(
        docs=docs, audit_rows=[], now=datetime.now(UTC)
    )
    assert result["state"] == "parsing"
    assert result["severity"] == "info"
    assert result["action"] is None
    assert "1/3" in result["label"]


def test_classify_stuck_parse_old_pending() -> None:
    """Pending doc uploaded >1h ago → stuck (warn, retry)."""
    docs = [
        _doc("pending", uploaded_at=datetime.now(UTC) - timedelta(hours=2.5))
    ]
    result = _classify_close_pipeline_state(
        docs=docs, audit_rows=[], now=datetime.now(UTC)
    )
    assert result["state"] == "stuck"
    assert result["action"] == "retry"


def test_classify_gated_manual_review() -> None:
    """All docs terminal, manual_review present → gated (review, NOT retry).

    This is the load-bearing distinction: A&R KM hit this state with 4
    Lili statements that the parser flagged for integrity concerns.
    The right action is operator review, not a retry — those flags
    will persist on a re-parse.
    """
    docs = [_doc("manual_review") for _ in range(4)]
    result = _classify_close_pipeline_state(
        docs=docs, audit_rows=[], now=datetime.now(UTC)
    )
    assert result["state"] == "gated"
    assert result["severity"] == "warn"
    assert result["action"] == "review"
    assert "underwriter" in result["label"].lower()
    assert "4 statement" in result["detail"]


def test_classify_gated_mix_with_errors_still_routes_to_underwriter() -> None:
    """If even one doc is manual_review, the merchant needs an
    underwriter regardless of how many errors siblings have. The retry
    button on a row with manual_review siblings would re-parse the
    flagged ones and not change the gating; the operator must look."""
    docs = [
        _doc("manual_review"),
        _doc("error"),
        _doc("proceed"),
    ]
    result = _classify_close_pipeline_state(
        docs=docs, audit_rows=[], now=datetime.now(UTC)
    )
    assert result["state"] == "gated"
    assert result["action"] == "review"
    # Sibling counts surface in the detail line.
    assert "1 errored" in result["detail"]
    assert "1 clean" in result["detail"]


def test_classify_failed_parse_all_errors() -> None:
    """All docs errored, no manual_review, no clean → failed_parse + retry."""
    docs = [_doc("error"), _doc("error")]
    result = _classify_close_pipeline_state(
        docs=docs, audit_rows=[], now=datetime.now(UTC)
    )
    assert result["state"] == "failed_parse"
    assert result["severity"] == "bad"
    assert result["action"] == "retry"


def test_classify_scored_all_clean() -> None:
    """All docs proceed/review, no errors, no manual_review → scored."""
    docs = [_doc("proceed"), _doc("proceed"), _doc("review")]
    result = _classify_close_pipeline_state(
        docs=docs, audit_rows=[], now=datetime.now(UTC)
    )
    assert result["state"] == "scored"
    assert result["severity"] == "good"
    assert result["action"] is None


def test_classify_scored_with_some_errors_sibling() -> None:
    """Some clean, some errored, none manual_review → still scored.
    The clean ones can be aggregated; the errored siblings show on the
    dossier separately. No retry needed at the queue level."""
    docs = [_doc("proceed"), _doc("error"), _doc("proceed")]
    result = _classify_close_pipeline_state(
        docs=docs, audit_rows=[], now=datetime.now(UTC)
    )
    assert result["state"] == "scored"
    assert "1 errored" in result["detail"]


# ---------------------------------------------------------------------
# Route + template integration
# ---------------------------------------------------------------------


@pytest.fixture
def empty_funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def fresh_repos() -> (
    tuple[InMemoryMerchantRepository, InMemoryDocumentRepository, InMemoryAuditLog]
):
    return (
        InMemoryMerchantRepository(),
        InMemoryDocumentRepository(),
        InMemoryAuditLog(),
    )


@pytest.fixture
def client(
    fresh_repos: tuple[
        InMemoryMerchantRepository, InMemoryDocumentRepository, InMemoryAuditLog
    ],
    empty_funder_repo: InMemoryFunderRepository,
) -> Iterator[TestClient]:
    merchants, docs, audit = fresh_repos
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_funder_repository] = lambda: empty_funder_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_close_queue_empty_state(client: TestClient) -> None:
    """No Close-sourced merchants → empty-state copy renders, no crash."""
    resp = client.get("/ui/close-queue")
    assert resp.status_code == 200
    assert "No Close-sourced merchants" in resp.text


def test_close_queue_excludes_non_close_merchants(
    client: TestClient,
    fresh_repos: tuple[
        InMemoryMerchantRepository, InMemoryDocumentRepository, InMemoryAuditLog
    ],
) -> None:
    """A merchant without close_lead_id must not appear on the queue."""
    merchants, _docs, _audit = fresh_repos
    merchants.upsert(
        MerchantRow(
            business_name="Walk-in Merchant",
            owner_name="Walk In",
            state="CA",
            close_lead_id=None,
        )
    )
    resp = client.get("/ui/close-queue")
    assert resp.status_code == 200
    assert "Walk-in Merchant" not in resp.text
    assert "No Close-sourced merchants" in resp.text


def test_close_queue_failed_pull_renders_retry_button(
    client: TestClient,
    fresh_repos: tuple[
        InMemoryMerchantRepository, InMemoryDocumentRepository, InMemoryAuditLog
    ],
) -> None:
    """list_failed audit + 0 docs → retry button targets close-rescan."""
    merchants, _docs, audit = fresh_repos
    m = MerchantRow(
        business_name="Stuck Merchant",
        owner_name="Op",
        state="CA",
        close_lead_id="lead_test_failed",
    )
    saved = merchants.upsert(m)
    audit.record(
        actor="worker",
        action="close.orchestration.list_failed",
        subject_type="merchant",
        subject_id=saved.id,
        details={"message": "close 404"},
    )
    resp = client.get("/ui/close-queue")
    assert resp.status_code == 200
    assert "Stuck Merchant" in resp.text
    assert "Failed to pull" in resp.text
    assert f"/ui/merchants/{saved.id}/close-rescan" in resp.text
    assert "Retry rescan" in resp.text


def test_close_queue_gated_renders_review_link_not_retry(
    client: TestClient,
    fresh_repos: tuple[
        InMemoryMerchantRepository, InMemoryDocumentRepository, InMemoryAuditLog
    ],
) -> None:
    """All docs manual_review → "Needs underwriter" chip + dossier link,
    NO retry button. This is the A&R KM case."""
    merchants, doc_repo, _audit = fresh_repos
    saved = merchants.upsert(
        MerchantRow(
            business_name="Gated Merchant",
            owner_name="Op",
            state="CA",
            close_lead_id="lead_test_gated",
        )
    )
    for i in range(4):
        doc = doc_repo.create_document(
            file_hash=f"{i:>064}", byte_size=1024, original_filename=f"stmt-{i}.pdf"
        )
        flagged = doc.model_copy(
            update={"merchant_id": saved.id, "parse_status": "manual_review"}
        )
        doc_repo._docs[doc.id] = flagged

    resp = client.get("/ui/close-queue")
    assert resp.status_code == 200
    assert "Gated Merchant" in resp.text
    assert "Needs underwriter" in resp.text
    # No retry POST form anchored on this merchant — review only.
    assert (
        f'action="/ui/merchants/{saved.id}/close-rescan"'
        not in resp.text
    )
    # Dossier link is present.
    assert f"/ui/merchants/{saved.id}" in resp.text
    assert "Open dossier" in resp.text


def test_close_queue_failed_parse_all_errors_offers_retry(
    client: TestClient,
    fresh_repos: tuple[
        InMemoryMerchantRepository, InMemoryDocumentRepository, InMemoryAuditLog
    ],
) -> None:
    """All docs at parse_status=error → retry button."""
    merchants, doc_repo, _audit = fresh_repos
    saved = merchants.upsert(
        MerchantRow(
            business_name="Errored Merchant",
            owner_name="Op",
            state="CA",
            close_lead_id="lead_test_err",
        )
    )
    for i in range(2):
        doc = doc_repo.create_document(
            file_hash=f"e{i:>063}", byte_size=1024, original_filename=f"bad-{i}.pdf"
        )
        errored = doc.model_copy(
            update={"merchant_id": saved.id, "parse_status": "error"}
        )
        doc_repo._docs[doc.id] = errored

    resp = client.get("/ui/close-queue")
    assert resp.status_code == 200
    assert "Failed to parse" in resp.text
    assert f"/ui/merchants/{saved.id}/close-rescan" in resp.text


def test_close_queue_scored_renders_view_only(
    client: TestClient,
    fresh_repos: tuple[
        InMemoryMerchantRepository, InMemoryDocumentRepository, InMemoryAuditLog
    ],
) -> None:
    """All docs proceed → "Scored" chip, View link, no retry."""
    merchants, doc_repo, _audit = fresh_repos
    saved = merchants.upsert(
        MerchantRow(
            business_name="Clean Merchant",
            owner_name="Op",
            state="CA",
            close_lead_id="lead_test_clean",
        )
    )
    doc = doc_repo.create_document(
        file_hash="c" * 64, byte_size=1024, original_filename="clean.pdf"
    )
    clean = doc.model_copy(
        update={"merchant_id": saved.id, "parse_status": "proceed"}
    )
    doc_repo._docs[doc.id] = clean

    resp = client.get("/ui/close-queue")
    assert resp.status_code == 200
    assert "Clean Merchant" in resp.text
    assert ">Scored<" in resp.text
    # No retry POST form on a scored row.
    assert (
        f'action="/ui/merchants/{saved.id}/close-rescan"'
        not in resp.text
    )


def test_close_queue_nav_link_present(client: TestClient) -> None:
    """Topstrip nav must include the Close-queue link on every page."""
    resp = client.get("/ui/merchants")
    assert resp.status_code == 200
    assert "/ui/close-queue" in resp.text


def test_close_queue_sorts_failures_first(
    client: TestClient,
    fresh_repos: tuple[
        InMemoryMerchantRepository, InMemoryDocumentRepository, InMemoryAuditLog
    ],
) -> None:
    """Failures appear above scored — at 30/day the operator scans top
    of list for what needs attention."""
    merchants, doc_repo, audit = fresh_repos
    # Scored first by name (would sort alphabetically before "Z-fail")
    a_clean = merchants.upsert(
        MerchantRow(
            business_name="A Clean",
            owner_name="Op",
            state="CA",
            close_lead_id="lead_a",
        )
    )
    doc = doc_repo.create_document(
        file_hash="a" * 64, byte_size=1024, original_filename="a.pdf"
    )
    doc_repo._docs[doc.id] = doc.model_copy(
        update={"merchant_id": a_clean.id, "parse_status": "proceed"}
    )
    # Failed
    z_fail = merchants.upsert(
        MerchantRow(
            business_name="Z Fail",
            owner_name="Op",
            state="CA",
            close_lead_id="lead_z",
        )
    )
    audit.record(
        actor="worker",
        action="close.orchestration.list_failed",
        subject_type="merchant",
        subject_id=z_fail.id,
        details={"message": "404"},
    )

    resp = client.get("/ui/close-queue")
    assert resp.status_code == 200
    pos_fail = resp.text.find("Z Fail")
    pos_clean = resp.text.find("A Clean")
    assert 0 <= pos_fail < pos_clean, (
        f"Z Fail at {pos_fail}, A Clean at {pos_clean}: failure should sort first"
    )
