"""PA A9 — dossier product chip on the funder matching section header.

The chip renders the merchant's product type in plain English so the
operator can see at-a-glance that the funder matching column is
filtered to product-eligible funders. The chip is always present so
the dossier never silently hides the product context; legacy rows pre-
PA-A7 fall back to "Revenue-based".

Render-only tests — no FastAPI surface needed. Uses the Jinja
environment exposed by ``aegis.web._templates`` directly.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.web._templates import templates


class _StubScore:
    """Minimal duck-typed ScoreResult — recommendation drives the
    funder section's decline-state vs. inline-match branching."""

    def __init__(self, recommendation: str = "approve") -> None:
        self.recommendation = recommendation
        self.score = 70
        self.tier = "B"
        self.paper_grade = "B"
        self.suggested_max_advance = Decimal("50000")
        self.hard_decline_reasons: list[str] = []
        self.soft_concerns: list[str] = []
        self.decline_details: dict[str, Any] = {}


def _render_dossier(*, merchant: MerchantRow | SimpleNamespace) -> str:
    """Render the dossier template with the minimum context the
    funder-matching section header needs. Branches that consume other
    context blocks collapse to empty / Undefined safely.
    """
    template = templates.get_template("merchant_detail_dossier.html.j2")
    return template.render(
        request=None,
        merchant=merchant,
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
        matched_funders=[],
        matched_funder_responses={},
        submitted_funder_ids=set(),
    )


def _make_merchant_ns(product_type: str | None) -> SimpleNamespace:
    """Build a duck-typed merchant — PA A7 wires ``product_type`` on
    ``MerchantRow`` proper; until that lands, SimpleNamespace is the
    cleanest way to test the template's defensive Jinja access."""
    return SimpleNamespace(
        id=uuid4(),
        business_name="Acme Painting LLC",
        owner_name="Jane Owner",
        state="CA",
        industry_naics="722511",
        industry_choice=None,
        time_in_business_months=24,
        credit_score=720,
        close_lead_id="lead_abc",
        product_type=product_type,
        web_presence_flags=None,
        ucc_filings=None,
        ucc_default_indicators=None,
    )


def test_dossier_renders_product_chip_for_revenue_based() -> None:
    """Default product — chip must render the 'Revenue-based' label."""
    merchant = _make_merchant_ns("revenue_based")
    html = _render_dossier(merchant=cast(MerchantRow, merchant))
    assert 'data-test-id="dossier-funder-matching-product-chip"' in html
    assert "Product: Revenue-based" in html


def test_dossier_renders_product_chip_for_business_loan() -> None:
    merchant = _make_merchant_ns("business_loan")
    html = _render_dossier(merchant=cast(MerchantRow, merchant))
    assert 'data-test-id="dossier-funder-matching-product-chip"' in html
    assert "Product: Business loan" in html


def test_dossier_renders_product_chip_for_equipment() -> None:
    merchant = _make_merchant_ns("equipment")
    html = _render_dossier(merchant=cast(MerchantRow, merchant))
    assert "Product: Equipment finance" in html


def test_dossier_product_chip_falls_back_to_revenue_based_for_null() -> None:
    """Legacy rows pre-PA-A7 have no product_type → Jinja sees the
    attribute as None/Undefined and the template falls back to the
    revenue_based label. Chip is always present so the dossier never
    silently hides the product context."""
    merchant = _make_merchant_ns(None)
    html = _render_dossier(merchant=cast(MerchantRow, merchant))
    assert 'data-test-id="dossier-funder-matching-product-chip"' in html
    assert "Product: Revenue-based" in html


def test_dossier_product_chip_falls_back_for_unknown_product_string() -> None:
    """A typo / pre-validation product string lands on the
    'Revenue-based' fallback rather than a missing label."""
    merchant = _make_merchant_ns("totally_made_up")
    html = _render_dossier(merchant=cast(MerchantRow, merchant))
    assert "Product: Revenue-based" in html
