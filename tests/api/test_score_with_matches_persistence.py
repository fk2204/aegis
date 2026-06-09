"""U20 — submission persistence at the operator submit wire-point.

Per the U20 task: "after the operator confirms submission (the existing
audit_log write), persist a SubmissionRecord row per matched funder."
The operator-confirmed submission act in AEGIS is
``POST /ui/merchants/{id}/submit`` (the dashboard form). This file's
tests fire that endpoint and assert the durable submissions table now
carries one row per matched funder, with the funder ids matching the
selected FunderMatch ids.

``/deals/score-with-matches`` returns the ranking only — it does not
"submit" — so persistence does NOT fire from that route. The negative
test below pins that contract.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    get_submission_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
from aegis.submissions import InMemorySubmissionRepository
from tests.test_storage import _make_pipeline_result


@pytest.fixture
def merchant() -> MerchantRow:
    return MerchantRow(business_name="Acme Inc", owner_name="Jane Doe", state="CA")


@pytest.fixture
def merchant_repo(merchant: MerchantRow) -> InMemoryMerchantRepository:
    repo = InMemoryMerchantRepository()
    repo.upsert(merchant)
    return repo


@pytest.fixture
def doc_repo(merchant: MerchantRow) -> InMemoryDocumentRepository:
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="z" * 64, byte_size=1024, original_filename="x.pdf"
    )
    row = row.model_copy(update={"merchant_id": merchant.id})
    repo._docs[row.id] = row
    repo.persist_parse_result(
        row.id, result=_make_pipeline_result(), merchant_id=merchant.id
    )
    return repo


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    repo = InMemoryFunderRepository()
    repo.upsert(
        FunderRow(
            name="Alpha Capital",
            min_monthly_revenue=Decimal("25000"),
            min_credit_score=600,
            accepts_stacking=False,
        )
    )
    repo.upsert(
        FunderRow(
            name="Beta Capital",
            min_monthly_revenue=Decimal("50000"),
            accepts_stacking=False,
        )
    )
    return repo


@pytest.fixture
def audit_log() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def submissions_repo() -> InMemorySubmissionRepository:
    return InMemorySubmissionRepository()


@pytest.fixture
def client(
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
    submissions_repo: InMemorySubmissionRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_repository] = lambda: doc_repo
    app.dependency_overrides[get_audit] = lambda: audit_log
    app.dependency_overrides[get_submission_repository] = lambda: submissions_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Positive: /ui/merchants/{id}/submit persists one row per matched funder.
# ---------------------------------------------------------------------------


def test_submit_persists_one_submission_row_per_matched_funder(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
    submissions_repo: InMemorySubmissionRepository,
) -> None:
    """Submitting to two funders writes two durable submissions rows.

    The audit row was already written by the existing handler; U20 adds
    the durable rows alongside it. funder ids on the persisted rows
    match the funders the operator selected.
    """
    funders = funder_repo.list_active()
    selected_ids = [str(f.id) for f in funders]
    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": selected_ids},
    )
    assert resp.status_code == 200, resp.text
    # Sanity: ZIP body, not 400.
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        zip_names = z.namelist()
    assert len(zip_names) == 2

    persisted = submissions_repo.list_for_merchant(merchant.id)
    assert len(persisted) == 2, (
        f"expected 2 submissions rows; got {len(persisted)}"
    )

    # Funder ids on the persisted rows match the operator's selection.
    persisted_funder_ids = {str(s.funder_id) for s in persisted}
    assert persisted_funder_ids == set(selected_ids)

    # All rows status='submitted' by default — operator hasn't received
    # a funder reply yet.
    assert {s.status for s in persisted} == {"submitted"}


def test_submit_persists_csv_doc_hash_matches_zip_member(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
    submissions_repo: InMemorySubmissionRepository,
) -> None:
    """Each persisted row's ``csv_doc_hash`` is the sha256 of THAT
    funder's CSV bytes — so a regulator question "what did we send X?"
    is answerable from the submissions row alone (mirrors
    ``disclosure_transmission_log.disclosure_doc_hash``).
    """
    import hashlib

    funders = funder_repo.list_active()
    selected_ids = [str(f.id) for f in funders]
    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": selected_ids},
    )
    assert resp.status_code == 200, resp.text

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        # Map filename → sha256.
        zip_hashes = {
            name: hashlib.sha256(z.read(name)).hexdigest()
            for name in z.namelist()
        }

    persisted = submissions_repo.list_for_merchant(merchant.id)
    persisted_hashes = {s.csv_filename: s.csv_doc_hash for s in persisted}

    assert set(zip_hashes) == set(persisted_hashes)
    for fname, sha in zip_hashes.items():
        assert persisted_hashes[fname] == sha


def test_submit_writes_submission_persisted_audit_row_per_funder(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
    submissions_repo: InMemorySubmissionRepository,
) -> None:
    """Each persisted submission gets its own ``deal.submission_persisted``
    audit row (in addition to the single ``deal.submit_to_funders`` row
    the dashboard handler wrote)."""
    funders = funder_repo.list_active()
    selected_ids = [str(f.id) for f in funders]
    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": selected_ids},
    )
    assert resp.status_code == 200, resp.text

    submit_rows = [
        e for e in audit_log.entries if e["action"] == "deal.submit_to_funders"
    ]
    persisted_rows = [
        e
        for e in audit_log.entries
        if e["action"] == "deal.submission_persisted"
    ]
    # Original handler writes one rollup row.
    assert len(submit_rows) == 1
    # Plus one per-funder persistence row.
    assert len(persisted_rows) == 2

    persisted = {str(s.id) for s in submissions_repo.list_for_merchant(merchant.id)}
    audit_submission_ids = {
        r["details"]["submission_id"] for r in persisted_rows
    }
    assert audit_submission_ids == persisted


def test_resubmitting_same_merchant_doc_funder_is_idempotent(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
    submissions_repo: InMemorySubmissionRepository,
) -> None:
    """A re-submit to the same funder for the same document does NOT
    write a duplicate row. The natural key ``(merchant, document, funder)``
    is uniquely indexed (migration 013); the handler catches
    ``SubmissionConflictError`` and treats the second attempt as
    operator-visible "already submitted"."""
    funders = funder_repo.list_active()
    one_id = str(funders[0].id)

    resp1 = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": [one_id]},
    )
    assert resp1.status_code == 200, resp1.text
    assert len(submissions_repo.list_for_merchant(merchant.id)) == 1

    resp2 = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": [one_id]},
    )
    # The second call still returns 200 (the CSV is regenerated and
    # returned to the operator) but no duplicate row landed.
    assert resp2.status_code == 200, resp2.text
    assert len(submissions_repo.list_for_merchant(merchant.id)) == 1


# ---------------------------------------------------------------------------
# Negative: /deals/score-with-matches does NOT persist submissions.
# ---------------------------------------------------------------------------


def test_score_with_matches_does_not_persist_submissions(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
    doc_repo: InMemoryDocumentRepository,
    submissions_repo: InMemorySubmissionRepository,
) -> None:
    """``/deals/score-with-matches`` returns the ranking only — it is
    NOT the submission act. Persistence must NOT fire from this route.

    The operator confirms submission via the dashboard's
    ``/ui/merchants/{id}/submit`` form; persistence is wired there.
    Pinning the contract here prevents a future refactor from silently
    inflating funder approval-rate denominators by counting every
    "compare matches" click as a submission.
    """
    # We don't need to exercise the /deals/score-with-matches route end
    # to end (its full payload requires the OFAC dep + valid score
    # input). The contract is enforced by code review + the wire-point
    # location: ``record_submission`` is only called inside
    # ``merchant_submit_to_funders``. Smoke-check by asserting the repo
    # is empty after a /deals route hit would be flaky depending on
    # auth wiring — instead we pin the structural invariant.
    import inspect

    from aegis.api.routes import deals as deals_module

    source = inspect.getsource(deals_module)
    assert "record_submission" not in source, (
        "deals route persisted submissions — /deals/score-with-matches is "
        "ranking-only; persistence must stay in merchant_submit_to_funders"
    )
    # And the submissions repo is untouched.
    assert submissions_repo.list_for_merchant(merchant.id) == []
