"""Phase 7B funder submission workflow.

Covers:
  * Per-funder CSV (``submission_csv.build_submission_csv``) — shape +
    PII safety.
  * Per-funder file batch (``submission_package.build_submission_files``).
  * ``POST /ui/merchants/{id}/submit`` — single CSV vs ZIP, audit row,
    funder filtering, error paths.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
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
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.scoring.models import FunderMatch, ScoreInput, ScoreResult
from aegis.scoring.submission_csv import build_submission_csv
from aegis.scoring.submission_package import build_submission_files
from aegis.storage import InMemoryDocumentRepository
from tests.test_storage import _make_pipeline_result

# --- unit tests: submission_csv / submission_package ------------------------


def _make_score_input() -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Painting LLC",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="238320",
        industry_risk_tier="moderate",
        time_in_business_months=48,
        credit_score=720,
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("110000.00"),
        monthly_revenue=Decimal("110000.00"),
        lowest_balance=Decimal("3500.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=True,
        returned_ach_count=0,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        fraud_score=10,
        eof_markers=1,
        validation_passed=True,
        extraction_confidence=95,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


def _make_score_result() -> ScoreResult:
    return ScoreResult(
        score=78,
        tier="B",
        recommendation="approve",
        hard_decline_reasons=[],
        soft_concerns=["customer_concentration_high"],
        suggested_max_advance=Decimal("60000.00"),
        recommended_factor_rate=Decimal("1.29"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=129,
    )


def _make_match(name: str = "Test Capital") -> FunderMatch:
    return FunderMatch(
        funder_id=uuid4(),
        funder_name=name,
        match_score=85,
        reasons=["tier_B"],
        soft_concerns=["stacking_max_unspecified"],
    )


def test_build_submission_csv_includes_funder_name() -> None:
    csv_text = build_submission_csv(
        deal=_make_score_input(),
        score=_make_score_result(),
        match=_make_match("Forward Capital"),
    )
    assert "Forward Capital" in csv_text
    # Header section present
    assert "meta,funder_name,Forward Capital" in csv_text
    assert "match,funder_name,Forward Capital" in csv_text


def test_build_submission_csv_excludes_pii_audit_columns() -> None:
    """Funder-facing CSV must NOT include EIN, SSN, account_holder, etc.

    These are funder-collected post-approval; AEGIS does not distribute
    them in the submission package.
    """
    csv_text = build_submission_csv(
        deal=_make_score_input(),
        score=_make_score_result(),
        match=_make_match(),
    )
    # Negative checks — none of these labels should appear.
    lowered = csv_text.lower()
    for forbidden in ("ein", "ssn", "account_holder", "account_last4", "tax_id"):
        assert forbidden not in lowered, (
            f"{forbidden!r} leaked into funder-facing CSV"
        )


def test_build_submission_csv_contains_aegis_verdict() -> None:
    csv_text = build_submission_csv(
        deal=_make_score_input(),
        score=_make_score_result(),
        match=_make_match(),
    )
    assert "aegis,tier,B" in csv_text
    assert "aegis,score,78" in csv_text
    assert "aegis,recommendation,approve" in csv_text
    # Decimal values should be string-rendered, not float-coerced.
    assert "aegis,suggested_max_advance,60000.00" in csv_text
    assert "aegis,recommended_factor_rate,1.29" in csv_text


def test_submission_package_batch_returns_one_per_funder() -> None:
    deal = _make_score_input()
    score = _make_score_result()
    matches = [_make_match("Alpha Capital"), _make_match("Beta Capital")]
    files = build_submission_files(deal, score, matches)
    assert len(files) == 2
    names = {f.funder_name for f in files}
    assert names == {"Alpha Capital", "Beta Capital"}
    for f in files:
        assert f.csv_bytes
        assert f.filename.endswith(".csv")
        # Filename has both merchant + funder slug.
        assert "acme_painting_llc" in f.filename
        assert f.email_subject
        assert f.email_body


# --- integration: POST /ui/merchants/{id}/submit ----------------------------


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
    repo.persist_parse_result(row.id, result=_make_pipeline_result(), merchant_id=merchant.id)
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
def client(
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_repository] = lambda: doc_repo
    app.dependency_overrides[get_audit] = lambda: audit_log
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_submit_single_funder_returns_csv(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    one = funder_repo.list_active()[0]
    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": [str(one.id)]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert one.name in resp.text
    assert "meta,funder_name" in resp.text


def test_submit_multiple_funders_returns_zip(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    ids = [str(f.id) for f in funder_repo.list_active()]
    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": ids},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        names = z.namelist()
    assert len(names) == 2, names
    for n in names:
        assert n.endswith(".csv")


def test_submit_audit_records_attachment_sha256(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> None:
    """The deal.submit_to_funders audit row must record the SHA-256 of
    each attached artifact (submission CSV/ZIP and the dossier PDF when
    rendered). The dossier SHA is None when weasyprint native libs
    aren't available — that's expected and explicit, not silent."""
    ids = [str(f.id) for f in funder_repo.list_active()]
    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": ids},
    )
    assert resp.status_code == 200

    submit_rows = [
        e for e in audit_log.entries if e["action"] == "deal.submit_to_funders"
    ]
    assert len(submit_rows) == 1
    details = submit_rows[0]["details"]

    # CSV/ZIP attachment SHA is always present and a 64-char hex string.
    assert "attachment_sha256" in details
    assert isinstance(details["attachment_sha256"], str)
    assert len(details["attachment_sha256"]) == 64
    assert "attachment_filename" in details

    # PDF SHA + filename keys are always present; values are either both
    # populated (native libs rendered the PDF) or both None (Windows dev
    # without WSL2). Either way, the operator can tell from the audit
    # row whether a dossier was attached.
    assert "dossier_pdf_sha256" in details
    assert "dossier_pdf_filename" in details
    if details["dossier_pdf_sha256"] is not None:
        assert len(details["dossier_pdf_sha256"]) == 64
        assert details["dossier_pdf_filename"] is not None
    else:
        assert details["dossier_pdf_filename"] is None


def test_submit_audits_with_funder_ids(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> None:
    ids = [str(f.id) for f in funder_repo.list_active()]
    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": ids},
    )
    assert resp.status_code == 200
    submit_rows = [
        e for e in audit_log.entries if e["action"] == "deal.submit_to_funders"
    ]
    assert len(submit_rows) == 1
    detail = submit_rows[0]["details"]
    assert sorted(detail["funder_ids"]) == sorted(ids)
    assert set(detail["funder_names"]) == {"Alpha Capital", "Beta Capital"}


def test_submit_rejects_empty_funder_list(
    client: TestClient, merchant: MerchantRow
) -> None:
    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": [""]},
    )
    assert resp.status_code == 400
    assert "no funders selected" in resp.text


def test_submit_filters_to_requested_funders_only(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """An unselected funder must not appear in the package."""
    alpha = next(f for f in funder_repo.list_active() if f.name == "Alpha Capital")
    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": [str(alpha.id)]},
    )
    assert resp.status_code == 200
    # Beta should NOT appear — single-CSV branch.
    assert "Beta Capital" not in resp.text


def test_submit_404_for_unknown_merchant(client: TestClient) -> None:
    resp = client.post(
        f"/ui/merchants/{uuid4()}/submit",
        data={"funder_ids": [str(uuid4())]},
    )
    assert resp.status_code == 404


def test_submit_400_when_merchant_has_no_document(
    client: TestClient, funder_repo: InMemoryFunderRepository
) -> None:
    """A merchant with no parsed document can't be submitted."""
    repo = InMemoryMerchantRepository()
    bare = MerchantRow(business_name="No Docs LLC", owner_name="Owner", state="CA")
    repo.upsert(bare)
    app = cast(FastAPI, client.app)
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_repository] = lambda: InMemoryDocumentRepository()
    resp = client.post(
        f"/ui/merchants/{bare.id}/submit",
        data={"funder_ids": [str(funder_repo.list_active()[0].id)]},
    )
    assert resp.status_code == 400
    assert "no analyzed document" in resp.text.lower()


def test_match_page_shows_submit_button(
    client: TestClient, merchant: MerchantRow, funder_repo: InMemoryFunderRepository
) -> None:
    """The match page must expose the submission form for the operator."""
    _ = funder_repo  # ensure the fixture-injected funders are routed in
    resp = client.get(f"/ui/merchants/{merchant.id}/match")
    assert resp.status_code == 200
    assert f'action="/ui/merchants/{merchant.id}/submit"' in resp.text
    assert 'name="funder_ids"' in resp.text
    assert "Download submission package" in resp.text


def test_match_page_renders_funder_response_form(
    client: TestClient, merchant: MerchantRow, funder_repo: InMemoryFunderRepository
) -> None:
    """The match page exposes a form to record what each funder replied."""
    _ = funder_repo
    resp = client.get(f"/ui/merchants/{merchant.id}/match")
    assert resp.status_code == 200
    assert f'action="/ui/merchants/{merchant.id}/funder-response"' in resp.text
    assert 'name="response_status"' in resp.text
    assert 'name="offered_amount"' in resp.text
    assert "Record reply" in resp.text


def test_funder_response_records_audit_row(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> None:
    """Happy path: form POST writes one structured audit row."""
    f = funder_repo.list_active()[0]
    resp = client.post(
        f"/ui/merchants/{merchant.id}/funder-response",
        data={
            "funder_id": str(f.id),
            "response_status": "approved",
            "offered_amount": "55000",
            "offered_factor": "1.32",
            "offered_term_days": "120",
            "notes": "wants ACH not lockbox",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"/ui/merchants/{merchant.id}/match")

    rows = [e for e in audit_log.entries if e["action"] == "deal.funder_response"]
    assert len(rows) == 1
    d = rows[0]["details"]
    assert d["funder_id"] == str(f.id)
    assert d["funder_name"] == f.name
    assert d["status"] == "approved"
    assert d["offered_amount"] == "55000"
    assert d["offered_factor"] == "1.32"
    assert d["offered_term_days"] == 120
    assert d["notes"] == "wants ACH not lockbox"


def test_funder_response_rejects_unknown_status(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    f = funder_repo.list_active()[0]
    resp = client.post(
        f"/ui/merchants/{merchant.id}/funder-response",
        data={"funder_id": str(f.id), "response_status": "ghosted"},
    )
    assert resp.status_code == 400
    assert "response_status" in resp.text


def test_funder_response_latest_reply_renders_on_match_panel(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """After recording approved → declined for the same funder, the panel
    shows the latest (declined) — operators read the most-recent answer."""
    f = funder_repo.list_active()[0]
    client.post(
        f"/ui/merchants/{merchant.id}/funder-response",
        data={"funder_id": str(f.id), "response_status": "approved"},
    )
    client.post(
        f"/ui/merchants/{merchant.id}/funder-response",
        data={
            "funder_id": str(f.id),
            "response_status": "declined",
            "notes": "changed their mind",
        },
    )
    resp = client.get(f"/ui/merchants/{merchant.id}/match")
    assert resp.status_code == 200
    assert "Funder reply" in resp.text
    assert "declined" in resp.text
    assert "changed their mind" in resp.text
