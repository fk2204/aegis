"""Marketplace badge for criteria-less funders.

Three funders in the live catalog (Splash Advance, Big Think Capital,
Bizi Connect, observed 2026-06-20) are active but carry no published
underwriting criteria. They route to aggregator funnels rather than
underwriting directly, so the match_score on their cards is
meaningless. ``_is_marketplace_funder`` flags them so both the
inline dossier match panel, the standalone /match panel, and the
funders list surface a "Marketplace" badge in place of the score.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.scoring.models import FunderMatch
from aegis.web._templates import templates
from aegis.web.routers.merchants import _is_marketplace_funder, _match_card


def _funder(
    *,
    active: bool = True,
    min_monthly_revenue: Decimal | None = None,
    min_credit_score: int | None = None,
    max_positions: int | None = None,
) -> FunderRow:
    return FunderRow(
        id=uuid4(),
        name="Test Funder",
        active=active,
        min_monthly_revenue=min_monthly_revenue,
        min_credit_score=min_credit_score,
        max_positions=max_positions,
        requires_coj=False,
        charges_merchant_advance_fees=False,
    )


# ---------------------------------------------------------------------------
# _is_marketplace_funder helper
# ---------------------------------------------------------------------------


def test_marketplace_helper_true_for_active_funder_with_no_criteria() -> None:
    assert _is_marketplace_funder(_funder()) is True


def test_marketplace_helper_false_when_min_monthly_revenue_is_set() -> None:
    assert _is_marketplace_funder(_funder(min_monthly_revenue=Decimal("1000"))) is False


def test_marketplace_helper_false_when_min_credit_score_is_set() -> None:
    assert _is_marketplace_funder(_funder(min_credit_score=650)) is False


def test_marketplace_helper_false_when_max_positions_is_set() -> None:
    assert _is_marketplace_funder(_funder(max_positions=3)) is False


def test_marketplace_helper_false_for_inactive_funder() -> None:
    """An inactive funder is correctly inactive — it shouldn't qualify
    for the marketplace badge even when its criteria are unset, because
    the operator-status chip already surfaces the inactive state."""
    assert _is_marketplace_funder(_funder(active=False)) is False


# ---------------------------------------------------------------------------
# _match_card surfaces the flag onto the card dict
# ---------------------------------------------------------------------------


def _match() -> FunderMatch:
    return FunderMatch(
        funder_id=uuid4(),
        funder_name="Test Funder",
        match_score=42,
        reasons=["tier_C"],
        soft_concerns=[],
    )


def test_match_card_marks_marketplace_funder() -> None:
    card = _match_card(_funder(), _match())
    assert card["is_marketplace"] is True


def test_match_card_does_not_mark_normal_funder() -> None:
    card = _match_card(
        _funder(min_monthly_revenue=Decimal("1000"), min_credit_score=650, max_positions=4),
        _match(),
    )
    assert card["is_marketplace"] is False


# ---------------------------------------------------------------------------
# Dossier inline panel template — Marketplace badge replaces match score
# ---------------------------------------------------------------------------


def _card_dict(*, is_marketplace: bool, match_score: int = 75) -> dict[str, object]:
    """Minimal card dict shape that the dossier inline panel template reads."""
    return {
        "funder_id": str(uuid4()),
        "funder_name": "Wide Net Capital",
        "match_score": match_score,
        "color": "green",
        "hard_reasons": [],
        "soft_concerns": [],
        "criteria_comparison": [],
        "funder_requires_coj": False,
        "funder_charges_merchant_advance_fees": False,
        "estimated_terms": None,
        "tier_matches": [],
        "historical_approval_rate": None,
        "is_marketplace": is_marketplace,
    }


class _StubMerchant:
    """Minimal merchant stub for the dossier template render context."""

    id = uuid4()
    business_name = "Render Test LLC"
    owner_name = "Owner"
    state = "CA"
    industry_naics = None
    entity_type = None
    time_in_business_months = None
    credit_score = 720
    intake_date = None
    requested_amount = None
    requested_factor = None
    requested_term_days = None
    close_lead_id = None
    close_lead_description = None
    close_notes_summary = None
    close_call_transcripts = None
    submitted_to_funder_ids: ClassVar[list[str]] = []
    last_submitted_at = None
    voided_check_on_file = False
    drivers_license_on_file = False
    bank_statements_months = 0


def _render_inline_panel(*, is_marketplace: bool, match_score: int = 75) -> str:
    """Render the dossier template with a single matched-funder card
    of the requested shape. The § 4 panel is the section under test."""

    class _StubScore:
        recommendation = "approve"
        score = 70
        tier = "B"
        paper_grade = "B"
        suggested_max_advance = Decimal("50000")
        hard_decline_reasons: ClassVar[list[str]] = []
        soft_concerns: ClassVar[list[str]] = []
        decline_details: ClassVar[dict[str, object]] = {}

    template = templates.get_template("merchant_detail_dossier.html.j2")
    return template.render(
        request=None,
        merchant=_StubMerchant(),
        documents=[],
        document=None,
        analysis=None,
        aggregate_labels={},
        aggregate_unit_kind={},
        pattern_cards=[],
        latest_transactions=[],
        soft_signals=None,
        has_concentration_pattern=False,
        from_intake=False,
        intake_docs_uploaded=0,
        intake_docs_failed=0,
        score_result=_StubScore(),
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
        unified_tracks=None,
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
        matched_funders=[_card_dict(is_marketplace=is_marketplace, match_score=match_score)],
        matched_funder_responses={},
        submitted_funder_ids=set(),
    )


def test_dossier_inline_panel_renders_marketplace_badge() -> None:
    html = _render_inline_panel(is_marketplace=True, match_score=99)
    assert 'data-test-id="dossier-matched-funder-marketplace-badge"' in html
    assert 'data-marketplace="true"' in html
    # The numeric score MUST NOT render on a marketplace card.
    assert ">99<" not in html


def test_dossier_inline_panel_renders_score_when_not_marketplace() -> None:
    html = _render_inline_panel(is_marketplace=False, match_score=87)
    assert 'data-test-id="dossier-matched-funder-marketplace-badge"' not in html
    # The numeric score renders verbatim on a normal card.
    assert ">87<" in html


# ---------------------------------------------------------------------------
# Standalone /match panel — same badge swap
# ---------------------------------------------------------------------------


def _render_standalone_match(*, is_marketplace: bool, match_score: int = 75) -> str:
    class _StubScore:
        recommendation = "approve"
        score = 70
        tier = "B"
        paper_grade = "B"
        suggested_max_advance = Decimal("50000")
        hard_decline_reasons: ClassVar[list[str]] = []
        soft_concerns: ClassVar[list[str]] = []
        decline_details: ClassVar[dict[str, object]] = {}

    template = templates.get_template("merchant_match.html.j2")
    return template.render(
        request=None,
        merchant=_StubMerchant(),
        missing=None,
        score_result=_StubScore(),
        matches=[_card_dict(is_marketplace=is_marketplace, match_score=match_score)],
        score_window=None,
        funder_responses={},
        preselect_funder_id=None,
        preselect_banner=None,
    )


def test_standalone_match_panel_renders_marketplace_badge() -> None:
    html = _render_standalone_match(is_marketplace=True, match_score=99)
    assert 'data-test-id="match-card-marketplace-badge"' in html
    assert ">99<" not in html


def test_standalone_match_panel_renders_score_when_not_marketplace() -> None:
    html = _render_standalone_match(is_marketplace=False, match_score=87)
    assert 'data-test-id="match-card-marketplace-badge"' not in html
    assert ">87<" in html


# ---------------------------------------------------------------------------
# Funders list template — Marketplace chip next to operator-status chip
# ---------------------------------------------------------------------------


class _StubPerformanceRow:
    """Row shape the funders list template reads: ``row.funder`` plus
    derived ``row.approval_rate`` / ``row.last_submitted_at``."""

    def __init__(self, funder: FunderRow) -> None:
        self.funder = funder
        self.approval_rate = None
        self.last_submitted_at = None


def _render_funders_list(funder: FunderRow) -> str:
    template = templates.get_template("funders.html.j2")
    return template.render(
        request=None,
        funder_rows=[_StubPerformanceRow(funder)],
        performance_window_days=90,
        relative_time=lambda *_args, **_kw: "—",
        now_utc=None,
    )


def test_funders_list_renders_marketplace_badge_for_criteria_less_funder() -> None:
    html = _render_funders_list(_funder())
    assert 'data-test-id="funders-marketplace-badge"' in html


def test_funders_list_omits_marketplace_badge_for_funder_with_criteria() -> None:
    html = _render_funders_list(
        _funder(min_monthly_revenue=Decimal("1000"), min_credit_score=650, max_positions=4)
    )
    assert 'data-test-id="funders-marketplace-badge"' not in html


def test_funders_list_omits_marketplace_badge_for_inactive_funder() -> None:
    html = _render_funders_list(_funder(active=False))
    assert 'data-test-id="funders-marketplace-badge"' not in html
