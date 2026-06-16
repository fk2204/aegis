"""HTMX dashboard tests.

Verify each page renders, the merchant detail shows aggregates, and the
drill-down HTMX partial returns the contributing transactions only.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
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
    row = repo.create_document(file_hash="z" * 64, byte_size=1024, original_filename="x.pdf")
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
def audit_log() -> InMemoryAuditLog:
    """Shared in-memory audit log so tests can introspect emitted rows."""
    return InMemoryAuditLog()


@pytest.fixture
def client(
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
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
    app.dependency_overrides[get_audit] = lambda: audit_log
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
    # Funnel rows now live (have non-zero counts somewhere). After the
    # 2026-06-16 three-column Today redesign the legacy "Pipeline funnel"
    # heading text moved below the fold; assert on the stable test-id
    # exposed by the new pipeline column instead — that's the durable
    # contract a test should bind to.
    assert 'data-test-id="today-pipeline-funnel"' in resp.text


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


def test_index_attention_flags_rendered_as_chips_not_joined_string(
    client: TestClient,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    """Flags column on Today renders one chip per flag (the pattern
    review.html.j2 uses), not a ;-joined string. The joined-string
    form forced the flags column to expand and blow out the table
    layout — see fix/dashboard-table-alignment. Each chip now carries
    the raw code in ``data-flag-code`` (the humanize_flag pass adds
    plain-language title text; the identifier stays accessible for
    debugging and for the chip's hover tooltip)."""
    target = next(iter(doc_repo._docs.values()))
    flagged = target.model_copy(
        update={
            "parse_status": "manual_review",
            "fraud_score": 70,
            "all_flags": ["[META] foo_marker", "[PATTERN] bar_pattern"],
        }
    )
    doc_repo._docs[target.id] = flagged

    resp = client.get("/ui/")
    assert resp.status_code == 200
    # Each flag renders as its own chip span carrying the raw code in
    # data-flag-code — the new humanized chip shape from Proposal 2.
    assert 'data-flag-code="foo_marker"' in resp.text
    assert 'data-flag-code="bar_pattern"' in resp.text
    # No "; "-joined form (the bug this test originally protected).
    assert "foo_marker; [PATTERN]" not in resp.text


def test_index_attention_renders_all_unique_flags_no_truncation(
    client: TestClient,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    """The card layout (replaces the queue table) gives flags a full-width
    row that wraps freely. No more truncation to 3 — all unique flags
    render as chips so the operator sees the full triage picture."""
    target = next(iter(doc_repo._docs.values()))
    flagged = target.model_copy(
        update={
            "parse_status": "manual_review",
            "fraud_score": 90,
            "all_flags": [
                "[META] one",
                "[META] two",
                "[META] three",
                "[META] four",
                "[META] five",
            ],
        }
    )
    doc_repo._docs[target.id] = flagged

    resp = client.get("/ui/")
    assert resp.status_code == 200
    # All five flags render as chips (no truncation in the card view).
    # Each chip carries its raw code in ``data-flag-code`` post-humanize.
    assert 'data-flag-code="one"' in resp.text
    assert 'data-flag-code="two"' in resp.text
    assert 'data-flag-code="three"' in resp.text
    assert 'data-flag-code="four"' in resp.text
    assert 'data-flag-code="five"' in resp.text
    # No "+N more" overflow indicator — the cap is gone.
    assert "+2 more" not in resp.text


def test_index_attention_groups_same_merchant_into_one_card(
    client: TestClient,
    doc_repo: InMemoryDocumentRepository,
    merchant: MerchantRow,
) -> None:
    """The same merchant with multiple manual_review docs collapses into
    one card with embedded doc sub-rows — fixes the repeated-merchant
    wall on the Today page (Know Your Collectibles Inc x5 -> one card)."""
    # Seed three additional manual_review docs all tied to the same merchant.
    for i in range(3):
        row = doc_repo.create_document(
            file_hash=f"hash{i}".ljust(64, "0"),
            byte_size=1024,
            original_filename=f"extra-{i}.pdf",
        )
        doc_repo._docs[row.id] = row.model_copy(
            update={
                "merchant_id": merchant.id,
                "parse_status": "manual_review",
                "fraud_score": 50 + i * 10,
                "all_flags": [f"[META] doc{i}_flag"],
            }
        )
    # Plus flip the fixture's pre-existing doc to manual_review so we have 4.
    target = next(iter(doc_repo._docs.values()))
    if target.parse_status != "manual_review":
        doc_repo._docs[target.id] = target.model_copy(
            update={"parse_status": "manual_review", "fraud_score": 40}
        )

    resp = client.get("/ui/")
    assert resp.status_code == 200
    html = resp.text

    # Merchant name appears once in a card title — not four times as separate rows.
    # Loose check: there is exactly one .attention-card per unique merchant.
    # The chunk-B redesign adds a ``band-{fraud_band}`` modifier to the
    # class, so the match needs the prefix rather than the bare class.
    assert html.count('class="attention-card band-') == 1
    # The card surfaces a document count via the chunk-B "Documents · N"
    # heading (the earlier "N documents flagged" sub-line was replaced by
    # state / NAICS / requested-amount context on the merchant header).
    assert "Documents · 4" in html
    # All four doc-id stubs appear in the embedded sub-row list.
    assert html.count('class="card-doc"') == 4


def test_index_attention_card_shows_worst_fraud_score(
    client: TestClient,
    doc_repo: InMemoryDocumentRepository,
    merchant: MerchantRow,
) -> None:
    """The card-level fraud score is the max across the group's docs —
    operator triage signal so the worst case isn't hidden behind a
    moderate one."""
    # Two manual_review docs, scores 40 and 88.
    target = next(iter(doc_repo._docs.values()))
    doc_repo._docs[target.id] = target.model_copy(
        update={"parse_status": "manual_review", "fraud_score": 40}
    )
    row = doc_repo.create_document(
        file_hash="b" * 64, byte_size=1024, original_filename="second.pdf"
    )
    doc_repo._docs[row.id] = row.model_copy(
        update={
            "merchant_id": merchant.id,
            "parse_status": "manual_review",
            "fraud_score": 88,
        }
    )

    resp = client.get("/ui/")
    assert resp.status_code == 200
    # Card-level score is the worst (88), tagged "worst fraud", not 40.
    assert "worst fraud" in resp.text
    # Worst-score span carries the "bad" class because 88 >= 65.
    assert 'class="card-score bad"' in resp.text


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


def test_merchant_detail_shows_aggregate_tiles(client: TestClient, merchant: MerchantRow) -> None:
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


def test_merchant_edit_form_pre_fills(client: TestClient, merchant: MerchantRow) -> None:
    resp = client.get(f"/ui/merchants/{merchant.id}/edit")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text
    assert "Edit Merchant" in resp.text


def test_merchant_edit_submit_updates(
    client: TestClient,
    merchant: MerchantRow,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    assert merchant.state is not None  # fixture invariant
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
    detail_funder = next(f for f in funder_repo_seeded.list_active() if f.name == "Detail Capital")
    resp = client.get(f"/ui/funders/{detail_funder.id}")
    assert resp.status_code == 200
    assert "Detail Capital" in resp.text
    # Issue 3 (2026-05-27): notes_residual renders as bullet list; bullets
    # have trailing periods stripped, so assert without the period.
    assert "Operator-curated note" in resp.text
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
        def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, object], bool]:
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

        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, object], bool]:
            raise NotImplementedError

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


def test_funder_import_save_persists_step_c_fields(
    client: TestClient, funder_repo: InMemoryFunderRepository
) -> None:
    """Finding 1 fix: contact / tiers / conditions / notes_residual
    posted via the import-save form must land on the persisted
    FunderRow (pre-step-F they were silently dropped because the
    form signature didn't accept them)."""
    tiers_payload = (
        '[{"name":"Elite","buy_rate_low":"1.25","buy_rate_high":"1.30",'
        '"min_credit_score":700,"min_monthly_revenue":"100000",'
        '"max_advance":"1500000","max_holdback":"0.15"}]'
    )
    resp = client.post(
        "/ui/funders/import/save",
        data={
            "name": "Step C Saved Capital",
            "accepts_stacking": "false",
            "contact_name": "James Doe",
            "contact_phone": "555-123-4567",
            "contact_email": "james@stepc.com",
            "submission_email": "iso@stepc.com",
            "auto_decline_conditions": ("Active tax liens > $25K\nOpen bankruptcy"),
            "conditional_requirements": "Trucking: 2 yr MVR clean",
            "notes_residual": "Renewals: case-by-case after 50% paid down.",
            "tiers_json": tiers_payload,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    saved = next(
        (f for f in funder_repo.list_active() if f.name == "Step C Saved Capital"),
        None,
    )
    assert saved is not None
    assert saved.contact_name == "James Doe"
    assert saved.submission_email == "iso@stepc.com"
    assert len(saved.tiers) == 1
    assert saved.tiers[0].name == "Elite"
    assert saved.tiers[0].buy_rate_low == Decimal("1.25")
    assert saved.auto_decline_conditions == (
        "Active tax liens > $25K",
        "Open bankruptcy",
    )
    assert saved.conditional_requirements == ("Trucking: 2 yr MVR clean",)
    assert "Renewals" in saved.notes_residual


def test_funder_import_save_rejects_invalid_tiers_json(
    client: TestClient,
) -> None:
    """Malformed tier JSON → 400 with a helpful error."""
    resp = client.post(
        "/ui/funders/import/save",
        data={
            "name": "Bad Tier Capital",
            "accepts_stacking": "false",
            "tiers_json": "not-json",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "tier" in resp.text.lower()


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


def test_funder_import_save_emits_audit_row(
    client: TestClient,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> None:
    """CLAUDE.md: ``audit_log`` rows are written for every state change.
    The PDF-import save path was a bare write before — verify it now
    emits a ``funder.imported`` row at the route call site (the same
    pattern ``funder.reextracted`` and ``funder.operator_notes_updated``
    already follow).
    """
    resp = client.post(
        "/ui/funders/import/save",
        data={
            "name": "Audit Trail Capital",
            "accepts_stacking": "false",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    saved = next(
        (f for f in funder_repo.list_active() if f.name == "Audit Trail Capital"),
        None,
    )
    assert saved is not None

    rows = [e for e in audit_log.entries if e["action"] == "funder.imported"]
    assert len(rows) == 1
    row = rows[0]
    assert row["subject_type"] == "funder"
    assert row["subject_id"] == str(saved.id)
    assert row["actor"] == "dashboard"
    assert row["details"]["funder_name"] == "Audit Trail Capital"
    assert row["details"]["tier_count"] == 0


def test_funder_new_submit_creates_funder_and_emits_audit_row(
    client: TestClient,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> None:
    """Manual-create path (POST /ui/funders/new): verify upsert lands
    AND an audit row is recorded so the state change is traceable."""
    resp = client.post(
        "/ui/funders/new",
        data={
            "name": "Manual Create Capital",
            "active": "true",
            "min_monthly_revenue": "30000",
            "accepts_stacking": "false",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/ui/funders/")

    saved = next(
        (f for f in funder_repo.list_active() if f.name == "Manual Create Capital"),
        None,
    )
    assert saved is not None
    assert saved.min_monthly_revenue == Decimal("30000")

    rows = [e for e in audit_log.entries if e["action"] == "funder.created"]
    assert len(rows) == 1
    row = rows[0]
    assert row["subject_type"] == "funder"
    assert row["subject_id"] == str(saved.id)
    assert row["actor"] == "dashboard"
    assert row["details"]["funder_name"] == "Manual Create Capital"


# --- Wave 2 Track 1: multi-file + image import ----------------------------


@pytest.fixture
def stub_llm_image_extraction() -> object:
    """Canned LLMClient whose vision path returns an image-only payload.

    The PDF path raises so a test that should route to vision but hits
    the PDF code path fails loudly.
    """

    class _ImgStub:
        def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, object], bool]:
            raise NotImplementedError("vision-only stub")

        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, object], bool]:
            _ = (page_images_png, prompt)
            return (
                {
                    "draft": {
                        "name": "Shor Capital",
                        "min_monthly_revenue": 30000,
                        "excluded_industries": ["bail-bonds", "check-cashing"],
                        "accepts_stacking": False,
                        "tiers": [],
                    },
                    "confidence_by_field": {
                        "min_monthly_revenue": 90,
                        "excluded_industries": 88,
                    },
                    "unparseable_fragments": [],
                    "overall_confidence": 70,
                },
                False,
            )

        def classify_batch_json(self, prompt: str) -> dict[str, object]:
            raise NotImplementedError

    return _ImgStub()


def test_funder_import_form_accepts_image_types(client: TestClient) -> None:
    """The dropzone must declare PDF + PNG/JPEG and allow multiple files."""
    resp = client.get("/ui/funders/import")
    assert resp.status_code == 200
    assert "image/png" in resp.text
    assert "image/jpeg" in resp.text
    assert "multiple" in resp.text


def test_funder_import_review_accepts_single_image(
    client: TestClient, stub_llm_image_extraction: object
) -> None:
    """A single PNG routes through the vision path and renders review."""
    from aegis.api.deps import get_llm

    cast(FastAPI, client.app).dependency_overrides[get_llm] = lambda: stub_llm_image_extraction

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    resp = client.post(
        "/ui/funders/import",
        files={"pdf": ("guidelines.png", png_bytes, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    assert "Shor Capital" in resp.text
    assert "Review Extraction" in resp.text


def test_funder_import_review_merges_pdf_and_image(
    client: TestClient,
) -> None:
    """PDF + PNG together → fields from BOTH appear in the merged review."""
    from aegis.api.deps import get_llm

    class _DualStub:
        def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, object], bool]:
            _ = (pdf_bytes, prompt)
            return (
                {
                    "draft": {
                        "name": "Shor Capital",
                        "typical_factor_low": 1.30,
                        "typical_factor_high": 1.45,
                        "contact_name": "Iliya Mem",
                        "contact_email": "iliya@shor.capital",
                        "accepts_stacking": False,
                        "tiers": [],
                    },
                    "confidence_by_field": {
                        "typical_factor_low": 92,
                        "typical_factor_high": 92,
                        "contact_name": 95,
                        "contact_email": 95,
                    },
                    "unparseable_fragments": [],
                    "overall_confidence": 85,
                },
                False,
            )

        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, object], bool]:
            _ = (page_images_png, prompt)
            return (
                {
                    "draft": {
                        "name": "Shor Capital",
                        "min_monthly_revenue": 30000,
                        "excluded_industries": ["bail-bonds", "check-cashing"],
                        "accepts_stacking": False,
                        "tiers": [],
                    },
                    "confidence_by_field": {
                        "min_monthly_revenue": 90,
                        "excluded_industries": 88,
                    },
                    "unparseable_fragments": [],
                    "overall_confidence": 70,
                },
                False,
            )

        def classify_batch_json(self, prompt: str) -> dict[str, object]:
            raise NotImplementedError

    cast(FastAPI, client.app).dependency_overrides[get_llm] = lambda: _DualStub()

    resp = client.post(
        "/ui/funders/import",
        files=[
            ("pdf", ("iso.pdf", b"%PDF-1.4\n%payload\n%%EOF\n", "application/pdf")),
            ("pdf", ("guidelines.png", b"\x89PNG\r\n\x1a\n\x00" * 32, "image/png")),
        ],
    )

    assert resp.status_code == 200, resp.text
    # PDF-only fields surface.
    assert "Iliya Mem" in resp.text
    assert "1.30" in resp.text or "1.3" in resp.text
    # PNG-only fields surface.
    assert "bail-bonds" in resp.text
    assert "30000" in resp.text or "30,000" in resp.text


def test_funder_import_review_rejects_unsupported_type(client: TestClient) -> None:
    """A .docx (or any unsupported MIME) → 400 with a clear error."""
    resp = client.post(
        "/ui/funders/import",
        files={
            "pdf": (
                "guidelines.docx",
                b"PK\x03\x04 fake docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert resp.status_code == 400
    assert "unsupported" in resp.text.lower() or "accepted" in resp.text.lower()


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
    assert "Matched funders" in resp.text
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
    from uuid import uuid4

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
    # ``document_id`` is required post-U17 so the score call writes
    # an immutable decisions snapshot.
    document_id = uuid4()
    resp = client.post(
        f"/deals/score-with-matches?document_id={document_id}",
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


def test_view_v2_query_param_still_responds(client: TestClient, merchant: MerchantRow) -> None:
    """The legacy ?view=v2 link was retired when the app unified on the
    dossier. Any bookmarked v2 URL must still 200 — the query param is
    silently ignored and the dossier render is returned."""
    resp = client.get(f"/ui/merchants/{merchant.id}?view=v2")
    assert resp.status_code == 200
    # Dossier render — same as a no-param request.
    assert "The AEGIS Dossier" in resp.text
    assert "True Revenue" in resp.text


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


# /applicants — CRM "View in Aegis" Lead button (originally Zoho button
# id 7365508000001462009, being reconfigured in Close). Routes the
# operator to the right surface based on whether the merchant has
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
    m = MerchantRow(business_name="Doc Co", owner_name="Jane Doe", state="CA", email="ops@doc.co")
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
