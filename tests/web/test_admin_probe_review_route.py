"""Tests for /ui/admin/text-layer-probe-review GET + POST + banner.

Covers:
  * ``GET /ui/admin/text-layer-probe-review`` with a mocked repo + a
    seeded disagreement document renders the bank name, both routing
    decisions, and the row's data-test-id.
  * ``POST .../{doc_id}/verdict`` with ``verdict=v2_correct``
    persists the verdict + writes the ``probe_review.verdict_recorded``
    audit row + returns an empty 200 body.
  * Banner renders only after ``count_verdicts`` shows
    ``v2_correct >= 10`` AND ``v1_correct <= 2``.
  * Flip-to-live stub records the ``probe_review.flip_requested``
    audit row + returns 202 with the operator instruction message.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_operator_repository,
    get_probe_review_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.operator_repository import InMemoryOperatorRepository
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER, Operator, OperatorRole
from aegis.probe_review import (
    PROBE_TEXT_LAYER_V2,
    InMemoryProbeReviewRepository,
)
from aegis.storage import InMemoryDocumentRepository


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def probe_repo() -> InMemoryProbeReviewRepository:
    return InMemoryProbeReviewRepository()


_ADMIN_EMAIL = "filip@commerafunding.com"


@pytest.fixture
def operators() -> InMemoryOperatorRepository:
    repo = InMemoryOperatorRepository()
    repo._seed(
        Operator(
            id=uuid4(),
            email=_ADMIN_EMAIL,
            display_name="Filip",
            role=OperatorRole.ADMIN,
        )
    )
    return repo


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
    probe_repo: InMemoryProbeReviewRepository,
    operators: InMemoryOperatorRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_probe_review_repository] = lambda: probe_repo
    app.dependency_overrides[get_operator_repository] = lambda: operators
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_merchant(merchants: InMemoryMerchantRepository, *, business_name: str) -> UUID:
    saved = merchants.upsert(MerchantRow(business_name=business_name, state="CA"))
    return saved.id


def _seed_disagreement_document(
    docs: InMemoryDocumentRepository,
    *,
    merchant_id: UUID,
    filename: str = "flagged.pdf",
) -> UUID:
    row = docs.create_document(
        file_hash="hash-" + uuid4().hex,
        byte_size=2048,
        original_filename=filename,
        merchant_id=merchant_id,
    )
    docs._docs[row.id].all_flags = [
        (
            "[SHADOW] text_layer_probe_v2_disagrees: "
            "v2_route_vision=True live_route_vision=False "
            "chars_avg=4 numeric_lines=1"
        )
    ]
    docs._docs[row.id].parse_status = "proceed"
    docs._docs[row.id].parsed_at = datetime.now(UTC)
    return row.id


def test_get_renders_disagreement_rows(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
) -> None:
    merchant_id = _seed_merchant(merchants, business_name="Acme Inc")
    doc_id = _seed_disagreement_document(docs, merchant_id=merchant_id)

    resp = client.get(
        "/ui/admin/text-layer-probe-review",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "probe-review-table" in body
    assert str(doc_id) in body
    # The two routing decisions render in their humanised form.
    assert "route to vision" in body
    assert "use text layer" in body
    assert "flagged.pdf" in body


def test_get_renders_empty_state_with_no_disagreements(
    client: TestClient,
) -> None:
    resp = client.get(
        "/ui/admin/text-layer-probe-review",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200
    assert "probe-review-empty" in resp.text


def test_post_verdict_persists_and_audits(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
    probe_repo: InMemoryProbeReviewRepository,
    audit: InMemoryAuditLog,
) -> None:
    merchant_id = _seed_merchant(merchants, business_name="Acme Inc")
    doc_id = _seed_disagreement_document(docs, merchant_id=merchant_id)

    resp = client.post(
        f"/ui/admin/text-layer-probe-review/{doc_id}/verdict",
        data={"verdict": "v2_correct"},
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200
    assert resp.text == ""

    # Verdict is persisted in the repo.
    counts = probe_repo.count_verdicts(PROBE_TEXT_LAYER_V2)
    assert counts["v2_correct"] == 1
    assert counts["v1_correct"] == 0

    # Audit row was written with the expected action + details.
    actions = [e["action"] for e in audit.entries]
    assert "probe_review.verdict_recorded" in actions
    entry = next(e for e in audit.entries if e["action"] == "probe_review.verdict_recorded")
    assert entry["actor_email"] == "filip@commerafunding.com"
    assert entry["subject_type"] == "document"
    assert entry["subject_id"] == str(doc_id)
    assert entry["details"]["probe_name"] == PROBE_TEXT_LAYER_V2
    assert entry["details"]["verdict"] == "v2_correct"


def test_post_verdict_rejects_invalid_value(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
) -> None:
    merchant_id = _seed_merchant(merchants, business_name="Acme Inc")
    doc_id = _seed_disagreement_document(docs, merchant_id=merchant_id)

    resp = client.post(
        f"/ui/admin/text-layer-probe-review/{doc_id}/verdict",
        data={"verdict": "bogus"},
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 400


def test_banner_hidden_below_threshold(
    client: TestClient,
    probe_repo: InMemoryProbeReviewRepository,
) -> None:
    # Only 9 v2_correct (one short of the floor) — banner stays hidden.
    for _ in range(9):
        probe_repo.add_verdict(
            document_id=uuid4(),
            probe_name=PROBE_TEXT_LAYER_V2,
            verdict="v2_correct",
            operator_email=f"op-{uuid4()}@aegis.local",
        )
    resp = client.get(
        "/ui/admin/text-layer-probe-review",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200
    assert "probe-review-flip-banner" not in resp.text


def test_banner_appears_at_threshold(
    client: TestClient,
    probe_repo: InMemoryProbeReviewRepository,
) -> None:
    # 10 v2_correct + 2 v1_correct → banner renders.
    for _ in range(10):
        probe_repo.add_verdict(
            document_id=uuid4(),
            probe_name=PROBE_TEXT_LAYER_V2,
            verdict="v2_correct",
            operator_email=f"op-{uuid4()}@aegis.local",
        )
    for _ in range(2):
        probe_repo.add_verdict(
            document_id=uuid4(),
            probe_name=PROBE_TEXT_LAYER_V2,
            verdict="v1_correct",
            operator_email=f"op-{uuid4()}@aegis.local",
        )
    resp = client.get(
        "/ui/admin/text-layer-probe-review",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200
    assert "probe-review-flip-banner" in resp.text


def test_banner_hidden_when_too_many_v1_correct(
    client: TestClient,
    probe_repo: InMemoryProbeReviewRepository,
) -> None:
    # 10 v2_correct but 3 v1_correct — the ceiling is 2, banner hidden.
    for _ in range(10):
        probe_repo.add_verdict(
            document_id=uuid4(),
            probe_name=PROBE_TEXT_LAYER_V2,
            verdict="v2_correct",
            operator_email=f"op-{uuid4()}@aegis.local",
        )
    for _ in range(3):
        probe_repo.add_verdict(
            document_id=uuid4(),
            probe_name=PROBE_TEXT_LAYER_V2,
            verdict="v1_correct",
            operator_email=f"op-{uuid4()}@aegis.local",
        )
    resp = client.get(
        "/ui/admin/text-layer-probe-review",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200
    assert "probe-review-flip-banner" not in resp.text


def test_flip_to_live_returns_202_and_audits(
    client: TestClient,
    audit: InMemoryAuditLog,
) -> None:
    resp = client.post(
        "/ui/admin/text-layer-probe-review/flip-to-live",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert "operator must edit" in body["message"]
    assert body["probe_name"] == PROBE_TEXT_LAYER_V2

    actions = [e["action"] for e in audit.entries]
    assert "probe_review.flip_requested" in actions
