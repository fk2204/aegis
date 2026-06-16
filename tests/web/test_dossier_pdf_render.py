"""PDF dossier HTML-render parity tests for Sprint 6 Track B.

Validates that the printable PDF template renders the same chips the
on-screen dossier shows the operator: industry-tier classification,
revenue trends, offer sizing, top funder matches, and submission
history. Built so the funder-facing PDF (the artifact the operator
emails when a funder requests the full underwriting package) carries
the full chip set rather than the historical § 1-5 subset.

Strategy
--------
These tests render the Jinja TEMPLATE to an HTML string via
``_build_pdf_dossier_context`` + ``templates.get_template`` and assert
on the HTML. They deliberately do NOT exercise the WeasyPrint binary
path — WeasyPrint's native libs (Pango / Cairo / HarfBuzz) ship on the
Hetzner production box but are absent on Windows native dev boxes; the
HTML-only assertion is portable across both. ``tests/web/test_dossier
_pdf.py`` already covers the bytes / 503 contract for the route end-
to-end on Linux.

Graceful-omission cases each construct a different scenario and assert
the matching section either omits cleanly OR shows the "no matches /
no history" empty-state copy. They are NOT integration tests of the
matcher / submission repo — they prove the template handles ``None``
without rendering a confusing empty section heading.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.parser.metadata import MetadataAnalysis
from aegis.parser.models import (
    Aggregates,
    ClassifiedTransaction,
    ExtractedStatement,
    StatementSummary,
    ValidationResult,
    _SourcedInt,
    _SourcedMoney,
)
from aegis.parser.pipeline import PipelineResult
from aegis.scoring.ofac import OFACClient
from aegis.storage import InMemoryDocumentRepository
from aegis.web._templates import templates
from aegis.web.routers.merchants import _build_pdf_dossier_context
from tests.test_storage import _make_pipeline_result

# ---------------------------------------------------------------------------
# Pipeline fixture helpers
# ---------------------------------------------------------------------------


def _make_fat_pipeline_result(
    *,
    true_revenue: Decimal = Decimal("60000.00"),
    months: int = 2,
) -> PipelineResult:
    """Build a PipelineResult substantial enough to trigger the full
    chip set: an above-floor ``offer`` (true_revenue >= $5k floor),
    a multi-month ``monthly_breakdown`` (so revenue_trends renders),
    and high enough monthly revenue to clear at least one funder gate
    (so top_matched_funders renders non-empty).

    Default $60k single-statement true_revenue feeds compute_offer
    above its $5k floor; the two-month monthly_breakdown gives
    revenue_trends.months_compared = 2 which clears the template's
    ``>= 2`` gate.
    """
    tx_id = uuid4()
    summary = StatementSummary(
        beginning_balance=Decimal("5000.00"),
        ending_balance=Decimal("8000.00"),
        deposit_total=true_revenue,
        withdrawal_total=true_revenue - Decimal("3000.00"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 28),
    )
    classified = [
        ClassifiedTransaction(
            id=tx_id,
            posted_date=date(2026, 1, 5),
            description="DEPOSIT",
            amount=true_revenue,
            running_balance=Decimal("65000.00"),
            source_page=1,
            source_line=10,
            category="deposit",
            classification_confidence=95,
        )
    ]
    aggregates = Aggregates(
        avg_daily_balance=_SourcedMoney(value=Decimal("15000.00"), source_ids=[tx_id]),
        true_revenue=_SourcedMoney(value=true_revenue, source_ids=[tx_id]),
        num_nsf=_SourcedInt(value=0, source_ids=[]),
        days_negative=_SourcedInt(value=0, source_ids=[]),
        debt_to_revenue=Decimal("0.00"),
        mca_daily_total=_SourcedMoney(value=Decimal("0.00"), source_ids=[]),
    )
    extraction_stub: Any = type(
        "Stub",
        (),
        {"statement": ExtractedStatement(summary=summary, transactions=classified)},
    )()

    # monthly_breakdown drives ScoreInput.monthly_breakdown which drives
    # compute_revenue_trends. Two buckets at distinct calendar months
    # give months_compared >= 2 so the trends section renders.
    monthly_breakdown: list[dict[str, str]] = []
    if months >= 1:
        monthly_breakdown.append(
            {
                "month": "2026-01",
                "deposits": str(true_revenue / 2),
                "withdrawals": str((true_revenue - Decimal("3000")) / 2),
                "avg_balance": "12000.00",
                "nsf_count": "0",
            }
        )
    if months >= 2:
        monthly_breakdown.append(
            {
                "month": "2026-02",
                "deposits": str(true_revenue / 2),
                "withdrawals": str((true_revenue - Decimal("3000")) / 2),
                "avg_balance": "18000.00",
                "nsf_count": "0",
            }
        )

    return PipelineResult(
        parse_status="proceed",
        metadata=MetadataAnalysis(
            pdf_creation_date=None,
            pdf_modification_date=None,
            pdf_producer=None,
            pdf_creator=None,
            pdf_author=None,
            page_count=2,
            file_size_bytes=10240,
            eof_markers=1,
            page_sizes=["LETTER"],
            flags=[],
            fraud_score=0,
        ),
        extraction=extraction_stub,
        validation=ValidationResult(passed=True),
        classified=classified,
        patterns=None,
        aggregates=aggregates,
        fraud_score=10,
        fraud_score_breakdown={"metadata_score": 0, "math_score": 0, "patterns_score": 0},
        all_flags=[],
        monthly_breakdown=monthly_breakdown,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def merchant() -> MerchantRow:
    """Finalized merchant with industry choice + requested terms.

    ``industry_choice`` populates the unified-tracks industry tier chip
    (drives § 6). State CA + finalized status lets OFAC + state-compliance
    sections render in their populated branches.
    """
    return MerchantRow(
        business_name="Bright Sky Plumbing LLC",
        owner_name="Maria Calderon",
        state="CA",
        industry_choice="Construction — Specialty Trades",
        time_in_business_months=36,
        credit_score=680,
        requested_amount=Decimal("75000"),
        requested_factor=Decimal("1.30"),
    )


@pytest.fixture
def doc_repo(merchant: MerchantRow) -> InMemoryDocumentRepository:
    """One parsed document attached to the merchant — enough to drive
    the scoring / mca_stack / balance_health pipeline.

    Uses the fat pipeline result (above the $5k offer floor, two-month
    breakdown so revenue_trends renders) so the fully-populated test
    case exercises every new section.
    """
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="d" * 64, byte_size=1024, original_filename="statement.pdf"
    )
    row = row.model_copy(update={"merchant_id": merchant.id})
    repo._docs[row.id] = row
    repo.persist_parse_result(row.id, result=_make_fat_pipeline_result(), merchant_id=merchant.id)
    return repo


@pytest.fixture
def thin_doc_repo(merchant: MerchantRow) -> InMemoryDocumentRepository:
    """Thin pipeline (below-floor revenue) for the offer-None case."""
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="e" * 64, byte_size=1024, original_filename="statement.pdf"
    )
    row = row.model_copy(update={"merchant_id": merchant.id})
    repo._docs[row.id] = row
    repo.persist_parse_result(row.id, result=_make_pipeline_result(), merchant_id=merchant.id)
    return repo


@pytest.fixture
def single_month_doc_repo(merchant: MerchantRow) -> InMemoryDocumentRepository:
    """Single-month fixture for the revenue-trends omission case."""
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="f" * 64, byte_size=1024, original_filename="statement.pdf"
    )
    row = row.model_copy(update={"merchant_id": merchant.id})
    repo._docs[row.id] = row
    repo.persist_parse_result(
        row.id, result=_make_fat_pipeline_result(months=1), merchant_id=merchant.id
    )
    return repo


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    """Funder roster with one permissive + one strict row.

    The permissive funder clears every gate the ``_make_pipeline_result``
    fixture produces; the strict one fails on min_monthly_revenue. Lets
    the top-matches section render at least one row when included.
    """
    repo = InMemoryFunderRepository()
    repo.upsert(
        FunderRow(
            name="Permissive Capital",
            active=True,
            min_monthly_revenue=Decimal("1000"),
            min_avg_daily_balance=Decimal("100"),
        )
    )
    repo.upsert(
        FunderRow(
            name="Strict Capital",
            active=True,
            min_monthly_revenue=Decimal("100000"),
            min_avg_daily_balance=Decimal("50000"),
        )
    )
    return repo


@pytest.fixture
def funder_note_subs(
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
) -> InMemoryFunderNoteSubmissionRepository:
    """Submission history with two terminal-state rows.

    Both submissions reference a real funder id from ``funder_repo``;
    template renders status + offer + responded_at chips.
    """
    repo = InMemoryFunderNoteSubmissionRepository()
    funders = funder_repo.list_active()
    approved_funder = funders[0]
    declined_funder = funders[1] if len(funders) > 1 else funders[0]

    row1 = repo.create(
        merchant_id=merchant.id,
        funder_id=approved_funder.id,
        funder_note="historical funder note copy",
        submitted_by="operator@commerafunding.com",
    )
    repo.update_status(
        row1.id,
        status="approved",
        offer_amount=Decimal("50000"),
        offer_factor=Decimal("1.2500"),
    )
    row2 = repo.create(
        merchant_id=merchant.id,
        funder_id=declined_funder.id,
        funder_note="historical funder note copy",
        submitted_by="operator@commerafunding.com",
    )
    repo.update_status(row2.id, status="declined")
    return repo


@pytest.fixture
def empty_funder_repo() -> InMemoryFunderRepository:
    """Empty roster — proves the top-matches empty-state branch."""
    return InMemoryFunderRepository()


@pytest.fixture
def empty_funder_note_subs() -> InMemoryFunderNoteSubmissionRepository:
    """No submissions yet — proves the submission-history omission branch."""
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def ofac() -> OFACClient | None:
    """No OFAC client wired — same as the legacy PDF tests."""
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_template(context: dict[str, object]) -> str:
    """Render the PDF Jinja template to an HTML string.

    Returns the raw HTML so callers can assert on section headings,
    chip labels, and empty-state copy. Pure Jinja — does NOT invoke
    WeasyPrint, so the test is portable across Windows + Linux.
    """
    template = templates.get_template("merchant_detail_dossier_pdf.html.j2")
    return template.render(context)


# ---------------------------------------------------------------------------
# Case 1: fully populated — every new section heading present
# ---------------------------------------------------------------------------


def test_dossier_pdf_renders_all_new_sections_when_fully_populated(
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    ofac: OFACClient | None,
) -> None:
    context = _build_pdf_dossier_context(
        merchant,
        doc_repo,
        ofac,
        funder_repo=funder_repo,
        funder_note_subs=funder_note_subs,
    )
    html = _render_template(context)

    # Legacy headings still render — back-compat smoke check.
    assert "§ 1" in html, "verdict heading missing"
    assert "§ 2" in html, "cashflow heading missing"

    # New dedicated sections. ``§ 6`` industry classification only renders
    # when unified_tracks carries a tier — which it does because the
    # merchant has industry_choice set.
    assert "§ 6" in html, "industry classification heading missing"
    assert "Industry classification" in html

    # § 7 revenue trends needs >= 2 months_compared — the fat pipeline
    # fixture supplies two monthly_breakdown buckets so the section
    # renders.
    assert "§ 7" in html, "revenue trends heading missing"
    assert "Revenue trends" in html

    # § 8 offer sizing renders when ``offer`` is non-None — which it is
    # for any finalized merchant with monthly revenue above the $5k floor.
    assert "§ 8" in html, "offer sizing heading missing"
    assert "Offer sizing" in html

    # § 9 top funder matches always renders when ``top_matched_funders``
    # is in the context — even when empty (shows empty-state copy).
    assert "§ 9" in html, "top funder matches heading missing"
    assert "Top funder matches" in html

    # § 10 submission history renders only when the list is non-empty.
    assert "§ 10" in html, "submission history heading missing"
    assert "Submission history" in html

    # The merchant + content data points appear too.
    assert merchant.business_name in html
    assert "Permissive Capital" in html, "top match funder name missing"
    assert "approved" in html, "submission status missing from history table"


def test_dossier_pdf_renders_at_least_six_new_headings_when_full_data(
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    ofac: OFACClient | None,
) -> None:
    """Per the spec: at least 6 of the new headings should be present
    when full data is supplied.

    The full set is § 1, § 2, § 4, § 5, § 6, § 7, § 8, § 9, § 10
    (§ 3 needs pattern_cards or stacking which the fat fixture omits).
    """
    context = _build_pdf_dossier_context(
        merchant,
        doc_repo,
        ofac,
        funder_repo=funder_repo,
        funder_note_subs=funder_note_subs,
    )
    html = _render_template(context)

    headings = ["§ 1", "§ 2", "§ 4", "§ 5", "§ 6", "§ 7", "§ 8", "§ 9", "§ 10"]
    present = [h for h in headings if h in html]
    assert len(present) >= 6, (
        f"expected at least 6 section headings, found {len(present)}: {present}"
    )


# ---------------------------------------------------------------------------
# Case 2: no matched funders — top-matches section shows empty state
# ---------------------------------------------------------------------------


def test_dossier_pdf_top_matches_empty_state_when_no_funders(
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
    empty_funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    ofac: OFACClient | None,
) -> None:
    context = _build_pdf_dossier_context(
        merchant,
        doc_repo,
        ofac,
        funder_repo=empty_funder_repo,
        funder_note_subs=funder_note_subs,
    )
    html = _render_template(context)

    # Section still renders so the funder-facing PDF doesn't have a
    # phantom gap, but the empty-state copy replaces the table.
    assert "§ 9" in html
    assert "No matches found" in html, "empty-state copy missing"
    assert "Permissive Capital" not in html


# ---------------------------------------------------------------------------
# Case 3: no submission history — section omitted
# ---------------------------------------------------------------------------


def test_dossier_pdf_submission_history_omitted_when_empty(
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    empty_funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    ofac: OFACClient | None,
) -> None:
    context = _build_pdf_dossier_context(
        merchant,
        doc_repo,
        ofac,
        funder_repo=funder_repo,
        funder_note_subs=empty_funder_note_subs,
    )
    html = _render_template(context)

    # Submission-history section is gracefully omitted (no heading at
    # all rather than a blank "no submissions" line — the spec wants
    # NOT shown with blank rows).
    assert "§ 10" not in html
    assert "Submission history" not in html


# ---------------------------------------------------------------------------
# Case 4: no MCA stacking findings — stacking-specific section omitted
# ---------------------------------------------------------------------------


def test_dossier_pdf_stacking_section_omitted_when_no_findings(
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    ofac: OFACClient | None,
) -> None:
    """No MCA debits in the parse → ``stacking`` is None, mca_stack
    has active_mca_count=0. The ``§ 3 Pattern findings`` section omits
    its stacking sub-block in that case (template guard:
    ``{% if stacking or pattern_cards %}``); also the ``§ 2`` scoreboard
    omits the MCA-counterparties / monthly-load rows.

    The single-month fixture has zero MCA debits — confirm the stacking
    sub-section isn't rendered with confusing zero values.
    """
    context = _build_pdf_dossier_context(
        merchant,
        doc_repo,
        ofac,
        funder_repo=funder_repo,
        funder_note_subs=funder_note_subs,
    )
    html = _render_template(context)

    # The "Active MCA counterparties" row in § 2 is guarded by
    # active_mca_count > 0 — should NOT render.
    assert "Active MCA counterparties" not in html, (
        "MCA-counterparty scoreboard row leaked despite zero findings"
    )
    # The MCA stacking sub-heading in § 3 only shows when ``stacking``
    # exists. It should not render.
    assert "MCA stacking" not in html, "MCA stacking sub-heading leaked despite zero findings"


# ---------------------------------------------------------------------------
# Case 5: offer=None — § 8 offer sizing section omitted
# ---------------------------------------------------------------------------


def test_dossier_pdf_offer_section_omitted_when_scoring_incomplete(
    merchant: MerchantRow,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    ofac: OFACClient | None,
) -> None:
    """A merchant with no parsed documents has no ``offer`` (scoring
    didn't run) — § 8 offer sizing must be omitted, not rendered
    blank."""
    empty_doc_repo = InMemoryDocumentRepository()
    context = _build_pdf_dossier_context(
        merchant,
        empty_doc_repo,
        ofac,
        funder_repo=funder_repo,
        funder_note_subs=funder_note_subs,
    )
    html = _render_template(context)

    assert "§ 8" not in html, "offer sizing heading leaked despite offer=None"
    assert "Offer sizing" not in html


# ---------------------------------------------------------------------------
# Case 6: revenue trends — single-month fixture → trends section omitted
# ---------------------------------------------------------------------------


def test_dossier_pdf_revenue_trends_omitted_when_under_two_months(
    merchant: MerchantRow,
    single_month_doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    ofac: OFACClient | None,
) -> None:
    """Single-month fixture produces months_compared=1 → all-flat
    trends. The template guard is ``months_compared >= 2`` so the
    whole § 7 section should be omitted rather than rendered with
    flat values that imply real trend data.
    """
    context = _build_pdf_dossier_context(
        merchant,
        single_month_doc_repo,
        ofac,
        funder_repo=funder_repo,
        funder_note_subs=funder_note_subs,
    )
    html = _render_template(context)

    assert "§ 7" not in html, "revenue trends heading leaked at single-month"
    assert "Revenue trends" not in html


# ---------------------------------------------------------------------------
# Case 7: non-finalized merchant — score-derived sections gracefully missing
# ---------------------------------------------------------------------------


def test_dossier_pdf_provisional_merchant_omits_score_dependent_sections(
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    ofac: OFACClient | None,
) -> None:
    """A provisional merchant (pre-finalization placeholder) skips the
    scoring branch, so ``offer``, ``score_result``, and ``top_matched_
    funders`` are all empty. The template still renders verdict +
    cashflow + statements without crashing."""
    provisional = MerchantRow(
        business_name="(awaiting parse)",
        status="provisional",
        state="CA",
    )
    context = _build_pdf_dossier_context(
        provisional,
        doc_repo,
        ofac,
        funder_repo=funder_repo,
        funder_note_subs=funder_note_subs,
    )
    html = _render_template(context)

    # § 1 still renders (carries the "score unavailable" copy when
    # score_result is None).
    assert "§ 1" in html
    # § 8 offer sizing omitted on a provisional (no offer was computed).
    assert "§ 8" not in html
    # § 9 top funder matches: provisional merchant has no score_result,
    # so top_matched_funders is empty — the template renders the empty
    # state, not the table.
    assert "No matches found" in html


# ---------------------------------------------------------------------------
# Case 8: legacy callers without funder_repo / funder_note_subs deps
# ---------------------------------------------------------------------------


def test_dossier_pdf_legacy_caller_without_repos_renders_cleanly(
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
    ofac: OFACClient | None,
) -> None:
    """The submit-to-funders flow calls _maybe_render_dossier_pdf
    without threading funder_repo / funder_note_subs (back-compat
    signature). The PDF must still render — new sections gracefully
    omit when their data sources are absent.
    """
    context = _build_pdf_dossier_context(merchant, doc_repo, ofac)
    html = _render_template(context)

    # Verdict + cashflow + offer still render — they don't depend on
    # the optional repos.
    assert "§ 1" in html
    assert "§ 2" in html
    assert "§ 8" in html, "offer sizing should render even without funder repos"

    # Submission-history section gracefully omitted.
    assert "§ 10" not in html

    # Top-matches section: legacy callers get an empty top_matched_
    # funders list, so the template renders the empty-state copy.
    assert "§ 9" in html
    assert "No matches found" in html
