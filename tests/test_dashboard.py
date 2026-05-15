"""HTMX dashboard tests.

Verify each page renders, the merchant detail shows aggregates, and the
drill-down HTMX partial returns the contributing transactions only.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

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
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
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
    # Tie the document to the merchant + persist a parsed result.
    row = row.model_copy(update={"merchant_id": merchant.id})
    repo._docs[row.id] = row
    repo.persist_parse_result(row.id, result=_make_pipeline_result(), merchant_id=merchant.id)
    return repo


@pytest.fixture
def funder_repo_seeded() -> InMemoryFunderRepository:
    """Funder repo populated with two test funders for /ui/funders coverage."""
    from decimal import Decimal

    from aegis.funders.models import FunderRow

    repo = InMemoryFunderRepository()
    repo.upsert(
        FunderRow(
            name="Test Capital",
            min_monthly_revenue=Decimal("25000"),
            min_credit_score=600,
            accepts_stacking=False,
        )
    )
    repo.upsert(
        FunderRow(
            name="Detail Capital",
            min_monthly_revenue=Decimal("50000"),
            excluded_states=("TX",),
            notes="Operator-curated note.",
        )
    )
    return repo


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    """Empty funder repo. Tests that need seeded funders use ``funder_repo_seeded``."""
    return InMemoryFunderRepository()


@pytest.fixture
def client(
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    request: pytest.FixtureRequest,
) -> Iterator[TestClient]:
    """Default client uses an empty funder repo. Tests requesting
    ``funder_repo_seeded`` get the seeded one routed in via the
    dependency override below.
    """
    if "funder_repo_seeded" in request.fixturenames:
        funder_repo = request.getfixturevalue("funder_repo_seeded")
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_repository] = lambda: doc_repo
    app.dependency_overrides[get_audit] = lambda: InMemoryAuditLog()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_dashboard_index_renders(client: TestClient) -> None:
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert "AEGIS" in resp.text


def test_index_renders_live_kpis(
    client: TestClient,
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    """Today dashboard must surface live counts (not "—" placeholders).

    The fixture creates one merchant + one parsed document with
    parse_status="proceed", so we expect: merchant_total=1, proceed=1,
    in_pipeline=1, manual_review=0.
    """
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert merchant is not None  # fixture-loaded
    assert doc_repo is not None  # fixture-loaded
    # No placeholder text from the v2-redesign era.
    assert "data hookup TBD" not in resp.text
    assert "preview · audit_log hookup TBD" not in resp.text
    # KPI labels for the live counts are present.
    assert "Merchants" in resp.text
    assert "In pipeline" in resp.text
    assert "Cleared" in resp.text
    # Funnel rows now live (have non-zero counts somewhere).
    assert "Pipeline funnel" in resp.text


def test_index_manual_review_doc_surfaces_in_attention_panel(
    client: TestClient,
    doc_repo: InMemoryDocumentRepository,
    merchant: MerchantRow,
) -> None:
    """Force a doc into manual_review and verify it appears on Today."""
    docs = list(doc_repo._docs.values())
    target = docs[0]
    flagged = target.model_copy(
        update={
            "parse_status": "manual_review",
            "fraud_score": 82,
            "all_flags": ["[META] suspicious_metadata"],
        }
    )
    doc_repo._docs[target.id] = flagged

    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text
    assert "82" in resp.text
    assert "suspicious_metadata" in resp.text


def test_dashboard_upload_page_has_form(client: TestClient) -> None:
    resp = client.get("/ui/upload")
    assert resp.status_code == 200
    assert 'enctype="multipart/form-data"' in resp.text
    # Browser-friendly route — POSTs to /ui/upload (no bearer), not /upload (bearer-only).
    assert 'action="/ui/upload"' in resp.text
    # Multi-file upload supported.
    assert "multiple" in resp.text


def test_dashboard_lists_merchants(client: TestClient, merchant: MerchantRow) -> None:
    resp = client.get("/ui/merchants")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text


def test_merchant_detail_shows_aggregate_tiles(
    client: TestClient, merchant: MerchantRow
) -> None:
    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200
    assert "True Revenue" in resp.text
    assert "drill down" in resp.text


def test_aggregate_drilldown_returns_contributing_transactions(
    client: TestClient,
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    docs = list(doc_repo._docs.values())
    assert docs, "fixture should have created a document"
    document_id = docs[0].id

    resp = client.get(f"/ui/documents/{document_id}/aggregate/true_revenue")
    assert resp.status_code == 200
    # Partial header includes the aggregate label.
    assert "True Revenue" in resp.text
    # Includes the page/line refs from the synthetic transaction.
    assert "page 1" in resp.text and "line 10" in resp.text


def test_aggregate_drilldown_unknown_aggregate_400(
    client: TestClient, doc_repo: InMemoryDocumentRepository
) -> None:
    document_id = next(iter(doc_repo._docs.values())).id
    resp = client.get(f"/ui/documents/{document_id}/aggregate/not_real")
    assert resp.status_code == 400


def test_dashboard_deals_lists_merchant_with_latest_doc(
    client: TestClient, merchant: MerchantRow
) -> None:
    resp = client.get("/ui/deals")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text
    # The fixture's parsed result has parse_status="proceed".
    assert "tag-proceed" in resp.text


def test_dashboard_review_queue_empty_by_default(client: TestClient) -> None:
    """Fixture document is parse_status='proceed', so review queue is empty."""
    resp = client.get("/ui/review")
    assert resp.status_code == 200
    assert "No documents in manual-review state" in resp.text


def test_dashboard_review_queue_lists_manual_review_doc(
    client: TestClient,
    doc_repo: InMemoryDocumentRepository,
    merchant: MerchantRow,
) -> None:
    """Force a document into manual_review state and verify the queue surfaces it."""
    docs = list(doc_repo._docs.values())
    target = docs[0]
    flagged = target.model_copy(
        update={
            "parse_status": "manual_review",
            "fraud_score": 78,
            "all_flags": ["[META] incremental_saves: 2 EOF markers"],
        }
    )
    doc_repo._docs[target.id] = flagged

    resp = client.get("/ui/review")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text
    assert "incremental_saves" in resp.text
    assert "78" in resp.text


def test_dashboard_nav_links_visible(client: TestClient) -> None:
    """Phase 7A added Deals + Review + Funders to the nav."""
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert 'href="/ui/deals"' in resp.text
    assert 'href="/ui/review"' in resp.text
    assert 'href="/ui/funders"' in resp.text


def test_merchant_new_form_renders(client: TestClient) -> None:
    resp = client.get("/ui/merchants/new")
    assert resp.status_code == 200
    assert "New Merchant" in resp.text
    assert 'name="business_name"' in resp.text
    assert 'name="state"' in resp.text


def test_merchant_new_submit_creates_and_redirects(
    client: TestClient, merchant_repo: InMemoryMerchantRepository
) -> None:
    resp = client.post(
        "/ui/merchants/new",
        data={
            "business_name": "Beta Bakery LLC",
            "owner_name": "Sam Roe",
            "state": "FL",
            "credit_score": "720",
            "time_in_business_months": "36",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/ui/merchants/")
    rows = merchant_repo.list_all()
    assert any(m.business_name == "Beta Bakery LLC" and m.state == "FL" for m in rows)


def test_merchant_new_submit_rejects_unserved_state(client: TestClient) -> None:
    resp = client.post(
        "/ui/merchants/new",
        data={
            "business_name": "Texas Test Co",
            "owner_name": "Ima Test",
            "state": "TX",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "TX" in resp.text or "not served" in resp.text.lower()


def test_merchant_edit_form_pre_fills(
    client: TestClient, merchant: MerchantRow
) -> None:
    resp = client.get(f"/ui/merchants/{merchant.id}/edit")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text
    assert "Edit Merchant" in resp.text


def test_merchant_edit_submit_updates(
    client: TestClient,
    merchant: MerchantRow,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    resp = client.post(
        f"/ui/merchants/{merchant.id}/edit",
        data={
            "business_name": merchant.business_name,
            "owner_name": "Updated Owner",
            "state": merchant.state,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert merchant_repo.get(merchant.id).owner_name == "Updated Owner"


def test_funders_page_empty_renders(client: TestClient) -> None:
    resp = client.get("/ui/funders")
    assert resp.status_code == 200
    assert "Funders" in resp.text


def test_funders_page_shows_active_funders(
    client: TestClient, funder_repo_seeded: InMemoryFunderRepository
) -> None:
    """Inject a funder and verify it surfaces in the table."""
    resp = client.get("/ui/funders")
    assert resp.status_code == 200
    assert "Test Capital" in resp.text
    assert "$25,000" in resp.text
    assert "600" in resp.text


def test_funder_detail_renders_full_row(
    client: TestClient, funder_repo_seeded: InMemoryFunderRepository
) -> None:
    detail_funder = next(
        f for f in funder_repo_seeded.list_active() if f.name == "Detail Capital"
    )
    resp = client.get(f"/ui/funders/{detail_funder.id}")
    assert resp.status_code == 200
    assert "Detail Capital" in resp.text
    assert "Operator-curated note." in resp.text
    assert "TX" in resp.text


def test_funder_detail_404_when_missing(client: TestClient) -> None:
    from uuid import uuid4

    resp = client.get(f"/ui/funders/{uuid4()}")
    assert resp.status_code == 404


# --- Phase 7B: funder PDF import + matched-funders panel --------------------


@pytest.fixture
def stub_llm_extraction() -> object:
    """Canned LLMClient that returns a populated FunderGuidelineExtraction.

    Mirrors the extraction-feed pattern from tests/funders/conftest.py but
    inlined here so the dashboard tests don't require that conftest to be
    in scope.
    """

    class _Stub:
        def extract_raw_json(
            self, pdf_bytes: bytes, prompt: str
        ) -> tuple[dict[str, object], bool]:
            _ = (pdf_bytes, prompt)
            return (
                {
                    "draft": {
                        "name": "Imported Capital",
                        "min_monthly_revenue": 30000,
                        "min_credit_score": 620,
                        "accepts_stacking": False,
                        "excluded_industries": ["adult"],
                        "excluded_states": ["VT"],
                        "notes": "Operator review needed.",
                    },
                    "confidence_by_field": {
                        "min_monthly_revenue": 90,
                        "min_credit_score": 45,  # low → highlighted
                        "excluded_industries": 80,
                    },
                    "unparseable_fragments": ["renewals: case-by-case"],
                    "overall_confidence": 75,
                },
                False,
            )

        def classify_batch_json(self, prompt: str) -> dict[str, object]:
            raise NotImplementedError

    return _Stub()


def test_funder_import_form_renders(client: TestClient) -> None:
    resp = client.get("/ui/funders/import")
    assert resp.status_code == 200
    assert "Import Funder Criteria" in resp.text
    assert 'enctype="multipart/form-data"' in resp.text


def test_funder_import_review_renders_extraction(
    client: TestClient, stub_llm_extraction: object
) -> None:
    from aegis.api.deps import get_llm

    cast(FastAPI, client.app).dependency_overrides[get_llm] = lambda: stub_llm_extraction

    pdf_bytes = b"%PDF-1.4\n%any-bytes\n%%EOF\n"
    resp = client.post(
        "/ui/funders/import",
        files={"pdf": ("guidelines.pdf", pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 200
    assert "Imported Capital" in resp.text
    assert "Review Extraction" in resp.text
    # Low-confidence field flagged.
    assert "conf-low" in resp.text
    # Unparseable fragment surfaced.
    assert "renewals: case-by-case" in resp.text


def test_funder_import_review_rejects_empty_pdf(client: TestClient) -> None:
    resp = client.post(
        "/ui/funders/import",
        files={"pdf": ("empty.pdf", b"", "application/pdf")},
    )
    assert resp.status_code == 400
    assert "empty" in resp.text.lower()


def test_funder_import_save_creates_funder(
    client: TestClient, funder_repo: InMemoryFunderRepository
) -> None:
    resp = client.post(
        "/ui/funders/import/save",
        data={
            "name": "Saved Capital",
            "min_monthly_revenue": "25000",
            "min_credit_score": "600",
            "accepts_stacking": "false",
            "excluded_states": "TX, VT",
            "excluded_industries": "adult, gambling",
            "notes": "Saved from import flow.",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/ui/funders/")
    rows = funder_repo.list_active()
    saved = next((f for f in rows if f.name == "Saved Capital"), None)
    assert saved is not None
    assert saved.min_monthly_revenue is not None
    assert "TX" in saved.excluded_states
    assert "adult" in saved.excluded_industries


def test_funder_import_save_rejects_invalid_decimal(client: TestClient) -> None:
    resp = client.post(
        "/ui/funders/import/save",
        data={
            "name": "Bad Capital",
            "min_monthly_revenue": "not-a-number",
            "accepts_stacking": "false",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "validation" in resp.text.lower() or "invalid" in resp.text.lower()


def test_merchant_match_no_document_renders_placeholder(client: TestClient) -> None:
    """Merchant with no uploaded document → placeholder, not crash."""
    from aegis.merchants.models import MerchantRow

    repo = InMemoryMerchantRepository()
    bare = MerchantRow(business_name="Empty Inc", owner_name="No Doc", state="FL")
    repo.upsert(bare)
    app = cast(FastAPI, client.app)
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_repository] = lambda: InMemoryDocumentRepository()

    resp = client.get(f"/ui/merchants/{bare.id}/match")
    assert resp.status_code == 200
    assert "No analyzed document" in resp.text


def test_merchant_match_with_funders_renders_cards(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo_seeded: InMemoryFunderRepository,
) -> None:
    """Merchant + analyzed doc + funders → match cards rendered with color."""
    resp = client.get(f"/ui/merchants/{merchant.id}/match")
    assert resp.status_code == 200
    assert "Matched Funders" in resp.text
    # Both seeded funders appear.
    assert "Test Capital" in resp.text
    assert "Detail Capital" in resp.text
    # Score panel is shown.
    assert "Tier" in resp.text


def test_merchant_match_no_funders_shows_import_link(
    client: TestClient, merchant: MerchantRow
) -> None:
    """Default empty funder repo → page shows import link, no cards."""
    resp = client.get(f"/ui/merchants/{merchant.id}/match")
    assert resp.status_code == 200
    assert "No active funders configured" in resp.text
    assert 'href="/ui/funders/import"' in resp.text


def test_api_funders_extract_returns_extraction(
    client: TestClient, stub_llm_extraction: object
) -> None:
    """POST /funders/extract returns a FunderGuidelineExtraction JSON shape."""
    from aegis.api.deps import get_llm

    cast(FastAPI, client.app).dependency_overrides[get_llm] = lambda: stub_llm_extraction

    pdf_bytes = b"%PDF-1.4\n%any\n%%EOF\n"
    resp = client.post(
        "/funders/extract",
        files={"pdf": ("g.pdf", pdf_bytes, "application/pdf")},
        headers={"Authorization": "Bearer test-token-not-real"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft"]["name"] == "Imported Capital"
    assert body["overall_confidence"] == 75
    assert body["confidence_by_field"]["min_credit_score"] == 45


def test_api_score_with_matches_returns_combined_payload(
    client: TestClient,
    merchant: MerchantRow,
    funder_repo_seeded: InMemoryFunderRepository,
) -> None:
    payload = {
        "merchant_id": str(merchant.id),
        "business_name": merchant.business_name,
        "owner_name": merchant.owner_name,
        "state": merchant.state,
        "industry_naics": "238320",
        "industry_risk_tier": "moderate",
        "time_in_business_months": 36,
        "credit_score": 700,
        "avg_daily_balance": "12000.00",
        "true_revenue": "90000.00",
        "monthly_revenue": "90000.00",
        "lowest_balance": "3000.00",
        "num_nsf": 1,
        "days_negative": 0,
        "mca_positions": 0,
        "mca_daily_total": "0.00",
        "debt_to_revenue": "0.05",
        "payroll_detected": True,
        "returned_ach_count": 0,
        "statement_period_start": "2026-04-01",
        "statement_period_end": "2026-04-30",
        "statement_days": 30,
        "fraud_score": 12,
        "eof_markers": 1,
        "validation_passed": True,
        "extraction_confidence": 95,
        "requested_amount": "40000.00",
        "requested_factor": "1.30",
        "requested_term_days": 120,
    }
    resp = client.post(
        "/deals/score-with-matches",
        json=payload,
        headers={"Authorization": "Bearer test-token-not-real"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "score" in body
    assert "matched_funders" in body
    assert isinstance(body["matched_funders"], list)
    assert len(funder_repo_seeded.list_active()) >= 1


# ----------------------------------------------------------------------
# Dossier-view smoke tests (default merchant_detail surface)
# ----------------------------------------------------------------------


def test_merchant_detail_dossier_renders_without_error(
    client: TestClient, merchant: MerchantRow
) -> None:
    """The default merchant_detail view is the editorial dossier.

    Asserts the response renders and contains the section anchors that
    identify the dossier template (not the v2-panel template).
    """
    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200
    assert "The AEGIS Dossier" in resp.text
    # Section anchors that only the dossier template emits.
    assert "§ 1" in resp.text
    assert "§ 4" in resp.text  # Funder routing always present
    assert "§ 5" in resp.text  # Disposition always present
    # The dossier-page body class scopes the namespaced CSS.
    assert "dossier-page" in resp.text


def test_view_v2_query_param_falls_back_to_panels(
    client: TestClient, merchant: MerchantRow
) -> None:
    """?view=v2 explicitly opts back into the panel layout."""
    resp = client.get(f"/ui/merchants/{merchant.id}?view=v2")
    assert resp.status_code == 200
    # Dossier-only marker must NOT appear.
    assert "The AEGIS Dossier" not in resp.text
    assert "dossier-page" not in resp.text
    # v2-panel marker (drill-down aggregates) IS present.
    assert "True Revenue" in resp.text
    assert "drill down" in resp.text


def test_dossier_omits_audit_section_when_history_empty(
    client: TestClient, merchant: MerchantRow
) -> None:
    """Regression for the empty-audit-history crash fixed in commit 3ccd34f.

    With a fresh fixture audit log (no merchant subject entries), the
    § 6 Audit log section is gated out of the TOC and the page must still
    render. § 1 must always be present.
    """
    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200
    assert "§ 1" in resp.text
    # § 6 absent — the section + the TOC link are both gated by
    # `{% if history %}` and the audit log only has unrelated entries
    # (document persistence, no merchant subject rows).
    assert "§ 6" not in resp.text


# /applicants — Zoho CRM "View in Aegis" Lead button (button id 7365508000001462009)
# routes the operator to the right surface based on whether the merchant has
# documents uploaded.


def test_applicants_unknown_email_redirects_to_dashboard(client: TestClient) -> None:
    resp = client.get("/applicants?email=nobody@example.com", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/ui/"


def test_applicants_known_merchant_with_docs_redirects_to_detail(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    # Seed merchant with email; reuse the already-attached document from the
    # doc_repo fixture by pointing it at this merchant.
    m = MerchantRow(
        business_name="Doc Co", owner_name="Jane Doe", state="CA", email="ops@doc.co"
    )
    m = merchant_repo.upsert(m)
    for row in list(doc_repo._docs.values()):
        doc_repo._docs[row.id] = row.model_copy(update={"merchant_id": m.id})

    resp = client.get("/applicants?email=ops@doc.co", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/ui/merchants/{m.id}"


def test_applicants_known_merchant_no_docs_redirects_to_dashboard(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    # Detach every doc from any merchant so the new merchant has none.
    for row in list(doc_repo._docs.values()):
        doc_repo._docs[row.id] = row.model_copy(update={"merchant_id": None})
    merchant_repo.upsert(
        MerchantRow(
            business_name="Bare Co",
            owner_name="John Doe",
            state="NY",
            email="ops@bare.co",
        )
    )

    resp = client.get("/applicants?email=ops@bare.co", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/ui/"


def test_applicants_email_lookup_is_case_insensitive(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    merchant_repo.upsert(
        MerchantRow(
            business_name="Mixed Co",
            owner_name="Pat Doe",
            state="FL",
            email="Mixed@Example.COM",
        )
    )
    resp = client.get("/applicants?email=MIXED@example.com", follow_redirects=False)
    assert resp.status_code == 302
    # No docs on this merchant -> dashboard.
    assert resp.headers["location"] == "/ui/"
