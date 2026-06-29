"""Dossier render coverage for the SBA eligibility badge.

Master plan § 12.1 — the verdict header carries a small badge that
flips between a green "SBA Eligible — {program}" pill and a gray
"SBA: Not eligible" pill (with a <details> block listing blockers).
The badge is informational only; this test verifies the visual
contract, not any decision-boundary behavior.
"""

from __future__ import annotations

from decimal import Decimal

from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.sba_eligibility import SBAEligibilityResult
from aegis.web._templates import templates


def _make_merchant() -> MerchantRow:
    return MerchantRow(
        business_name="Rendezvous Inc",
        owner_name="Jane Owner",
        state="CA",
        industry_naics="722511",
        time_in_business_months=420,
        credit_score=708,
    )


def _render_dossier(*, sba_eligibility: SBAEligibilityResult | None) -> str:
    """Render the dossier template with the minimum context needed.

    Mirrors the helper in ``test_dossier_monthly_trend`` — Jinja's
    default ``Undefined`` lets unrelated sections collapse silently
    while we exercise the verdict-header SBA branch.
    """
    template = templates.get_template("merchant_detail_dossier.html.j2")
    return template.render(
        request=None,
        merchant=_make_merchant(),
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
        score_result=None,
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
        merchant_monthly_trend=[],
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
        matched_funders=[],
        matched_funder_responses={},
        submitted_funder_ids=set(),
        sba_eligibility=sba_eligibility,
    )


def test_dossier_renders_green_badge_when_eligible() -> None:
    result = SBAEligibilityResult(
        eligible=True,
        program="7(a)",
        blockers=[],
        strengths=["FICO ≥ 700 — strong credit"],
        estimated_max_amount=Decimal("120000") * Decimal("36"),
    )

    html = _render_dossier(sba_eligibility=result)

    assert 'data-test-id="sba-eligible-badge"' in html
    assert "SBA Eligible — 7(a)" in html
    assert "badge-proceed" in html
    assert "Est. max $4,320,000" in html
    # The ineligible <details> block must NOT render when eligible.
    assert 'data-test-id="sba-ineligible-details"' not in html


def test_dossier_renders_gray_pill_when_not_eligible() -> None:
    result = SBAEligibilityResult(
        eligible=False,
        program=None,
        blockers=["Active bankruptcy on file", "FICO < 650 (SBA 7(a) typical floor)"],
        strengths=[],
        estimated_max_amount=None,
    )

    html = _render_dossier(sba_eligibility=result)

    assert 'data-test-id="sba-ineligible-details"' in html
    assert "SBA: Not eligible" in html
    assert "badge-none" in html
    # Both blockers must appear in the <details> body.
    assert "Active bankruptcy on file" in html
    assert "FICO &lt; 650" in html
    # The eligible badge must NOT render in the ineligible branch.
    assert 'data-test-id="sba-eligible-badge"' not in html


def test_dossier_renders_no_badge_when_sba_context_missing() -> None:
    """When ``sba_eligibility`` is None the dossier renders no badge."""
    html = _render_dossier(sba_eligibility=None)

    assert 'data-test-id="sba-eligible-badge"' not in html
    assert 'data-test-id="sba-ineligible-details"' not in html
