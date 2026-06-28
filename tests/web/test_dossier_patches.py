"""Render coverage for the 2026-06-26 dossier patches.

The 1fd6c11 commit shipped five visible UI elements without dedicated
pytest coverage (the existing snapshots only catch the Track A panel).
This module locks the contract on the other four data-test-ids plus
the background-checks stub introduced by the patch alongside these
tests:

* ``narrator-prompt`` — yellow "Generate deal summary" card that renders
  when ``narrator_summary`` is None and a scoreable analysis exists.
* ``dossier-funder-matching-manual-review-banner`` — amber banner above
  the matched-funders grid when every settled statement is in
  ``manual_review``.
* ``plain_status_label`` macro — plain-English ledger label for each
  ``parse_status`` + ``manual_review_reason`` combination.
* ``dossier-stacking-alert`` — red banner when one or more confirmed MCA
  cadences detected, yellow when three or more pattern-only cadences.
* ``dossier-raw-flags-section`` / ``dossier-raw-flags-details`` —
  collapsible that opens by default only when ``narrator_summary`` is
  absent.
* ``background-checks-section`` — inert stub reserving the slot for
  Agent 2's UCC / web-presence / OFAC roll-up.

Tests render the template directly through the real Jinja environment,
mirroring ``test_dossier_credit_score_chip.py``. No FastAPI app, no
repository wiring — the contract under test is the template source.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from aegis.merchants.models import MerchantRow
from aegis.storage import AnalysisRow, DocumentRow
from aegis.web._templates import templates


def _make_merchant() -> MerchantRow:
    return MerchantRow(
        business_name="Acme Logistics LLC",
        owner_name="Jane Doe",
        state="NY",
        industry_naics="484110",
        time_in_business_months=24,
        credit_score=720,
        intake_date=date(2026, 5, 1),
    )


def _make_doc(
    *,
    parse_status: str = "proceed",
    all_flags: list[str] | None = None,
    fraud_score: int | None = 20,
    original_filename: str = "stmt.pdf",
) -> DocumentRow:
    return DocumentRow(
        id=uuid4(),
        file_hash=uuid4().hex,
        byte_size=1024,
        original_filename=original_filename,
        parse_status=parse_status,
        fraud_score=fraud_score,
        all_flags=all_flags or [],
        uploaded_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _make_analysis() -> AnalysisRow:
    """Build a benign AnalysisRow so the § 2 cashflow block renders."""
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=None,
        statement_period_start=date(2026, 5, 1),
        statement_period_end=date(2026, 5, 31),
        statement_days=31,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("1500.00"),
        avg_daily_balance=Decimal("2000.00"),
        true_revenue=Decimal("50000.00"),
        monthly_revenue=Decimal("50000.00"),
        lowest_balance=Decimal("500.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        returned_ach_count=0,
    )


class _StubScore:
    """Minimal score_result so the funder-matching section renders cards."""

    def __init__(self, recommendation: str = "approve") -> None:
        self.recommendation = recommendation
        self.score = 70
        self.tier = "B"
        self.paper_grade = "B"
        self.suggested_max_advance = Decimal("50000")
        self.hard_decline_reasons: list[str] = []
        self.soft_concerns: list[str] = []
        self.decline_details: dict[str, Any] = {}


class _StubCashflow:
    """Mirrors the CashflowSignals attrs the dossier + Track-B panel touch.

    All defaults are benign zero-ish values; the stacking-alert tests
    set ``mca_confirmed_count`` and ``mca_pattern_count`` explicitly.
    """

    def __init__(
        self,
        *,
        mca_confirmed_count: int = 0,
        mca_pattern_count: int = 0,
    ) -> None:
        self.true_revenue_total = Decimal("50000")
        self.monthly_revenue_estimate = Decimal("10000")
        self.statement_period_days = 30
        self.nsf_count = 0
        self.mca_position_count = mca_confirmed_count + mca_pattern_count
        self.mca_confirmed_count = mca_confirmed_count
        self.mca_pattern_count = mca_pattern_count
        self.international_client_share_pct: Decimal | None = None
        self.average_daily_balance: Decimal | None = None
        self.lowest_balance: Decimal | None = None
        self.negative_days = 0


class _StubRiskBand:
    def __init__(self, *, cashflow: _StubCashflow) -> None:
        self.band = "low"
        self.action = "auto_forward"
        self.cashflow = cashflow
        self.reasons: tuple[Any, ...] = ()
        self.insufficient_data_factors: tuple[str, ...] = ()


class _StubUnifiedTracks:
    """Carries the attrs `_unified_tracks_panel.html.j2` reads in addition
    to the stacking-alert macro on `merchant_detail_dossier.html.j2`."""

    def __init__(
        self,
        *,
        mca_confirmed_count: int = 0,
        mca_pattern_count: int = 0,
    ) -> None:
        self.integrity_verdicts: tuple[Any, ...] = ()
        self.integrity_worst_verdict = None
        self.integrity_summary = ""
        self.risk_band = _StubRiskBand(
            cashflow=_StubCashflow(
                mca_confirmed_count=mca_confirmed_count,
                mca_pattern_count=mca_pattern_count,
            )
        )
        self.context_panel = None
        self.industry_tier = None
        self.industry_tier_reason = ""
        self.insufficient_data_reason = ""


def _funder_card(funder_name: str = "Wide Net Capital") -> dict[str, Any]:
    return {
        "funder_id": str(uuid4()),
        "funder_name": funder_name,
        "match_score": 75,
        "color": "green",
        "hard_reasons": [],
        "soft_concerns": [],
        "criteria_comparison": [],
        "funder_requires_coj": False,
        "funder_charges_merchant_advance_fees": False,
        "estimated_terms": None,
        "tier_matches": [],
        "historical_approval_rate": None,
    }


def _render(
    *,
    merchant: MerchantRow | None = None,
    documents: list[dict[str, Any]] | None = None,
    document: DocumentRow | None = None,
    analysis: AnalysisRow | None = None,
    narrator_summary: dict[str, Any] | None = None,
    matched_funders: list[dict[str, Any]] | None = None,
    all_statements_manual_review: bool = False,
    unified_tracks: _StubUnifiedTracks | None = None,
    score_recommendation: str = "approve",
    has_score: bool = True,
) -> str:
    """Render the dossier template with stub context."""
    return templates.get_template("merchant_detail_dossier.html.j2").render(
        request=None,
        merchant=merchant or _make_merchant(),
        documents=documents or [],
        document=document,
        analysis=analysis,
        aggregate_labels={},
        aggregate_unit_kind={},
        pattern_cards=[],
        latest_transactions=[],
        soft_signals=None,
        has_concentration_pattern=False,
        from_intake=False,
        intake_docs_uploaded=0,
        intake_docs_failed=0,
        score_result=_StubScore(score_recommendation) if has_score else None,
        score_window=None,
        statement_coverage=None,
        stacking=None,
        mca_stack=None,
        balance_health=None,
        offer=None,
        state_tier=None,
        ofac_status="pending",
        ofac_match=None,
        trend=None,
        history=[],
        close_last_orchestration_capped=False,
        unified_tracks=unified_tracks,
        shadow_signals=[],
        merchant_shadow_signals=[],
        revenue_trends=None,
        funder_note_submissions=[],
        operator_notes=[],
        operator_note_max_chars=2000,
        deal_summary=None,
        funder_narrative="",
        doc_checklist={
            "voided_check_on_file": False,
            "drivers_license_on_file": False,
            "bank_statements_months": 0,
        },
        stips_result=None,
        top_matched_funder_name=None,
        matched_funders=matched_funders or [],
        matched_funder_responses={},
        submitted_funder_ids=set(),
        narrator_summary=narrator_summary,
        all_statements_manual_review=all_statements_manual_review,
    )


# ---------------------------------------------------------------------------
# Fix 1 — narrator-prompt
# ---------------------------------------------------------------------------


def test_narrator_prompt_renders_when_summary_absent_and_analysis_present() -> None:
    doc = _make_doc()
    html = _render(
        document=doc,
        analysis=_make_analysis(),
        narrator_summary=None,
    )
    assert 'data-test-id="narrator-prompt"' in html
    assert 'data-test-id="narrator-prompt-generate"' in html
    assert "No deal summary yet" in html


def test_narrator_prompt_absent_when_summary_populated() -> None:
    doc = _make_doc()
    narrator = {
        "deal_summary": "Clean five-month history, ready to route.",
        "flag_explanations": [],
        "recommended_action": {
            "action": "submit",
            "next_step": "Submit now",
            "top_funder_match": None,
            "estimated_terms": None,
        },
    }
    html = _render(
        document=doc,
        analysis=_make_analysis(),
        narrator_summary=narrator,
    )
    assert 'data-test-id="narrator-prompt"' not in html
    assert 'data-test-id="narrator-summary"' in html


def test_narrator_prompt_absent_when_no_analysis() -> None:
    # Prompt requires both analysis AND document — fresh merchant
    # with no analysis row gets the existing "no statements" copy
    # in the masthead, not the prompt card.
    html = _render(document=None, analysis=None, narrator_summary=None)
    assert 'data-test-id="narrator-prompt"' not in html


# ---------------------------------------------------------------------------
# Fix 2 — manual-review banner above the funder matching grid
# ---------------------------------------------------------------------------


def test_manual_review_banner_renders_when_all_settled_manual_review() -> None:
    html = _render(
        matched_funders=[_funder_card()],
        all_statements_manual_review=True,
    )
    assert 'data-test-id="dossier-funder-matching-manual-review-banner"' in html
    assert "Statements in manual review" in html


def test_manual_review_banner_absent_when_some_statements_proceed() -> None:
    html = _render(
        matched_funders=[_funder_card()],
        all_statements_manual_review=False,
    )
    assert 'data-test-id="dossier-funder-matching-manual-review-banner"' not in html


def test_manual_review_banner_absent_when_no_matched_funders() -> None:
    # Banner is gated inside the matches-rendering branch; if there
    # are no matches the empty-state copy runs instead and the
    # banner stays hidden.
    html = _render(matched_funders=[], all_statements_manual_review=True)
    assert 'data-test-id="dossier-funder-matching-manual-review-banner"' not in html


# ---------------------------------------------------------------------------
# Fix 3 — plain_status_label macro
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parse_status", "all_flags", "expected_label_fragment", "expected_tag"),
    [
        ("proceed", [], "Parsed clean", "proceed"),
        ("review", [], "Review band", "review"),
        ("pending", [], "Being analyzed", "pending"),
        ("error", [], "Parse error", "error"),
        (
            "manual_review",
            ["editor_detected", "reconciliation_drift"],
            "Possible tampering",
            "manual_review",
        ),
        (
            "manual_review",
            ["reconciliation_drift"],
            "Math discrepancy",
            "manual_review",
        ),
        (
            "manual_review",
            ["period_unresolved"],
            "Statement period couldn",
            "manual_review",
        ),
        (
            "manual_review",
            ["extraction_failed"],
            "Parser couldn",
            "manual_review",
        ),
        (
            "manual_review",
            [],
            "Needs manual review",
            "manual_review",
        ),
    ],
)
def test_plain_status_label_outputs(
    parse_status: str,
    all_flags: list[str],
    expected_label_fragment: str,
    expected_tag: str,
) -> None:
    doc = _make_doc(parse_status=parse_status, all_flags=all_flags)
    html = _render(documents=[{"document": doc, "analysis": None}])
    assert expected_label_fragment in html
    # The tag chip re-emits the raw parse_status next to the label.
    assert f">{expected_tag}<" in html


# ---------------------------------------------------------------------------
# Fix 5 — stacking-alert banner
# ---------------------------------------------------------------------------


def test_stacking_alert_confirmed_renders_when_one_or_more_named_funder() -> None:
    html = _render(
        matched_funders=[_funder_card()],
        unified_tracks=_StubUnifiedTracks(mca_confirmed_count=1, mca_pattern_count=0),
    )
    assert 'data-test-id="dossier-stacking-alert"' in html
    assert 'data-stacking-severity="confirmed"' in html
    assert "Confirmed MCA stack detected" in html


def test_stacking_alert_pattern_renders_when_three_or_more_pattern_only() -> None:
    html = _render(
        matched_funders=[_funder_card()],
        unified_tracks=_StubUnifiedTracks(mca_confirmed_count=0, mca_pattern_count=3),
    )
    assert 'data-test-id="dossier-stacking-alert"' in html
    assert 'data-stacking-severity="pattern"' in html
    assert "Possible MCA stack" in html


def test_stacking_alert_absent_when_both_counts_under_threshold() -> None:
    html = _render(
        matched_funders=[_funder_card()],
        unified_tracks=_StubUnifiedTracks(mca_confirmed_count=0, mca_pattern_count=2),
    )
    assert 'data-test-id="dossier-stacking-alert"' not in html


def test_stacking_alert_absent_when_unified_tracks_missing() -> None:
    html = _render(
        matched_funders=[_funder_card()],
        unified_tracks=None,
    )
    assert 'data-test-id="dossier-stacking-alert"' not in html


# ---------------------------------------------------------------------------
# Fix 5 — raw-flags collapsible
# ---------------------------------------------------------------------------


def test_raw_flags_section_renders_when_document_has_flags() -> None:
    doc = _make_doc(all_flags=["[H-01] mca_stacking", "[H-02] high_nsf_rate"])
    html = _render(document=doc, analysis=_make_analysis(), narrator_summary=None)
    assert 'data-test-id="dossier-raw-flags-section"' in html
    assert "[H-01] mca_stacking" in html
    assert "[H-02] high_nsf_rate" in html


def test_raw_flags_details_always_closed() -> None:
    """2026-06-28 — the technical-audit-log details block is ALWAYS
    closed regardless of narrator_summary presence. The raw [META] /
    [MATH] tokens never serve as primary content; the Track A/B/C
    plain-English panel above is the canonical render. Operators who
    need the raw audit strings still have one-click access via the
    <summary>.
    """
    doc = _make_doc(all_flags=["[H-01] mca_stacking"])
    # Both branches (narrator present / absent) must render closed.
    html_no_narrator = _render(document=doc, analysis=_make_analysis(), narrator_summary=None)
    narrator = {
        "deal_summary": "Stable five-month history.",
        "flag_explanations": [],
        "recommended_action": {
            "action": "submit",
            "next_step": "Submit now",
            "top_funder_match": None,
            "estimated_terms": None,
        },
    }
    html_with_narrator = _render(document=doc, analysis=_make_analysis(), narrator_summary=narrator)
    for html in (html_no_narrator, html_with_narrator):
        assert 'data-test-id="dossier-raw-flags-details"' in html
        # ``open`` attribute MUST be absent in both branches.
        assert 'data-test-id="dossier-raw-flags-details" open' not in html
        # Summary text renamed from "Show flags" → "Technical audit log".
        assert "Technical audit log" in html


def test_raw_flags_section_absent_when_no_flags() -> None:
    doc = _make_doc(all_flags=[])
    html = _render(document=doc, analysis=_make_analysis())
    assert 'data-test-id="dossier-raw-flags-section"' not in html


# ---------------------------------------------------------------------------
# Patch 2 — visible Background-checks stub
# ---------------------------------------------------------------------------


def test_background_checks_section_renders_visible_placeholder() -> None:
    """A1 reserved a visible bg-checks slot; A2 replaced the inline stub
    with the real include (`_background_checks_section.html.j2`). The
    section renders empty-state rows ("Not checked") with HTMX
    refresh buttons when no scan has run for the merchant."""
    html = _render()
    assert 'data-test-id="dossier-background-checks"' in html
    # Header is "Background <em>checks</em>" — text is split by markup.
    assert "Background" in html
    assert "checks</em>" in html
    # Empty-state row text from the A2 partial.
    assert "Not checked" in html
